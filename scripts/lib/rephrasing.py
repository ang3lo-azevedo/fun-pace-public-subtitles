"""Optional post-translation polish pass: faster-whisper's translate task gets
the meaning right but reads stiff and overly literal in side-by-side
testing, because it's a speech-to-text model's built-in
translation head, not a fluency-tuned language model. This module runs a
small local instruction-tuned LLM over each cue to rewrite it more naturally
while preserving meaning, names, and honorifics - same uvx-isolated-subprocess
pattern as the rest of scripts/lib/transcription.py.
"""
from __future__ import annotations

import glob
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

from lib.common import log, run_command
from lib.transcription import rocm_available

# Pinned to a specific quantization so re-runs stay reproducible and the cache
# key below can't silently point at a different model later.
REPHRASE_MODEL_FILENAME = "Qwen2.5-3B-Instruct-Q4_K_M.gguf"
REPHRASE_MODEL_URL = (
    "https://huggingface.co/bartowski/Qwen2.5-3B-Instruct-GGUF/resolve/main/"
    + REPHRASE_MODEL_FILENAME
)
REPHRASE_MODEL_CACHE_DIR = Path.home() / ".cache" / "fun-pace-subs" / "rephrase-model"

# Pinned exact version: this is installed by building from source with custom
# CMAKE_ARGS, not a normal pip/uvx resolve, so an unpinned version could drift
# to a build that behaves differently between runs.
REPHRASE_LLAMA_CPP_VERSION = "0.3.33"
REPHRASE_VENV_DIR = Path.home() / ".cache" / "fun-pace-subs" / "rephrase-venv"
REPHRASE_VENV_VARIANT_FILE = REPHRASE_VENV_DIR / ".variant"

# CPU-only inference for a 3B model turned out to be the slowest stage in the
# whole pipeline (~28-30 minutes for one episode's cues, longer
# than transcription+alignment+translation combined). llama.cpp's HIP/ROCm
# backend gives a real ~9x per-token speedup on this hardware and - unlike
# CTranslate2 - doesn't hit the known RDNA4 LLVM codegen crash, so it's worth
# the one-time build complexity below. There's no prebuilt ROCm wheel for
# llama-cpp-python (same situation as CTranslate2), so this builds from source
# via CMAKE_ARGS, which needs nixpkgs' rocmPackages split across many
# individual store paths fed into CMAKE_PREFIX_PATH - upstream cmake configs
# expect these merged into one prefix, which nixpkgs deliberately doesn't do.
# Version-pinned to match this project's current flake.lock nixpkgs revision;
# bump these fragments if a nixpkgs update changes the installed versions.
ROCM_CMAKE_DEP_FRAGMENTS = [
    "rocm-device-libs-22",
    "rocm-comgr-22",
    "rocm-runtime-7.2.3",
    "rocm-runtime-7.2.1",
    "rocrand-7.2.1",
    "hipblas-common-7.2.1",
    "hipblas-7.2.1",
    "hiprand-7.2.1",
    "hipsparse-7.2.1",
    "hipsolver-7.2.1",
    "rocblas-7.2.1",
    "miopen-7.2.1",
]


def _find_store_path(fragment: str) -> str | None:
    matches = sorted(p for p in glob.glob(f"/nix/store/*{fragment}*") if not p.endswith(".drv"))
    return matches[0] if matches else None


def detect_amdgpu_target() -> str | None:
    """Parses rocminfo for the GPU agent's gfx architecture (e.g. "gfx1200")
    rather than hardcoding one, so this keeps working if the hardware changes.
    The CPU agent listed by rocminfo never starts with "gfx", so the first
    match is always the GPU.
    """
    try:
        result = run_command(["rocminfo"], capture=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    match = re.search(r"^\s*Name:\s+(gfx\w+)", result.stdout, re.MULTILINE)
    return match.group(1) if match else None


def rephrase_build_env(env: dict[str, str]) -> tuple[dict[str, str], int]:
    """Returns the env to build/run llama-cpp-python with, and the
    n_gpu_layers value to pass at inference time (-1 for "all layers on GPU",
    0 for CPU-only). Falls back to plain CPU on any non-ROCm machine (no
    NVIDIA-specific path here since this project's target hardware is AMD;
    llama-cpp-python's own CUDA wheels cover NVIDIA users who need that).
    """
    if not rocm_available():
        return dict(env), 0

    # Resolved via `which hipcc`, not a name-fragment glob: nixpkgs also ships a
    # separate, much smaller "clr-*-icd" package (just the ICD loader, no
    # compiler) that a fragment search like "clr-7" matches just as readily,
    # silently pointing CMAKE_PREFIX_PATH at the wrong package entirely.
    hipcc = shutil.which("hipcc")
    gfx_target = detect_amdgpu_target()
    if not hipcc or not gfx_target:
        log("ROCm detected but couldn't resolve the HIP toolchain/GPU target; rephrasing will run on CPU.")
        return dict(env), 0
    rocm_path = str(Path(hipcc).resolve().parent.parent)

    prefix_parts = [rocm_path]
    for fragment in ROCM_CMAKE_DEP_FRAGMENTS:
        found = _find_store_path(fragment)
        if found:
            prefix_parts.append(found)

    build_env = dict(env)
    build_env["ROCM_PATH"] = rocm_path
    build_env["HIP_PATH"] = rocm_path
    existing_prefix = build_env.get("CMAKE_PREFIX_PATH", "")
    build_env["CMAKE_PREFIX_PATH"] = ":".join(prefix_parts + ([existing_prefix] if existing_prefix else []))
    build_env["CMAKE_ARGS"] = f"-DGGML_HIP=ON -DAMDGPU_TARGETS={gfx_target}"
    return build_env, -1


def ensure_rephrase_venv(build_env: dict[str, str], variant: str) -> Path:
    """`uvx --from llama-cpp-python` turned out unusable for this: its tool
    cache keys off the package name/version only, not CMAKE_ARGS, so it
    silently kept reusing a plain CPU build from before GPU support existed,
    twice, including through an explicit `uv cache clean`
    (which cleared files but the exact same cached archive got reused anyway).
    Managing a plain venv at a fixed path directly sidesteps that: this
    project fully controls when it gets rebuilt, via the variant marker file
    below, instead of trusting uv's opaque cache-key logic for a build whose
    identity depends on environment variables it doesn't track.
    """
    if REPHRASE_VENV_VARIANT_FILE.is_file() and REPHRASE_VENV_VARIANT_FILE.read_text().strip() == variant:
        return REPHRASE_VENV_DIR / "bin" / "python"

    log(f"Building the rephrasing venv (variant: {variant}). This only happens once per variant.")
    if REPHRASE_VENV_DIR.exists():
        shutil.rmtree(REPHRASE_VENV_DIR)
    run_command(["uv", "venv", str(REPHRASE_VENV_DIR)], env=build_env)
    run_command(
        [
            "uv", "pip", "install",
            "--python", str(REPHRASE_VENV_DIR / "bin" / "python"),
            f"llama-cpp-python=={REPHRASE_LLAMA_CPP_VERSION}",
        ],
        env=build_env,
    )
    REPHRASE_VENV_VARIANT_FILE.write_text(variant)
    return REPHRASE_VENV_DIR / "bin" / "python"


def rephrase_model_path() -> Path:
    """A small (3B, Q4) instruct model is deliberately chosen over something
    bigger: the rewriting task itself is simple enough not to need a larger
    one, and it keeps the one-time download and per-cue inference cost low.
    """
    REPHRASE_MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    model_path = REPHRASE_MODEL_CACHE_DIR / REPHRASE_MODEL_FILENAME
    if not model_path.is_file():
        log(f"Downloading rephrasing model from {REPHRASE_MODEL_URL}")
        urllib.request.urlretrieve(REPHRASE_MODEL_URL, model_path)
    return model_path


def rephrase_srt(input_srt: str, output_srt: str, env: dict[str, str]) -> bool:
    """Runs after terminology normalization (so the model sees correctly-named
    characters) and before line-wrapping (so wrapping applies to the final
    text, not the pre-rewrite draft). Feeds a couple of already-rewritten
    lines back in as context, the same rolling-window trick used for
    translation itself, so pronouns/references stay consistent across cues
    instead of each line being rewritten in total isolation.
    """
    model_path = rephrase_model_path()
    build_env, n_gpu_layers = rephrase_build_env(env)
    variant = f"gpu-{build_env.get('CMAKE_ARGS', '')}" if n_gpu_layers != 0 else "cpu"
    venv_python = ensure_rephrase_venv(build_env, variant)
    if n_gpu_layers != 0:
        log("Rephrasing with GPU acceleration (ROCm).")
    else:
        log("Rephrasing on CPU (no ROCm GPU detected).")

    python_code = (
        "import re\n"
        "import sys\n"
        "\n"
        "from llama_cpp import Llama\n"
        "\n"
        "input_srt, output_srt, model_path, n_gpu_layers = sys.argv[1:5]\n"
        "n_gpu_layers = int(n_gpu_layers)\n"
        "\n"
        "def parse_srt(raw):\n"
        "    blocks = re.split(r'\\r?\\n\\r?\\n', raw.strip())\n"
        "    cues = []\n"
        "    for block in blocks:\n"
        "        lines = block.splitlines()\n"
        "        if len(lines) < 3:\n"
        "            continue\n"
        "        cues.append((lines[0], lines[1], ' '.join(lines[2:])))\n"
        "    return cues\n"
        "\n"
        "with open(input_srt, encoding='utf-8-sig') as fh:\n"
        "    cues = parse_srt(fh.read())\n"
        "if not cues:\n"
        "    raise SystemExit('No cues to rephrase')\n"
        "\n"
        "llm = Llama(model_path=model_path, n_ctx=2048, n_gpu_layers=n_gpu_layers, verbose=False)\n"
        "\n"
        "SYSTEM = (\n"
        "    'You rewrite anime subtitle lines to sound natural and conversational in '\n"
        "    'English, the way a native speaker would actually say it. Keep the exact '\n"
        "    'same meaning and tone. Keep character names and honorifics unchanged. '\n"
        "    'Output ONLY the rewritten line, no quotes, no explanation, no extra '\n"
        "    'commentary. If it already sounds natural, return it unchanged.'\n"
        ")\n"
        "\n"
        "# Recent already-rewritten lines as prior turns, not just prompt text, keeps\n"
        "# the model anchored on the established conversational register instead of\n"
        "# treating every cue as a cold, context-free rewrite request.\n"
        "recent_pairs = []\n"
        "\n"
        "def rephrase(text):\n"
        "    if not text.strip():\n"
        "        return text\n"
        "    messages = [{'role': 'system', 'content': SYSTEM}]\n"
        "    for original, rewritten in recent_pairs[-2:]:\n"
        "        messages.append({'role': 'user', 'content': original})\n"
        "        messages.append({'role': 'assistant', 'content': rewritten})\n"
        "    messages.append({'role': 'user', 'content': text})\n"
        "    result = llm.create_chat_completion(messages=messages, temperature=0.0, max_tokens=120)\n"
        "    rewritten = result['choices'][0]['message']['content'].strip().strip('\"')\n"
        "    return rewritten or text\n"
        "\n"
        "with open(output_srt, 'w', encoding='utf-8') as handle:\n"
        "    for index_str, timecode, text in cues:\n"
        "        rewritten = rephrase(text)\n"
        "        recent_pairs.append((text, rewritten))\n"
        "        handle.write(f'{index_str}\\n{timecode}\\n{rewritten}\\n\\n')\n"
    )

    args = [
        str(venv_python),
        "-c",
        python_code,
        input_srt,
        output_srt,
        str(model_path),
        str(n_gpu_layers),
    ]

    try:
        run_command(args, env=build_env)
    except subprocess.CalledProcessError as exc:
        log(f"LLM rephrasing failed, keeping the literal translation: {exc}")
        return False
    return Path(output_srt).is_file()
