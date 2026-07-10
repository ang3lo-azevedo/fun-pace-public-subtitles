"""The back end of the pipeline: pulling ASS styling out of a source MKV (used
as a style reference when none is given explicitly) and muxing the final
generated subtitles + fonts into a new MKV.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from lib.common import command_exists, die, log, run_command


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
    """Last-resort fallback: if the source MKV happens to ship its own ASS
    track (e.g. a prior release with subs), pull its styling instead of
    falling all the way back to srt_to_ass.py's hardcoded default styles.
    Failure here just means "no reference available", not a fatal error.
    """
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
    default_reference: Path,
) -> str | None:
    """Precedence: explicit --style-reference-ass > the project's default
    reference file > whatever ASS styling (if any) is already in the source
    MKV > no reference at all (srt_to_ass.py falls back to built-in styles).
    """
    if explicit_reference:
        ref_path = Path(explicit_reference).expanduser().resolve()
        if ref_path.is_file():
            return str(ref_path)
        die(f"Style reference ASS not found: {ref_path}")

    if default_reference.is_file():
        return str(default_reference)

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
    # "0:eng" tags the subtitle TRACK's language, not the source audio's -
    # the generated text is always English regardless of whether it was
    # transcribed or translated from Japanese.
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
    """Most media players auto-load a subtitle file that sits next to the video
    with a matching name. The generated .ass lives under output/episodes/,
    not next to the source video in input/episodes/, so this symlink bridges
    that gap without duplicating the file. A relative link keeps it valid if
    the whole project directory gets moved.
    """
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()

    relative_target = os.path.relpath(target_path, start=link_path.parent)
    link_path.symlink_to(relative_target)
