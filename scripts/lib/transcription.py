"""The transcription/translation engine. This is the most involved module in
the pipeline: it drives faster-whisper on the GPU (ROCm) with a hand-tuned
set of workarounds for real bugs hit on this hardware, falls back to
WhisperX/CPU when needed, and implements the two-stage translate flow
(transcribe + align in the source language first, then translate each
already-precisely-timed segment independently) that fixes the timing and
context problems a single translate-mode pass has over long audio.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from lib.common import command_exists, die, log, run_command, stem_for

# Pinned instead of "latest" so a future OpenNMT release can't silently change
# behavior underneath us. Bump deliberately and re-test the RDNA4 workaround
# below still applies. See rocm_ctranslate2_wheel_path() for why this needs
# its own wheel at all (it's not published to PyPI).
CTRANSLATE2_ROCM_RELEASE_VERSION = "v4.8.1"
CTRANSLATE2_ROCM_RELEASE_URL = f"https://github.com/OpenNMT/CTranslate2/releases/download/{CTRANSLATE2_ROCM_RELEASE_VERSION}/rocm-python-wheels-Linux.zip"
CTRANSLATE2_ROCM_CACHE_DIR = Path.home() / ".cache" / "fun-pace-subs" / "ctranslate2-rocm"


def rocm_available() -> bool:
    """ROCm commonly exposes /dev/kfd. rocminfo is a secondary signal."""
    return Path("/dev/kfd").exists() or command_exists("rocminfo")


def nvidia_available() -> bool:
    return command_exists("nvidia-smi")


def python_wheel_tag() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def rocm_ctranslate2_wheel_path() -> Path:
    """OpenNMT doesn't publish ROCm-enabled CTranslate2 wheels to PyPI (only
    CPU/CUDA), so this downloads their GitHub release bundle once and caches
    the extracted wheel keyed by (version, python ABI tag) - that keying
    matters: without it, bumping CTRANSLATE2_ROCM_RELEASE_VERSION would
    silently keep using a stale cached wheel from the old version.
    """
    wheel_cache_dir = CTRANSLATE2_ROCM_CACHE_DIR / CTRANSLATE2_ROCM_RELEASE_VERSION / python_wheel_tag()
    wheel_cache_dir.mkdir(parents=True, exist_ok=True)

    wheel_candidates = sorted(wheel_cache_dir.glob("ctranslate2-*.whl"))
    if wheel_candidates:
        return wheel_candidates[0]

    archive_path = wheel_cache_dir / "rocm-python-wheels-Linux.zip"
    if not archive_path.is_file():
        log(f"Downloading ROCm CTranslate2 wheel bundle from {CTRANSLATE2_ROCM_RELEASE_URL}")
        urllib.request.urlretrieve(CTRANSLATE2_ROCM_RELEASE_URL, archive_path)

    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.namelist():
            if member.endswith(".whl") and python_wheel_tag() in Path(member).name:
                extracted_name = Path(member).name
                extracted_path = wheel_cache_dir / extracted_name
                if not extracted_path.is_file():
                    with archive.open(member) as source, open(extracted_path, "wb") as destination:
                        shutil.copyfileobj(source, destination)
                return extracted_path

    die(f"Unable to find a ROCm CTranslate2 wheel for {python_wheel_tag()} in {archive_path}")


def whisperx_batch_candidates(device: str, requested_batch: str, resolved_batch: str) -> list[str]:
    """Only auto-tune on GPU with an explicit "auto" request: CPU batch sizing
    isn't the bottleneck there, and a user-specified batch size should be
    respected as-is rather than silently downgraded.
    """
    if device != "cuda" or requested_batch != "auto":
        return [resolved_batch]

    ordered = ["32", "24", "16", "12", "8", "6", "4", "2", "1"]
    if resolved_batch in ordered:
        ordered.remove(resolved_batch)
    return [resolved_batch] + ordered


def transcribe_audio_faster_whisper(
    input_audio: str,
    output_dir: str,
    model: str,
    language: str,
    compute_type: str,
    device: str,
    batch_size: str,
    env: dict[str, str],
    task: str = "translate",
) -> tuple[str, str]:
    """This and the other transcribe/align/translate functions below run their
    actual work as a `python -c <script>` subprocess under `uvx --from
    faster-whisper` (or `--from whisperx`) rather than importing those
    packages directly. That's deliberate: uvx resolves and caches an isolated
    venv with the right heavy ML dependencies (torch, ctranslate2, ...) on
    demand, so this script itself stays dependency-free and installable via
    just python3 + nix.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    output_srt = str(Path(output_dir) / f"{stem_for(input_audio)}.srt")
    output_json = str(Path(output_dir) / f"{stem_for(input_audio)}.segments.json")
    wheel_path = rocm_ctranslate2_wheel_path()

    # Works around a ROCm LLVM codegen bug on RDNA4 (gfx1200/gfx1201) that otherwise
    # crashes CTranslate2 with "Memory access fault... Page not present" on GPU.
    # See https://github.com/OpenNMT/CTranslate2/issues/2021.
    env = dict(env)
    env.setdefault("CT2_CUDA_ALLOCATOR", "cub_caching")

    python_code = (
        "import json\n"
        "import sys\n"
        "\n"
        "from faster_whisper import WhisperModel\n"
        "\n"
        "def format_timestamp(seconds: float) -> str:\n"
        "    total_milliseconds = int(round(seconds * 1000))\n"
        "    hours, remainder = divmod(total_milliseconds, 3600000)\n"
        "    minutes, remainder = divmod(remainder, 60000)\n"
        "    seconds, milliseconds = divmod(remainder, 1000)\n"
        "    return f\"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}\"\n"
        "\n"
        "input_audio, output_srt, output_json, model_name, language, task, compute_type, device = sys.argv[1:9]\n"
        "if language == 'auto':\n"
        "    language = None\n"
        "\n"
        "model = WhisperModel(model_name, device=device, compute_type=compute_type)\n"
        "# Default VAD threshold (0.5) silently drops whole passages of quieter OP/ED\n"
        "# singing on full-length episodes. 0.2 still left an 18s gap confirmed by\n"
        "# isolated-clip testing; 0.1 plus a longer min_silence_duration_ms closes it\n"
        "# without reintroducing the old repetition-spam hallucination.\n"
        "segments, info = model.transcribe(\n"
        "    input_audio,\n"
        "    language=language,\n"
        "    task=task,\n"
        "    vad_filter=True,\n"
        "    vad_parameters={'threshold': 0.1, 'min_silence_duration_ms': 1500},\n"
        "    condition_on_previous_text=False,\n"
        ")\n"
        "\n"
        "raw_segments = []\n"
        "with open(output_srt, 'w', encoding='utf-8') as handle:\n"
        "    index = 0\n"
        "    for segment in segments:\n"
        "        text = segment.text.strip()\n"
        "        if not text:\n"
        "            continue\n"
        "        index += 1\n"
        "        handle.write(f'{index}\\n')\n"
        "        handle.write(\n"
        "            f'{format_timestamp(segment.start)} --> {format_timestamp(segment.end)}\\n'\n"
        "        )\n"
        "        handle.write(f'{text}\\n\\n')\n"
        "        raw_segments.append({'start': segment.start, 'end': segment.end, 'text': text})\n"
        "\n"
        "with open(output_json, 'w', encoding='utf-8') as handle:\n"
        "    json.dump({'language': info.language, 'segments': raw_segments}, handle)\n"
    )

    args = [
        "uvx",
        "--with",
        str(wheel_path),
        "--from",
        "faster-whisper",
        "python",
        "-c",
        python_code,
        input_audio,
        output_srt,
        output_json,
        model,
        language,
        task,
        compute_type,
        device,
    ]
    # task must stay ahead of compute_type/device in `args` above: the retry
    # loop below overwrites args[-2:] in place, so compute_type and device
    # need to keep being the last two entries.
    attempts = [(device, compute_type)]
    if device == "cuda":
        # A hard crash (e.g. an OOM on a huge model) still isn't caught by
        # CT2_CUDA_ALLOCATOR=cub_caching above, so keep this safety net.
        attempts.append(("cpu", "int8"))

    log(
        "Using uvx fallback for faster-whisper with ROCm CTranslate2 wheel: "
        f"{wheel_path.name}"
    )

    last_error: subprocess.CalledProcessError | None = None
    for attempt_device, attempt_compute in attempts:
        attempt_args = list(args)
        attempt_args[-2] = attempt_compute
        attempt_args[-1] = attempt_device

        try:
            if attempt_device != device:
                log("ROCm GPU transcription failed, retrying faster-whisper on CPU.")
            run_command(attempt_args, env=env)
            return output_srt, output_json
        except subprocess.CalledProcessError as exc:
            last_error = exc

    if last_error is not None:
        raise last_error
    return output_srt, output_json


def align_segments_whisperx(
    input_audio: str, segments_json: str, output_srt: str, env: dict[str, str], output_json: str | None = None
) -> bool:
    """Snaps faster-whisper's approximate segment timestamps to the actual
    audio via wav2vec2 forced alignment. Deliberately hardcoded to CPU:
    WhisperX's alignment model is plain PyTorch, not CTranslate2, so the ROCm
    workaround used elsewhere in this file doesn't apply to it, and getting a
    working ROCm-PyTorch build is exactly the fragile dependency chain this
    whole project avoids by using faster-whisper for the actual (expensive)
    decode. Alignment itself is cheap enough that CPU isn't a real bottleneck.
    """
    python_code = (
        "import json\n"
        "import sys\n"
        "\n"
        "import whisperx\n"
        "\n"
        "def format_timestamp(seconds: float) -> str:\n"
        "    total_milliseconds = int(round(seconds * 1000))\n"
        "    hours, remainder = divmod(total_milliseconds, 3600000)\n"
        "    minutes, remainder = divmod(remainder, 60000)\n"
        "    seconds, milliseconds = divmod(remainder, 1000)\n"
        "    return f\"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}\"\n"
        "\n"
        "input_audio, segments_json_path, output_srt, output_json = sys.argv[1:5]\n"
        "with open(segments_json_path, encoding='utf-8') as fh:\n"
        "    payload = json.load(fh)\n"
        "\n"
        "language = payload.get('language') or 'en'\n"
        "segments = payload.get('segments') or []\n"
        "if not segments:\n"
        "    raise SystemExit('No segments to align')\n"
        "\n"
        "device = 'cpu'\n"
        "audio = whisperx.load_audio(input_audio)\n"
        "model_a, metadata = whisperx.load_align_model(language_code=language, device=device)\n"
        "result = whisperx.align(segments, model_a, metadata, audio, device, return_char_alignments=False)\n"
        "\n"
        "aligned_segments = []\n"
        "with open(output_srt, 'w', encoding='utf-8') as handle:\n"
        "    index = 0\n"
        "    for segment in result['segments']:\n"
        "        text = (segment.get('text') or '').strip()\n"
        "        if not text:\n"
        "            continue\n"
        "        index += 1\n"
        "        handle.write(f'{index}\\n')\n"
        "        handle.write(\n"
        "            f\"{format_timestamp(segment['start'])} --> {format_timestamp(segment['end'])}\\n\"\n"
        "        )\n"
        "        handle.write(f'{text}\\n\\n')\n"
        "        aligned_segments.append({'start': segment['start'], 'end': segment['end'], 'text': text})\n"
        "\n"
        "if output_json != '-':\n"
        "    with open(output_json, 'w', encoding='utf-8') as handle:\n"
        "        json.dump({'language': language, 'segments': aligned_segments}, handle)\n"
    )

    args = [
        "uvx",
        "--from",
        "whisperx",
        "python",
        "-c",
        python_code,
        input_audio,
        segments_json,
        output_srt,
        output_json or "-",
    ]

    try:
        run_command(args, env=env)
    except subprocess.CalledProcessError as exc:
        log(f"WhisperX CPU alignment failed, keeping unaligned timestamps: {exc}")
        return False
    return Path(output_srt).is_file()


def translate_aligned_segments(
    input_audio: str,
    aligned_segments_json: str,
    output_srt: str,
    model: str,
    compute_type: str,
    device: str,
    env: dict[str, str],
) -> bool:
    """Whisper's translate task segments long audio unreliably (some cues end
    up spanning 30-50+ seconds). Aligned Japanese segments already have
    accurate, audio-locked timestamps (see align_segments_whisperx), so
    translate each segment's own short audio slice independently and keep
    its timing exactly as-is, instead of re-segmenting the whole episode
    during translation.
    """
    wheel_path = rocm_ctranslate2_wheel_path()
    env = dict(env)
    env.setdefault("CT2_CUDA_ALLOCATOR", "cub_caching")

    python_code = (
        "import json\n"
        "import sys\n"
        "\n"
        "from faster_whisper import WhisperModel\n"
        "from faster_whisper.audio import decode_audio\n"
        "\n"
        "def format_timestamp(seconds: float) -> str:\n"
        "    total_milliseconds = int(round(seconds * 1000))\n"
        "    hours, remainder = divmod(total_milliseconds, 3600000)\n"
        "    minutes, remainder = divmod(remainder, 60000)\n"
        "    seconds, milliseconds = divmod(remainder, 1000)\n"
        "    return f\"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}\"\n"
        "\n"
        "input_audio, segments_json_path, output_srt, model_name, compute_type, device = sys.argv[1:7]\n"
        "with open(segments_json_path, encoding='utf-8') as fh:\n"
        "    payload = json.load(fh)\n"
        "\n"
        "language = payload.get('language') or 'ja'\n"
        "segments = payload.get('segments') or []\n"
        "if not segments:\n"
        "    raise SystemExit('No segments to translate')\n"
        "\n"
        "model = WhisperModel(model_name, device=device, compute_type=compute_type)\n"
        "sample_rate = 16000\n"
        "# 0.3s of padding was still tight enough to occasionally cut a segment's\n"
        "# audio mid-word, which produced confidently wrong (not just imprecise)\n"
        "# translations even at temperature=0.0. 0.8s gives the model enough\n"
        "# leading/trailing context to place the utterance correctly.\n"
        "pad_seconds = 0.8\n"
        "audio = decode_audio(input_audio, sampling_rate=sample_rate)\n"
        "\n"
        "# Translating each short clip in isolation (see below) loses the surrounding\n"
        "# dialogue context a full-episode pass would have, which shows up as leftover\n"
        "# untranslated Japanese words, dropped honorifics, and name mix-ups (e.g.\n"
        "# 'Nami' rendered as 'Minami'). A static character-name glossary plus a\n"
        "# rolling window of the last couple of translated lines as initial_prompt\n"
        "# gives the model back some of that context without re-merging segments\n"
        "# (which is what broke timing in the first place).\n"
        "glossary = (\n"
        "    'Character names: Luffy, Zoro, Nami, Usopp, Sanji, Chopper, Robin, '\n"
        "    'Franky, Brook. Honorifics like -san and -kun are kept as-is.'\n"
        ")\n"
        "recent_lines = []\n"
        "\n"
        "index = 0\n"
        "with open(output_srt, 'w', encoding='utf-8') as handle:\n"
        "    for segment in segments:\n"
        "        start = segment['start']\n"
        "        end = segment['end']\n"
        "        pad_start = max(0.0, start - pad_seconds)\n"
        "        pad_end = end + pad_seconds\n"
        "        clip = audio[int(pad_start * sample_rate):int(pad_end * sample_rate)]\n"
        "        if clip.size == 0:\n"
        "            continue\n"
        "\n"
        "        prompt = ' '.join([glossary, *recent_lines])[-400:]\n"
        "        # temperature=0.0 forces greedy, deterministic decoding. Whisper's default\n"
        "        # temperature fallback schedule retries at higher, more random temperatures\n"
        "        # when it doesn't like its first attempt, and on short ambiguous clips like\n"
        "        # these that fallback is what produces unrelated, hallucinated sentences.\n"
        "        clip_segments, _ = model.transcribe(\n"
        "            clip,\n"
        "            language=language,\n"
        "            task='translate',\n"
        "            vad_filter=False,\n"
        "            condition_on_previous_text=False,\n"
        "            initial_prompt=prompt,\n"
        "            temperature=0.0,\n"
        "        )\n"
        "        text = ' '.join(s.text.strip() for s in clip_segments).strip()\n"
        "        if not text:\n"
        "            continue\n"
        "\n"
        "        recent_lines.append(text)\n"
        "        recent_lines = recent_lines[-2:]\n"
        "\n"
        "        index += 1\n"
        "        handle.write(f'{index}\\n')\n"
        "        handle.write(\n"
        "            f'{format_timestamp(start)} --> {format_timestamp(end)}\\n'\n"
        "        )\n"
        "        handle.write(f'{text}\\n\\n')\n"
    )

    args = [
        "uvx",
        "--with",
        str(wheel_path),
        "--from",
        "faster-whisper",
        "python",
        "-c",
        python_code,
        input_audio,
        aligned_segments_json,
        output_srt,
        model,
        compute_type,
        device,
    ]

    try:
        run_command(args, env=env)
    except subprocess.CalledProcessError as exc:
        log(f"Per-segment translation failed: {exc}")
        return False
    return Path(output_srt).is_file()


def resolve_whisper_runtime(device: str, compute_type: str, batch_size: str) -> tuple[str, str, str]:
    """Fills in "auto" (the CLI defaults) with sensible concrete values. Any
    value the caller passed explicitly is left untouched.
    """
    resolved_device = device
    resolved_compute = compute_type
    resolved_batch = batch_size

    if resolved_device == "auto":
        resolved_device = "cuda" if (rocm_available() or nvidia_available()) else "cpu"

    if resolved_compute == "auto":
        resolved_compute = "float16" if resolved_device == "cuda" else "int8"

    if resolved_batch == "auto":
        resolved_batch = "16" if resolved_device == "cuda" else "4"

    return resolved_device, resolved_compute, resolved_batch


def transcribe_audio(
    input_audio: str,
    output_dir: str,
    model: str,
    language: str,
    compute_type: str,
    device: str,
    batch_size: str,
    env: dict[str, str],
    force_device: str = "auto",
    task: str = "translate",
) -> str:
    """Top-level entry point the CLI calls. Picks between two completely
    different engines depending on the environment:
      - faster-whisper on ROCm/CUDA via uvx, when a GPU is available (the
        primary, fast path this whole module is built around).
      - WhisperX (also via uvx, or a real `whisperx` binary if present) as a
        pure-CPU fallback when no GPU is detected at all.
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    whisper_env = dict(env)

    rocm_detected = rocm_available()
    nvidia_detected = nvidia_available()
    log(
        "Accelerator detection: "
        f"rocm={'yes' if rocm_detected else 'no'} "
        f"nvidia={'yes' if nvidia_detected else 'no'}"
    )

    resolved_device, resolved_compute, resolved_batch = resolve_whisper_runtime(device, compute_type, batch_size)
    if rocm_detected and command_exists("uvx") and force_device != "cpu":
        log(
            f"Transcription runtime: device={resolved_device} compute_type={resolved_compute} "
            f"batch_size={resolved_batch}"
        )
        log(f"Transcribing {Path(input_audio).name} with faster-whisper model {model}")
        # Always transcribe in the source language first: Whisper's own translate-task
        # segmentation is unreliable over long audio (cues can span 30-50+ seconds).
        # Forced alignment matches transcript words to audio phonemes in the same
        # language, so it only works against a same-language transcript. Any
        # requested translation happens afterward, per-segment, preserving those
        # precise timestamps instead of re-segmenting the whole episode.
        raw_srt, segments_json = transcribe_audio_faster_whisper(
            input_audio,
            output_dir,
            model,
            language,
            resolved_compute,
            resolved_device,
            resolved_batch,
            whisper_env,
            "transcribe",
        )

        source_srt, source_json = raw_srt, segments_json
        if command_exists("uvx"):
            aligned_srt = str(Path(output_dir) / f"{stem_for(input_audio)}.aligned.srt")
            aligned_json = str(Path(output_dir) / f"{stem_for(input_audio)}.aligned.json")
            log("Refining segment timestamps with WhisperX forced alignment (CPU)")
            if align_segments_whisperx(input_audio, segments_json, aligned_srt, whisper_env, aligned_json):
                log(f"Alignment refinement applied: {Path(aligned_srt).name}")
                source_srt, source_json = aligned_srt, aligned_json
            else:
                log("Alignment refinement unavailable, using unaligned faster-whisper timestamps.")

        if task == "translate":
            translated_srt = str(Path(output_dir) / f"{stem_for(input_audio)}.translated.srt")
            log("Translating aligned segments to English (per-segment, preserves timing)")
            if translate_aligned_segments(
                input_audio, source_json, translated_srt, model, resolved_compute, resolved_device, whisper_env
            ):
                log(f"Translation applied: {Path(translated_srt).name}")
                return translated_srt
            log("Per-segment translation failed. Falling back to source-language transcript.")

        return source_srt

    if command_exists("whisperx"):
        whisperx_cmd = ["whisperx"]
    elif command_exists("uvx"):
        if resolved_device == "cpu" and device == "auto":
            resolved_device = "cpu"
            if compute_type == "auto":
                resolved_compute = "int8"
            if batch_size == "auto":
                resolved_batch = "4"
        whisperx_cmd = ["uvx", "--from", "whisperx", "whisperx"]
    else:
        die("Missing required command: whisperx (or uvx fallback)")

    log(
        f"WhisperX runtime: device={resolved_device} compute_type={resolved_compute} "
        f"batch_size={resolved_batch}"
    )
    log(f"Transcribing {Path(input_audio).name} with WhisperX model {model}")

    whisper_extra_args: list[str] = ["--task", task]
    if rocm_detected:
        whisper_extra_args.extend(["--vad_method", "silero"])
    if task == "translate":
        # WhisperX's own alignment step has the same language-mismatch problem as
        # our CPU alignment pass: it can't match English translated text to
        # source-language audio phonemes.
        whisper_extra_args.append("--no_align")

    batch_candidates = whisperx_batch_candidates(resolved_device, batch_size, resolved_batch)
    if len(batch_candidates) > 1:
        log(f"Auto batch tuning enabled. Candidates: {', '.join(batch_candidates)}")

    last_error: subprocess.CalledProcessError | None = None
    successful_batch: str | None = None

    for candidate_batch in batch_candidates:
        try:
            log(f"WhisperX attempt with batch_size={candidate_batch}")
            run_command(
                whisperx_cmd
                + [
                    input_audio,
                    "--model",
                    model,
                    "--language",
                    language,
                    "--output_dir",
                    output_dir,
                    "--output_format",
                    "srt",
                    "--compute_type",
                    resolved_compute,
                    "--device",
                    resolved_device,
                    "--batch_size",
                    candidate_batch,
                ]
                + whisper_extra_args,
                env=whisper_env,
            )
            successful_batch = candidate_batch
            break
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if candidate_batch == batch_candidates[-1]:
                break
            log(f"WhisperX failed with batch_size={candidate_batch}, retrying with a smaller batch.")

    if successful_batch is None:
        if rocm_detected and resolved_device == "cuda" and force_device != "cuda":
            log("ROCm GPU transcription failed, switching to ROCm-accelerated CPU mode.")
            resolved_device = "cpu"
            resolved_compute = "int8"
            cpu_batch_candidates = whisperx_batch_candidates("cpu", "auto", "4")
            last_error = None

            for candidate_batch in cpu_batch_candidates:
                try:
                    log(f"WhisperX ROCm-accelerated CPU attempt with batch_size={candidate_batch}")
                    run_command(
                        whisperx_cmd
                        + [
                            input_audio,
                            "--model",
                            model,
                            "--language",
                            language,
                            "--output_dir",
                            output_dir,
                            "--output_format",
                            "srt",
                            "--compute_type",
                            resolved_compute,
                            "--device",
                            resolved_device,
                            "--batch_size",
                            candidate_batch,
                        ]
                        + whisper_extra_args,
                        env=whisper_env,
                    )
                    successful_batch = candidate_batch
                    break
                except subprocess.CalledProcessError as exc:
                    last_error = exc
                    if candidate_batch == cpu_batch_candidates[-1]:
                        break
                    log(f"CPU fallback failed with batch_size={candidate_batch}, retrying with a smaller batch.")

        if successful_batch is None:
            if last_error is not None:
                raise last_error
            die("WhisperX failed before a transcription batch could be selected")

    if successful_batch != resolved_batch:
        log(f"Auto batch tuning selected stable batch_size={successful_batch}")

    return str(Path(output_dir) / f"{stem_for(input_audio)}.srt")
