"""Picks the right audio track out of a Dual Audio MKV and extracts it to a wav
faster-whisper can consume. Source files carry both an English dub track and
a Japanese track. Which one we want depends on --language (default: ja, since
the pipeline transcribes+translates the Japanese dub, not the English one).
"""
from __future__ import annotations

import json
from pathlib import Path

from lib.common import command_exists, die, log, run_command

AUDIO_LANGUAGE_ALIASES = {
    "en": {"tags": {"eng", "en", "english"}, "title_keywords": {"dub", "english"}},
    "ja": {"tags": {"jpn", "ja", "japanese"}, "title_keywords": {"japanese", "jpn", "original"}},
}


def normalize_target_language(language: str) -> str:
    """Collapses whatever a user types for --language ("ja", "jpn", "Japanese",
    "auto", ...) down to the two keys AUDIO_LANGUAGE_ALIASES actually knows,
    defaulting to Japanese since that's this project's primary source.
    """
    normalized = language.strip().lower()
    if normalized in {"en", "eng", "english"}:
        return "en"
    return "ja"


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


def select_audio_stream_index(input_path: str, env: dict[str, str], target_language: str = "ja") -> int:
    streams = ffprobe_audio_streams(input_path, env)
    if not streams:
        die("No audio streams found")

    aliases = AUDIO_LANGUAGE_ALIASES.get(target_language, AUDIO_LANGUAGE_ALIASES["ja"])

    # Score rather than filter: some source files only tag language OR title,
    # not both, so we want the best match available instead of requiring both
    # signals to agree.
    def score(stream: dict) -> tuple[int, int]:
        tags = stream.get("tags") or {}
        language = str(tags.get("language") or tags.get("LANGUAGE") or "").strip().lower()
        title = str(tags.get("title") or tags.get("TITLE") or "").strip().lower()

        value = 0
        if language in aliases["tags"]:
            value += 20
        if any(keyword in title for keyword in aliases["title_keywords"]):
            value += 50

        # Lower stream index as tiebreaker.
        idx = int(stream.get("index", 10**9))
        return value, -idx

    best = max(streams, key=score)
    return int(best["index"])


def extract_audio(
    input_video: str, output_audio: str, track_index: int | None, env: dict[str, str], target_language: str = "ja"
) -> None:
    if not command_exists("ffmpeg"):
        die("Missing required command: ffmpeg")
    if not command_exists("ffprobe"):
        die("Missing required command: ffprobe")

    chosen_index = (
        track_index if track_index is not None else select_audio_stream_index(input_video, env, target_language)
    )
    Path(output_audio).parent.mkdir(parents=True, exist_ok=True)

    log(f"Extracting audio stream {chosen_index} from {Path(input_video).name}")
    # 16kHz mono PCM is exactly what Whisper's feature extractor expects. Doing
    # the resample/downmix here means faster-whisper never has to do it itself.
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
