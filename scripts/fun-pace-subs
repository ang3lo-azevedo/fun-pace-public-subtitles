#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_STYLE_REFERENCE = (PROJECT_ROOT / "input" / "alabasta 18 en.ass").resolve()
DEFAULT_TERMS_PLACEHOLDER = "@DEFAULT_TERMS_FILE@"
DEFAULT_LD_LIBRARY_PATH_PLACEHOLDER = "@DEFAULT_LD_LIBRARY_PATH@"
CTRANSLATE2_ROCM_RELEASE_URL = "https://github.com/OpenNMT/CTranslate2/releases/download/v4.7.1/rocm-python-wheels-Linux.zip"
CTRANSLATE2_ROCM_CACHE_DIR = Path.home() / ".cache" / "fun-pace-subs" / "ctranslate2-rocm"


def die(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def log(message: str) -> None:
    print(message, file=sys.stderr)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def resolve_default_terms_file() -> str | None:
    default_terms = os.environ.get("FUN_PACE_DEFAULT_TERMS_FILE", DEFAULT_TERMS_PLACEHOLDER)
    if default_terms == DEFAULT_TERMS_PLACEHOLDER:
        default_terms = str((SCRIPT_DIR / "../data/one-piece-terms.tsv").resolve())
    return default_terms if Path(default_terms).is_file() else None


def prepare_subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    default_ld_path = env.get("FUN_PACE_DEFAULT_LD_LIBRARY_PATH", DEFAULT_LD_LIBRARY_PATH_PLACEHOLDER)
    if default_ld_path == DEFAULT_LD_LIBRARY_PATH_PLACEHOLDER:
        default_ld_path = ""
    if default_ld_path:
        current = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{default_ld_path}:{current}" if current else default_ld_path
    return env


def run_command(args: list[str], *, env: dict[str, str] | None = None, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=True,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def stem_for(path: str) -> str:
    return Path(path).stem


def rocm_available() -> bool:
    # ROCm commonly exposes /dev/kfd; rocminfo is a secondary signal.
    return Path("/dev/kfd").exists() or command_exists("rocminfo")


def nvidia_available() -> bool:
    return command_exists("nvidia-smi")


def python_wheel_tag() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def rocm_ctranslate2_wheel_path() -> Path:
    wheel_cache_dir = CTRANSLATE2_ROCM_CACHE_DIR / python_wheel_tag()
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
) -> str:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    output_srt = str(Path(output_dir) / f"{stem_for(input_audio)}.srt")
    wheel_path = rocm_ctranslate2_wheel_path()

    python_code = (
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
        "input_audio, output_srt, model_name, language, compute_type, device = sys.argv[1:7]\n"
        "if language == 'auto':\n"
        "    language = None\n"
        "\n"
        "model = WhisperModel(model_name, device=device, compute_type=compute_type)\n"
        "segments, info = model.transcribe(\n"
        "    input_audio,\n"
        "    language=language,\n"
        ")\n"
        "\n"
        "with open(output_srt, 'w', encoding='utf-8') as handle:\n"
        "    for index, segment in enumerate(segments, start=1):\n"
        "        text = segment.text.strip()\n"
        "        if not text:\n"
        "            continue\n"
        "        handle.write(f'{index}\\n')\n"
        "        handle.write(\n"
        "            f'{format_timestamp(segment.start)} --> {format_timestamp(segment.end)}\\n'\n"
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
        output_srt,
        model,
        language,
        compute_type,
        device,
    ]
    log(
        "Using uvx fallback for faster-whisper with ROCm CTranslate2 wheel: "
        f"{wheel_path.name}"
    )
    run_command(args, env=env)
    return output_srt


def resolve_whisper_runtime(device: str, compute_type: str, batch_size: str) -> tuple[str, str, str]:
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


def ffprobe_audio_streams(input_path: str, env: dict[str, str]) -> list[dict]:
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index:stream_tags=language,title",
            "-of",
            "json",
            input_path,
        ],
        env=env,
        capture=True,
    )
    payload = json.loads(result.stdout)
    return payload.get("streams", [])


def select_audio_stream_index(input_path: str, env: dict[str, str]) -> int:
    streams = ffprobe_audio_streams(input_path, env)
    if not streams:
        die("No audio streams found")

    def score(stream: dict) -> tuple[int, int]:
        tags = stream.get("tags") or {}
        language = str(tags.get("language") or tags.get("LANGUAGE") or "").strip().lower()
        title = str(tags.get("title") or tags.get("TITLE") or "").strip().lower()

        value = 0
        if language in {"eng", "en", "english"}:
            value += 20
        if "dub" in title:
            value += 50
        if "english" in title:
            value += 10

        # Lower stream index as tiebreaker.
        idx = int(stream.get("index", 10**9))
        return value, -idx

    best = max(streams, key=score)
    return int(best["index"])


def extract_audio(input_video: str, output_audio: str, track_index: int | None, env: dict[str, str]) -> None:
    if not command_exists("ffmpeg"):
        die("Missing required command: ffmpeg")
    if not command_exists("ffprobe"):
        die("Missing required command: ffprobe")

    chosen_index = track_index if track_index is not None else select_audio_stream_index(input_video, env)
    Path(output_audio).parent.mkdir(parents=True, exist_ok=True)

    log(f"Extracting audio stream {chosen_index} from {Path(input_video).name}")
    run_command(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            input_video,
            "-map",
            f"0:{chosen_index}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            output_audio,
        ],
        env=env,
    )


def normalize_srt(input_srt: str, output_srt: str, terms_file: str | None, env: dict[str, str]) -> None:
    args = [
        sys.executable,
        str(SCRIPT_DIR / "normalize_srt.py"),
        "--input",
        input_srt,
        "--output",
        output_srt,
    ]
    if terms_file:
        args.extend(["--terms-file", terms_file])
    run_command(args, env=env)


def style_srt(input_srt: str, output_srt: str, terms_file: str | None, env: dict[str, str]) -> None:
    args = [
        sys.executable,
        str(SCRIPT_DIR / "style_srt.py"),
        "--input",
        input_srt,
        "--output",
        output_srt,
    ]
    if terms_file:
        args.extend(["--terms-file", terms_file])
    run_command(args, env=env)


def convert_srt_to_ass(input_srt: str, output_ass: str, env: dict[str, str], style_from_ass: str | None = None) -> None:
    args = [
        sys.executable,
        str(SCRIPT_DIR / "srt_to_ass.py"),
        "--input",
        input_srt,
        "--output",
        output_ass,
    ]
    if style_from_ass:
        args.extend(["--style-from-ass", style_from_ass])
    run_command(args, env=env)


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
) -> str:
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
        if resolved_device == "cpu" and device == "auto":
            resolved_device = "cuda"
            if compute_type == "auto":
                resolved_compute = "float16"
            if batch_size == "auto":
                resolved_batch = "16"

        log(
            f"Transcription runtime: device={resolved_device} compute_type={resolved_compute} "
            f"batch_size={resolved_batch}"
        )
        log(f"Transcribing {Path(input_audio).name} with faster-whisper model {model}")
        return transcribe_audio_faster_whisper(
            input_audio,
            output_dir,
            model,
            language,
            resolved_compute,
            resolved_device,
            resolved_batch,
            whisper_env,
        )

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

    whisper_extra_args: list[str] = []
    if rocm_detected:
        whisper_extra_args.extend(["--vad_method", "silero"])

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
            log(f"WhisperX failed with batch_size={candidate_batch}; retrying with a smaller batch.")

    if successful_batch is None:
        if rocm_detected and resolved_device == "cuda" and force_device != "cuda":
            log("ROCm GPU transcription failed; switching to ROCm-accelerated CPU mode.")
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
                    log(f"CPU fallback failed with batch_size={candidate_batch}; retrying with a smaller batch.")

        if successful_batch is None:
            if last_error is not None:
                raise last_error
            die("WhisperX failed before a transcription batch could be selected")

    if successful_batch != resolved_batch:
        log(f"Auto batch tuning selected stable batch_size={successful_batch}")

    return str(Path(output_dir) / f"{stem_for(input_audio)}.srt")


def ffprobe_subtitle_streams(input_video: str, env: dict[str, str]) -> list[dict]:
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "s",
            "-show_entries",
            "stream=index,codec_name:stream_tags=language,title",
            "-of",
            "json",
            input_video,
        ],
        env=env,
        capture=True,
    )
    payload = json.loads(result.stdout)
    return payload.get("streams", [])


def choose_ass_subtitle_stream(input_video: str, env: dict[str, str]) -> int:
    streams = ffprobe_subtitle_streams(input_video, env)
    if not streams:
        die("No subtitle streams found in MKV")

    def score(stream: dict) -> tuple[int, int]:
        codec_name = str(stream.get("codec_name") or "").lower()
        tags = stream.get("tags") or {}
        language = str(tags.get("language") or tags.get("LANGUAGE") or "").strip().lower()
        title = str(tags.get("title") or tags.get("TITLE") or "").strip().lower()

        value = 0
        if codec_name in {"ass", "ssa"}:
            value += 100
        if language in {"eng", "en", "english"}:
            value += 20
        if "sign" in title:
            value -= 10

        idx = int(stream.get("index", 10**9))
        return value, -idx

    best = max(streams, key=score)
    return int(best["index"])


def extract_ass_from_mkv(input_video: str, output_ass: str, stream_index: int | None, env: dict[str, str]) -> None:
    if not command_exists("ffmpeg"):
        die("Missing required command: ffmpeg")
    if not command_exists("ffprobe"):
        die("Missing required command: ffprobe")

    chosen_index = stream_index if stream_index is not None else choose_ass_subtitle_stream(input_video, env)
    Path(output_ass).parent.mkdir(parents=True, exist_ok=True)

    log(f"Extracting subtitle stream {chosen_index} from {Path(input_video).name} to ASS")
    run_command(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            input_video,
            "-map",
            f"0:{chosen_index}",
            "-c:s",
            "ass",
            output_ass,
        ],
        env=env,
    )


def maybe_extract_reference_ass(input_video: str, temp_dir: str, env: dict[str, str]) -> str | None:
    reference_path = str(Path(temp_dir) / "style-reference.ass")
    try:
        extract_ass_from_mkv(input_video, reference_path, None, env)
    except (Exception, SystemExit):
        return None
    return reference_path if Path(reference_path).is_file() else None


def resolve_style_reference_ass(
    input_video: str,
    temp_dir: str,
    env: dict[str, str],
    explicit_reference: str | None,
) -> str | None:
    if explicit_reference:
        ref_path = Path(explicit_reference).expanduser().resolve()
        if ref_path.is_file():
            return str(ref_path)
        die(f"Style reference ASS not found: {ref_path}")

    if DEFAULT_STYLE_REFERENCE.is_file():
        return str(DEFAULT_STYLE_REFERENCE)

    return maybe_extract_reference_ass(input_video, temp_dir, env)


def collect_font_attachments(fonts_dir: Path) -> list[str]:
    if not fonts_dir.is_dir():
        return []

    attachment_args: list[str] = []
    font_exts = {".ttf", ".otf", ".ttc", ".otc"}
    for font_file in sorted(fonts_dir.iterdir()):
        if font_file.is_file() and font_file.suffix.lower() in font_exts:
            attachment_args.extend(["--attach-file", str(font_file)])
    return attachment_args


def mux_subtitles(input_video: str, input_subs: str, output_mkv: str, env: dict[str, str], fonts_dir: Path | None = None) -> None:
    if not command_exists("mkvmerge"):
        die("Missing required command: mkvmerge")

    attachment_args: list[str] = []
    if fonts_dir is not None:
        attachment_args = collect_font_attachments(fonts_dir)
        if attachment_args:
            attached_count = len(attachment_args) // 2
            log(f"Attaching {attached_count} font files from {fonts_dir}")

            Path(output_mkv).parent.mkdir(parents=True, exist_ok=True)
    log(f"Muxing subtitles into {Path(output_mkv).name}")
    run_command(
        [
            "mkvmerge",
            "-o",
            output_mkv,
            input_video,
            "--language",
            "0:eng",
            "--track-name",
            "0:English AI subtitles",
            "--default-track-flag",
            "0:yes",
            input_subs,
        ]
        + attachment_args,
        env=env,
    )


def create_media_symlink(link_path: Path, target_path: Path) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()

    relative_target = os.path.relpath(target_path, start=link_path.parent)
    link_path.symlink_to(relative_target)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fun-pace-subs")
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run")
    run_cmd.add_argument("input_video")
    run_cmd.add_argument("--terms-file")
    run_cmd.add_argument("--model", default="large-v3")
    run_cmd.add_argument("--compute-type", default="auto")
    run_cmd.add_argument("--device", default="auto")
    run_cmd.add_argument("--batch-size", default="auto")
    run_cmd.add_argument("--language", default="en")
    run_cmd.add_argument("--track-index", type=int)
    run_cmd.add_argument("--output-dir")
    run_cmd.add_argument("--style-reference-ass")
    run_cmd.add_argument("--no-mux", action="store_true")
    run_cmd.add_argument("--mux", action="store_true")
    run_cmd.add_argument("--keep-audio", action="store_true")

    extract_cmd = sub.add_parser("extract")
    extract_cmd.add_argument("input_video")
    extract_cmd.add_argument("output_audio", nargs="?")
    extract_cmd.add_argument("--track-index", type=int)

    transcribe_cmd = sub.add_parser("transcribe")
    transcribe_cmd.add_argument("input_audio")
    transcribe_cmd.add_argument("output_dir", nargs="?")
    transcribe_cmd.add_argument("--model", default="large-v3")
    transcribe_cmd.add_argument("--compute-type", default="auto")
    transcribe_cmd.add_argument("--device", default="auto")
    transcribe_cmd.add_argument("--batch-size", default="auto")
    transcribe_cmd.add_argument("--language", default="en")
    transcribe_cmd.add_argument("--force-device", choices=["auto", "cuda", "cpu"], default="auto", help="Skip CPU fallback and fail if device unavailable")

    normalize_cmd = sub.add_parser("normalize")
    normalize_cmd.add_argument("input_srt")
    normalize_cmd.add_argument("output_srt", nargs="?")
    normalize_cmd.add_argument("--terms-file")

    style_cmd = sub.add_parser("style")
    style_cmd.add_argument("input_srt")
    style_cmd.add_argument("output_srt", nargs="?")
    style_cmd.add_argument("--terms-file")

    assify_cmd = sub.add_parser("assify")
    assify_cmd.add_argument("input_srt")
    assify_cmd.add_argument("output_ass", nargs="?")
    assify_cmd.add_argument("--style-from-ass")

    extract_ass_cmd = sub.add_parser("extract-ass")
    extract_ass_cmd.add_argument("input_video")
    extract_ass_cmd.add_argument("output_ass", nargs="?")
    extract_ass_cmd.add_argument("--stream-index", type=int)

    mux_cmd = sub.add_parser("mux")
    mux_cmd.add_argument("input_video")
    mux_cmd.add_argument("input_subs")
    mux_cmd.add_argument("output_mkv", nargs="?")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    env = prepare_subprocess_env()

    terms_file = getattr(args, "terms_file", None) or resolve_default_terms_file()
    if terms_file and not Path(terms_file).is_file():
        die(f"Terms file not found: {terms_file}")

    if args.command == "run":
        input_video = args.input_video
        input_path = Path(input_video)
        mux_base_name = re.sub(r"\[Subs Missing\]", "[AI Subs]", input_path.stem)

        # Keep generated ASS files in a single subtitles root without per-episode folders.
        subtitle_root = Path(args.output_dir).resolve() if args.output_dir else (PROJECT_ROOT / "output" / "subtitles").resolve()
        subtitle_root.mkdir(parents=True, exist_ok=True)

        base_name = stem_for(input_video)
        ass_output_path = subtitle_root / f"{mux_base_name}.ass"
        ass_output = str(ass_output_path)

        media_symlink_path = input_path.with_suffix(".ass")
        muxed_output = str((PROJECT_ROOT / "output" / f"{mux_base_name}.mkv").resolve())
        fonts_dir = PROJECT_ROOT / "input" / "fonts"

        with tempfile.TemporaryDirectory() as tmp:
            extracted_audio = str(Path(tmp) / f"{base_name}.wav")
            normalized_srt = str(Path(tmp) / f"{base_name}.styled.srt")
            extract_audio(input_video, extracted_audio, args.track_index, env)

            raw_srt = transcribe_audio(
                extracted_audio,
                tmp,
                args.model,
                args.language,
                args.compute_type,
                args.device,
                args.batch_size,
                env,
            )

            normalize_srt(raw_srt, normalized_srt, terms_file, env)
            style_srt(normalized_srt, normalized_srt, terms_file, env)

            style_reference_ass = resolve_style_reference_ass(
                input_video,
                tmp,
                env,
                args.style_reference_ass,
            )
            if style_reference_ass:
                log(f"Using MKV subtitle style reference: {Path(style_reference_ass).name}")

            convert_srt_to_ass(normalized_srt, ass_output, env, style_reference_ass)

            if args.keep_audio:
                shutil.copy2(extracted_audio, subtitle_root / f"{mux_base_name}.wav")

        log(f"Wrote ASS subtitles to {ass_output}")

        create_media_symlink(media_symlink_path, ass_output_path.resolve())
        log(f"Linked media subtitle path {media_symlink_path} -> {ass_output_path}")

        should_mux = not args.no_mux
        if should_mux:
            mux_subtitles(input_video, ass_output, muxed_output, env, fonts_dir=fonts_dir)
            log(f"Wrote muxed MKV to {muxed_output}")
        return

    if args.command == "extract":
        output_audio = args.output_audio or str(Path(args.input_video).with_suffix(".wav"))
        extract_audio(args.input_video, output_audio, args.track_index, env)
        log(f"Wrote extracted audio to {output_audio}")
        return

    if args.command == "transcribe":
        output_dir = args.output_dir or str(Path(args.input_audio).resolve().parent)
        raw_srt = transcribe_audio(
            args.input_audio,
            output_dir,
            args.model,
            args.language,
            args.compute_type,
            args.device,
            args.batch_size,
            env,
            force_device=args.force_device,
        )
        log(f"Wrote transcribed subtitles to {raw_srt}")
        return

    if args.command == "normalize":
        output_srt = args.output_srt or str(Path(args.input_srt).with_name(f"{stem_for(args.input_srt)}.normalized.srt"))
        normalize_srt(args.input_srt, output_srt, terms_file, env)
        log(f"Wrote normalized subtitles to {output_srt}")
        return

    if args.command == "style":
        output_srt = args.output_srt or str(Path(args.input_srt).with_name(f"{stem_for(args.input_srt)}.styled.srt"))
        style_srt(args.input_srt, output_srt, terms_file, env)
        log(f"Wrote styled subtitles to {output_srt}")
        return

    if args.command == "assify":
        output_ass = args.output_ass or str(Path(args.input_srt).with_suffix(".ass"))
        convert_srt_to_ass(args.input_srt, output_ass, env, args.style_from_ass)
        log(f"Wrote ASS subtitles to {output_ass}")
        return

    if args.command == "extract-ass":
        output_ass = args.output_ass or str(Path(args.input_video).with_suffix(".source.ass"))
        extract_ass_from_mkv(args.input_video, output_ass, args.stream_index, env)
        log(f"Wrote extracted ASS subtitles to {output_ass}")
        return

    if args.command == "mux":
        mux_base_name = re.sub(r"\[Subs Missing\]", "[AI Subs]", stem_for(args.input_video))
        output_mkv = args.output_mkv or str((PROJECT_ROOT / "output" / f"{mux_base_name}.mkv").resolve())
        mux_subtitles(args.input_video, args.input_subs, output_mkv, env, fonts_dir=PROJECT_ROOT / "input" / "fonts")
        log(f"Wrote muxed MKV to {output_mkv}")
        return


if __name__ == "__main__":
    main()
