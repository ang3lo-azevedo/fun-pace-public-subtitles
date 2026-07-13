"""Retimes an existing, already-translated subtitle file for an uncut source
episode onto a trimmed fan-edit cut of that same episode, instead of
generating new subtitles from scratch. A cut edit keeps the exact same
underlying audio for every scene it retains (just removes the rest), so
cross-correlating the cut's audio against the source's finds precisely which
source time ranges survived and in what order, without needing any existing
edit-decision-list. Confirmed directly on real files before writing this:
distinct, stable offset blocks with clear jumps at cut boundaries, matching
the "Saved" runtime difference expected between the two.

The subtitle source itself is whatever English subtitle stream is already
embedded in the source episode's own MKV - no separate subtitle file needed.
A DVD-sourced release typically ships several ASS styles beyond plain
dialogue (karaoke-timed opening/ending lyrics, signs, credits); the only ones
worth keeping here are dialogue and any plain (non-karaoke) translated lyric
line, so styles are filtered by whether they actually carry per-syllable
karaoke timing tags, not by name (names vary release to release, karaoke
tags don't).
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from lib.audio import extract_audio
from lib.common import die, log, run_command
from lib.muxing import choose_ass_subtitle_stream, extract_ass_from_mkv
from lib.srt_to_ass import resolve_ass_header, resolve_dialogue_style, resolve_music_style

# Smaller windows find a cut boundary more precisely but are noisier on quiet
# passages; larger windows are more robust but blur the exact boundary.
ALIGNMENT_WINDOW_SECONDS = 15
ALIGNMENT_STEP_SECONDS = 10
# Consecutive windows within this offset tolerance count as the same kept
# scene rather than a new cut boundary - wider than a single frame to absorb
# the cross-correlation's own jitter (confirmed a fraction of a second of
# wobble between windows that are really part of one block).
BLOCK_OFFSET_TOLERANCE_SECONDS = 2.0
MIN_BLOCK_SECONDS = 5.0
# A real caption is never displayed for less than this. Anything shorter is
# a symptom, not a style: a letter-by-letter reveal effect (e.g. for an
# on-screen "Sign" translation) is built from dozens of separate lines each
# lasting a tiny fraction of a second - confirmed directly, one release had
# 71 such lines for a single sign. Checked against the *source* duration,
# before any cut-boundary clipping, so a legitimate cue that a cut happens
# to trim short isn't mistaken for one of these.
MIN_CUE_DURATION_SECONDS = 0.15
MIN_MATCH_SCORE = 0.02
MAX_GAP_SECONDS = 20.0
# Source lines known to be from content cut from the Fun Pace edit.
_BLOCKED_TEXTS = [
    "TaboTabo bacteria",
    "After showing the sign",
]


def extract_alignment_audio(input_video: str, output_wav: str, env: dict[str, str], track_index: int | None = None) -> None:
    extract_audio(input_video, output_wav, track_index, env, target_language="ja")


def _windowed_matches(source_wav: str, cut_wav: str, env: dict[str, str]) -> list[tuple[float, float, float]]:
    """Runs the actual cross-correlation search as a `uvx --from scipy`
    subprocess (numpy/scipy are heavy, isolated dependencies, same pattern as
    faster-whisper/whisperx elsewhere in this project). Returns raw
    (cut_time, source_time, score) triples, one per window; grouping them
    into contiguous blocks happens back in plain Python, see resolve_edl().
    """
    python_code = (
        "import json\n"
        "import sys\n"
        "\n"
        "import numpy as np\n"
        "from scipy.io import wavfile\n"
        "from scipy.signal import fftconvolve\n"
        "\n"
        "source_wav, cut_wav, window_seconds, step_seconds, min_score = sys.argv[1:6]\n"
        "window_seconds = float(window_seconds)\n"
        "step_seconds = float(step_seconds)\n"
        "min_score = float(min_score)\n"
        "\n"
        "def load_mono(path):\n"
        "    sr, data = wavfile.read(path)\n"
        "    if data.dtype != np.float32:\n"
        "        data = data.astype(np.float32) / 32768.0\n"
        "    return sr, data\n"
        "\n"
        "sr_src, src = load_mono(source_wav)\n"
        "sr_cut, cut = load_mono(cut_wav)\n"
        "assert sr_src == sr_cut\n"
        "sr = sr_src\n"
        "window = int(window_seconds * sr)\n"
        "step = int(step_seconds * sr)\n"
        "\n"
        "src_norm = src - src.mean()\n"
        "src_energy = np.sum(src_norm ** 2)\n"
        "\n"
        "matches = []\n"
        "for start in range(0, max(len(cut) - window, 0), step):\n"
        "    clip = cut[start:start + window]\n"
        "    clip_norm = clip - clip.mean()\n"
        "    if np.abs(clip_norm).max() < 1e-4:\n"
        "        continue\n"
        "    corr = fftconvolve(src_norm, clip_norm[::-1], mode='valid')\n"
        "    norm = np.sqrt(np.sum(clip_norm ** 2) * src_energy) + 1e-9\n"
        "    best_idx = int(np.argmax(corr))\n"
        "    best_score = float(corr[best_idx] / norm)\n"
        "    if best_score < min_score:\n"
        "        continue\n"
        "    matches.append([start / sr, best_idx / sr, best_score])\n"
        "\n"
        "print(json.dumps(matches))\n"
    )

    args = [
        "uvx",
        "--from",
        "scipy",
        "python",
        "-c",
        python_code,
        source_wav,
        cut_wav,
        str(ALIGNMENT_WINDOW_SECONDS),
        str(ALIGNMENT_STEP_SECONDS),
        str(MIN_MATCH_SCORE),
    ]
    result = run_command(args, env=env, capture=True)
    return [tuple(row) for row in json.loads(result.stdout)]


def resolve_edl(matches: list[tuple[float, float, float]]) -> list[dict]:
    """Groups per-window matches into contiguous "kept scene" blocks by
    offset (source_time - cut_time). A block boundary is wherever the offset
    jumps by more than BLOCK_OFFSET_TOLERANCE_SECONDS between consecutive
    windows - that jump is the cut. Each block's own start/end come from its
    first and last window, extended by half a step on each side so the
    boundary sits between windows rather than exactly on one.
    """
    if not matches:
        return []

    groups: list[list[tuple[float, float, float]]] = []
    for cut_time, source_time, score in sorted(matches):
        offset = source_time - cut_time
        if groups:
            last_offset = groups[-1][-1][1] - groups[-1][-1][0]
            if abs(offset - last_offset) <= BLOCK_OFFSET_TOLERANCE_SECONDS:
                groups[-1].append((cut_time, source_time, score))
                continue
        groups.append([(cut_time, source_time, score)])

    half_step = ALIGNMENT_STEP_SECONDS / 2
    edl = []
    for group in groups:
        cut_start = group[0][0] - half_step
        cut_end = group[-1][0] + half_step
        if cut_end - cut_start < MIN_BLOCK_SECONDS:
            continue
        avg_offset = sum(source_time - cut_time for cut_time, source_time, _ in group) / len(group)
        edl.append(
            {
                "cut_start": max(0.0, cut_start),
                "cut_end": cut_end,
                "source_start": max(0.0, cut_start + avg_offset),
                "source_end": cut_end + avg_offset,
            }
        )
    return edl


def build_edl(source_wav: str, cut_wav: str, env: dict[str, str]) -> list[dict]:
    matches = _windowed_matches(source_wav, cut_wav, env)
    edl = resolve_edl(matches)
    if not edl:
        die("Could not align any part of the cut against the source episode")
    return edl


_ASS_TIME_RE = re.compile(r"(\d+):(\d{2}):(\d{2})\.(\d{2})")


def parse_ass_time(value: str) -> float:
    match = _ASS_TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Invalid ASS timestamp: {value}")
    hours, minutes, seconds, centiseconds = (int(part) for part in match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) + centiseconds / 100


def format_ass_time(total_seconds: float) -> str:
    total_seconds = max(0.0, total_seconds)
    centiseconds = int(round(total_seconds * 100))
    seconds, centiseconds = divmod(centiseconds, 100)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def usable_source_styles(source_ass_text: str) -> set[str]:
    """DVD-sourced releases typically carry karaoke-timed opening/ending
    lyrics (per-syllable \\k timing tags) alongside plain dialogue. Those
    read fine set to music, they're unusable as a normal caption, so any
    style whose lines carry a \\k tag anywhere gets dropped; style *names*
    aren't a reliable signal since they vary release to release, but karaoke
    timing tags are unambiguous.
    """
    has_karaoke: dict[str, bool] = {}
    for line in source_ass_text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        style = parts[3]
        effect = parts[8]
        text = parts[9]
        # \k is per-syllable karaoke timing; \p switches into vector-drawing
        # mode for typeset logos/shapes rather than rendering any text at
        # all; a non-empty Effect field is conventionally reserved for these
        # same karaoke/typesetting purposes and essentially never used on
        # plain spoken dialogue.
        is_non_text = bool(re.search(r"\\k\d|\\p\d", text)) or bool(effect.strip())
        if is_non_text:
            has_karaoke[style] = True
        else:
            has_karaoke.setdefault(style, False)

    # A style with no karaoke/typesetting tags at all can still be a
    # transliteration track rather than an actual English translation
    # ("romaji" sing-along lyrics use plain, unstyled lines just like real
    # dialogue). Unlike song-specific style names, "romaji" is a near-
    # universal term across fansub/DVD releases, so it's worth checking for
    # directly rather than relying on tag-based detection alone. A style
    # like "Sign" (on-screen text translations) is deliberately not excluded
    # by name here even though it can carry a letter-by-letter reveal effect
    # built from dozens of fractional-second lines instead of any override
    # tag - excluding the whole style would also throw away perfectly good,
    # normal-duration sign translations. See MIN_CUE_DURATION_SECONDS below,
    # which drops just the unreadable individual lines instead.
    romaji_pattern = re.compile(r"romaji", re.IGNORECASE)
    return {
        style
        for style, karaoke in has_karaoke.items()
        if not karaoke and not romaji_pattern.search(style)
    }


# Never treat these as song lyrics even if their timing alone would suggest
# it (a title card and the ending narration both tend to sit right at the
# edges of an episode too, but neither one is a song).
NEVER_SONG_STYLE_PATTERN = re.compile(r"title|narrator|sign|credit|warning", re.IGNORECASE)
OPENING_WINDOW_SECONDS = 130
ENDING_WINDOW_SECONDS = 200


def classify_source_styles(source_ass_text: str, usable_styles: set[str], dialogue_style: str) -> set[str]:
    """Splits the usable (non-karaoke) styles into "song" vs "dialogue" so
    each can be mapped onto this project's actual Karaoke/dialogue styles.
    Timing alone isn't a reliable signal here (a title card and the ending
    narration both cluster at the edges of an episode the same way a
    translated ending theme does), so this only calls a style a song if
    every one of its lines sits inside the opening or ending window AND its
    name doesn't match a known non-song caption type.
    """
    style_times: dict[str, list[tuple[float, float]]] = {}
    max_end = 0.0
    for line in source_ass_text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10 or parts[3] not in usable_styles:
            continue
        start, end = parse_ass_time(parts[1]), parse_ass_time(parts[2])
        style_times.setdefault(parts[3], []).append((start, end))
        max_end = max(max_end, end)

    song_styles = set()
    for style, times in style_times.items():
        if style == dialogue_style or NEVER_SONG_STYLE_PATTERN.search(style):
            continue
        if all(
            end <= OPENING_WINDOW_SECONDS or start >= max_end - ENDING_WINDOW_SECONDS
            for start, end in times
        ):
            song_styles.add(style)
    return song_styles


def extract_source_subtitles(source_video: str, output_ass: str, env: dict[str, str]) -> None:
    stream_index = choose_ass_subtitle_stream(source_video, env)
    extract_ass_from_mkv(source_video, output_ass, stream_index, env)


def _retime_line(parts: list[str], edl: list[dict], style_name: str) -> tuple[float, str] | None:
    start = parse_ass_time(parts[1])
    end = parse_ass_time(parts[2])
    if end - start < MIN_CUE_DURATION_SECONDS:
        return None

    best_overlap = float("-inf")
    best_block = None
    for block in edl:
        overlap = min(end, block["source_end"]) - max(start, block["source_start"])
        if overlap > best_overlap:
            best_overlap = overlap
            best_block = block

    # Lines within 2s of a block boundary use the block directly.
    # This prevents adjacent source lines from getting different treatments.
    if best_block is not None and best_overlap > -15.0:
        offset = best_block["cut_start"] - best_block["source_start"]
        new_start_s = start + offset
        new_end_s = end + offset
        parts[1] = format_ass_time(new_start_s)
        parts[2] = format_ass_time(new_end_s)
        parts[3] = style_name
        return (new_start_s, ",".join(parts), start)

    # Larger gap — try interpolation
    if best_block is not None:
        mid = (start + end) / 2
        before = None
        after = None
        for block in edl:
            if block["source_end"] <= mid:
                before = block
            elif block["source_start"] >= mid and after is None:
                after = block
        if before is not None and after is not None:
            source_gap = after["source_start"] - before["source_end"]
            cut_gap = after["cut_start"] - before["cut_end"]
            if cut_gap > 0:
                ratio = (mid - before["source_end"]) / source_gap
                cut_mid = before["cut_end"] + ratio * cut_gap
            else:
                cut_mid = before["cut_end"]
            offset = cut_mid - mid
            new_start_s = start + offset
            new_end_s = end + offset
            parts[1] = format_ass_time(new_start_s)
            parts[2] = format_ass_time(new_end_s)
            parts[3] = style_name
            return (new_start_s, ",".join(parts), start)
        # Edge: before first block or after last block — use nearest block
        if best_block is not None:
            offset = best_block["cut_start"] - best_block["source_start"]
            new_start_s = start + offset
            new_end_s = end + offset
            parts[1] = format_ass_time(new_start_s)
            parts[2] = format_ass_time(new_end_s)
            parts[3] = style_name
            return (new_start_s, ",".join(parts), start)
        return None

    offset = best_block["cut_start"] - best_block["source_start"]
    new_start_s = start + offset
    new_end_s = end + offset

    parts[1] = format_ass_time(new_start_s)
    parts[2] = format_ass_time(new_end_s)
    parts[3] = style_name
    return (new_start_s, ",".join(parts), start)


def _resolve_overlaps(entries: list[tuple[float, str, float]]) -> list[tuple[float, str, float]]:
    """If two subtitle lines overlap in time, push the later one forward
    so they don't stack on top of each other. Song-style lines (Karaoke,
    Translation) are left overlapping intentionally. Entries are (cut_start,
    line, source_start) triples sorted by source time."""
    if len(entries) < 2:
        return entries
    song_styles = {"Karaoke", "Translation"}
    resolved: list[tuple[float, str, float]] = [entries[0]]
    for i in range(1, len(entries)):
        prev_cut, prev_line, prev_src = resolved[-1]
        curr_cut, curr_line, curr_src = entries[i]
        prev_style = prev_line.split(",", 9)[3]
        curr_style = curr_line.split(",", 9)[3]
        if prev_style in song_styles or curr_style in song_styles:
            resolved.append(entries[i])
            continue
        prev_end = parse_ass_time(prev_line.split(",", 9)[2])
        curr_end = parse_ass_time(curr_line.split(",", 9)[2])
        if curr_cut < prev_end:
            shift = prev_end - curr_cut + 0.01
            new_cut = curr_cut + shift
            new_end = curr_end + shift
            parts = curr_line.split(",", 9)
            parts[1] = format_ass_time(new_cut)
            parts[2] = format_ass_time(new_end)
            resolved.append((new_cut, ",".join(parts), curr_src))
        else:
            resolved.append(entries[i])
    return resolved


def retime_and_restyle_ass(
    source_ass: str,
    edl: list[dict],
    output_ass: str,
    style_reference_ass: str | None,
    op_from_ass: str | None = None,
) -> int:
    """Slices the source subtitle file down to only the cues that fall inside
    a kept block, retiming each into the cut's own timeline, and restyles
    surviving lines onto this project's own One Pace-style reference rather
    than keeping whatever styling the source release shipped with. A cue
    spanning a cut boundary is trimmed to whichever side has the larger
    overlap; one that's mostly in the trimmed-out part is dropped instead of
    flickering briefly.
    """
    raw = Path(source_ass).read_text(encoding="utf-8-sig", errors="replace")
    keep_styles = usable_source_styles(raw)

    style_ref_path = Path(style_reference_ass) if style_reference_ass else None
    op_ref_path = Path(op_from_ass) if op_from_ass else None
    header = resolve_ass_header(style_ref_path, op_ref_path)
    dialogue_style = resolve_dialogue_style(style_ref_path)
    music_style = resolve_music_style(header, dialogue_style)
    song_styles = classify_source_styles(raw, keep_styles, dialogue_style)

    kept_dialogue: list[str] = []
    for line in raw.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        source_style = parts[3]
        if source_style not in keep_styles:
            continue
        if any(t in parts[9] for t in _BLOCKED_TEXTS):
            continue
        if parts[9].startswith("{\\"):
            continue
        if op_from_ass and source_style in song_styles:
            start = parse_ass_time(parts[1])
            end = parse_ass_time(parts[2])
            if end <= OPENING_WINDOW_SECONDS and start < OPENING_WINDOW_SECONDS:
                continue
        style_name = music_style if source_style in song_styles else dialogue_style
        result = _retime_line(parts, edl, style_name)
        if result:
            kept_dialogue.append(result)

    if op_from_ass:
        source_first_op = None
        for line in raw.splitlines():
            if not line.startswith("Dialogue:"):
                continue
            parts = line.split(",", 9)
            if len(parts) < 10 or parts[3] not in song_styles:
                continue
            source_first_op = parse_ass_time(parts[1])
            break

        op_raw = Path(op_from_ass).read_text(encoding="utf-8-sig", errors="replace")

        # Find the first OP line time in the reference (any style) for time
        # alignment with the source episode's OP timing window.
        ref_first_op = None
        op_shift = 0.0
        for line in op_raw.splitlines():
            if not (line.startswith("Comment:") or line.startswith("Dialogue:")):
                continue
            parts = line.split(",", 9)
            if len(parts) < 10:
                continue
            if parts[3] not in ("Translation", "Karaoke"):
                continue
            if parts[8].strip() == "fx":
                continue
            ref_first_op = parse_ass_time(parts[1])
            if source_first_op is not None:
                op_shift = source_first_op - ref_first_op - 3.5
            break

        # Pass 1: English translation (Comment lines, Translation style)
        # Only keep lines whose shifted time overlaps with the first EDL
        # block (the one covering the OP window).
        first_block = edl[0] if edl else None
        for line in op_raw.splitlines():
            if not (line.startswith("Comment:") or line.startswith("Dialogue:")):
                continue
            parts = line.split(",", 9)
            if len(parts) < 10:
                continue
            if parts[3] != "Translation":
                continue
            if parts[8].strip() == "fx":
                continue
            shifted_start = parse_ass_time(parts[1]) + op_shift
            shifted_end = parse_ass_time(parts[2]) + op_shift
            if shifted_start > OPENING_WINDOW_SECONDS:
                continue
            parts[0] = "Dialogue: 0"
            parts[8] = ""
            parts[1] = format_ass_time(shifted_start)
            parts[2] = format_ass_time(shifted_end)
            result = _retime_line(parts, edl, "Translation")
            if result:
                kept_dialogue.append(result)

        # Pass 2: Japanese romaji (Comment lines, Karaoke style, strip \k tags)
        for line in op_raw.splitlines():
            if not (line.startswith("Comment:") or line.startswith("Dialogue:")):
                continue
            parts = line.split(",", 9)
            if len(parts) < 10:
                continue
            if parts[3] != "Karaoke":
                continue
            text = parts[9]
            # Keep only lines with \k tags (romaji karaoke), skip fx/empty
            if not re.search(r"\\k\d", text):
                continue
            # Strip \k timing tags, keep just the text
            text = re.sub(r"\{\\k\d+\}", "", text)
            if not text.strip():
                continue
            parts[0] = "Dialogue: 0"
            parts[8] = ""
            parts[9] = text
            shifted_start = parse_ass_time(parts[1]) + op_shift
            shifted_end = parse_ass_time(parts[2]) + op_shift
            if shifted_start > OPENING_WINDOW_SECONDS:
                continue
            parts[0] = "Dialogue: 0"
            parts[8] = ""
            parts[9] = text
            parts[1] = format_ass_time(shifted_start)
            parts[2] = format_ass_time(shifted_end)
            result = _retime_line(parts, edl, "Karaoke")
            if result:
                kept_dialogue.append(result)

    kept_dialogue.sort(key=lambda item: item[2])
    kept_dialogue = _resolve_overlaps(kept_dialogue)

    # Clip to cut video duration — drop any subtitle that starts past the
    # last EDL block's cut end, and clip lines that extend past it.
    if edl:
        cut_end = max(b["cut_end"] for b in edl)
        clipped: list[tuple[float, str, float]] = []
        for start_s, line, src_s in kept_dialogue:
            parts = line.split(",", 9)
            end_s = parse_ass_time(parts[2])
            if start_s > cut_end:
                continue
            if end_s > cut_end:
                parts[2] = format_ass_time(cut_end)
                line = ",".join(parts)
            clipped.append((start_s, line, src_s))
        kept_dialogue = clipped

    kept_dialogue = [line for _, line, _ in kept_dialogue]

    output_path = Path(output_ass)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(header + "\n".join(kept_dialogue) + "\n", encoding="utf-8")
    return len(kept_dialogue)


def retime_episode_subtitles(
    source_video: str,
    cut_video: str,
    output_ass: str,
    env: dict[str, str],
    style_reference_ass: str | None = None,
    op_from_ass: str | None = None,
) -> bool:
    """Top-level entry point: pull the embedded subtitle stream out of the
    uncut source episode, extract Japanese audio from both videos to align
    them, and retime + restyle the source subtitles onto the cut's timeline.
    """
    with tempfile.TemporaryDirectory() as tmp:
        source_ass = str(Path(tmp) / "source.ass")
        log(f"Extracting embedded subtitles from {Path(source_video).name}")
        extract_source_subtitles(source_video, source_ass, env)

        source_wav = str(Path(tmp) / "source.wav")
        cut_wav = str(Path(tmp) / "cut.wav")
        log(f"Extracting Japanese audio from {Path(source_video).name}")
        extract_alignment_audio(source_video, source_wav, env)
        log(f"Extracting Japanese audio from {Path(cut_video).name}")
        extract_alignment_audio(cut_video, cut_wav, env)

        log("Cross-correlating audio to find kept scene ranges")
        edl = build_edl(source_wav, cut_wav, env)
        log(f"Found {len(edl)} kept scene block(s)")

        kept = retime_and_restyle_ass(source_ass, edl, output_ass, style_reference_ass, op_from_ass)
        log(f"Retimed {kept} subtitle cue(s) to {output_ass}")
    return kept > 0
