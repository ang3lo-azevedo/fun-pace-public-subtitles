"""Retimes an existing, already-translated subtitle file for an uncut source
episode onto a trimmed fan-edit cut of that same episode, instead of
generating new subtitles from scratch. A cut edit keeps the exact same
underlying audio for every scene it retains (just removes the rest), so
cross-correlating the cut's audio against the source's finds precisely which
source time ranges survived and in what order, without needing any existing
edit-decision-list. On real files this gives distinct, stable offset blocks
with clear jumps at cut boundaries, matching the "Saved" runtime difference
expected between the two.

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
import math
import re
import sys
import tempfile
import wave
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
# the cross-correlation's own jitter (a fraction of a second of wobble
# between windows that are really part of one block).
BLOCK_OFFSET_TOLERANCE_SECONDS = 2.0
MIN_BLOCK_SECONDS = 5.0
# A real caption is never displayed for less than this. Anything shorter is
# a symptom, not a style: a letter-by-letter reveal effect (e.g. for an
# on-screen "Sign" translation) is built from dozens of separate lines each
# lasting a tiny fraction of a second (one release had 71 such lines for a
# single sign). Checked against the *source* duration,
# before any cut-boundary clipping, so a legitimate cue that a cut happens
# to trim short isn't mistaken for one of these.
MIN_CUE_DURATION_SECONDS = 0.15
MIN_MATCH_SCORE = 0.02
MAX_GAP_SECONDS = 20.0
# Per-cue placement, see _locate_cues(). The blocks above only say roughly
# where a cue should land: a block's boundary is no more precise than the
# alignment step, and its offset is an average over windows that can differ
# by up to the tolerance. So each cue's own stretch of source audio is
# searched for within this radius of where its nearby blocks predict it.
CUE_SEARCH_RADIUS_SECONDS = 6.0
# Short cues are padded out to this much audio for the search; a second or
# two of speech alone matches too many places to be told apart from noise.
CUE_MIN_SEGMENT_SECONDS = 4.0
# Wide enough to reach the end of the cut from the last block, which can
# stop up to a window plus a step short of it.
CUE_CANDIDATE_MARGIN_SECONDS = 20.0
# The padding can find a position from a neighbouring line's audio alone,
# when the cue itself was cut out right next to it (seen on Marine Base
# G-8 01). So once located, a cue is scored on its own audio only, at that
# position: audio that survived into the cut scores close to 1, audio that
# was cut scores close to 0. A line trimmed partway through lands in between.
MIN_CUE_MATCH_SCORE = 0.2
# A cue whose audio can't be matched (e.g. a sign over silence) is still
# kept at its block's offset if it sits at least this deep inside the block,
# well clear of the imprecise boundaries.
BLOCK_INTERIOR_SECONDS = 10.0
# A match scoring below WEAK is dropped if it lands on top of one scoring at
# least CONFIDENT that it didn't overlap in the source: two lines can't both
# be spoken there, and the confident one is. Seen on Marine Base G-8 02: a
# line from a removed scene scored 0.29 against the stretch a real line
# (1.00) occupies.
WEAK_CUE_MATCH_SCORE = 0.5
CONFIDENT_CUE_MATCH_SCORE = 0.8
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


def resolve_edl(matches: list[tuple[float, float, float, int]]) -> list[dict]:
    """Groups per-window matches into contiguous "kept scene" blocks by
    offset (source_time - cut_time). A block boundary is wherever the offset
    jumps by more than BLOCK_OFFSET_TOLERANCE_SECONDS between consecutive
    windows - that jump is the cut - or wherever the cut moves on to a
    different source episode. Each block's own start/end come from its
    first and last window, extended by half a step on each side so the
    boundary sits between windows rather than exactly on one.
    """
    if not matches:
        return []

    groups: list[list[tuple[float, float, float, int]]] = []
    for cut_time, source_time, score, source in sorted(matches):
        offset = source_time - cut_time
        if groups:
            last_offset = groups[-1][-1][1] - groups[-1][-1][0]
            same_source = groups[-1][-1][3] == source
            if same_source and abs(offset - last_offset) <= BLOCK_OFFSET_TOLERANCE_SECONDS:
                groups[-1].append((cut_time, source_time, score, source))
                continue
        groups.append([(cut_time, source_time, score, source)])

    half_step = ALIGNMENT_STEP_SECONDS / 2
    edl = []
    for group in groups:
        cut_start = group[0][0] - half_step
        cut_end = group[-1][0] + half_step
        if cut_end - cut_start < MIN_BLOCK_SECONDS:
            continue
        avg_offset = sum(source_time - cut_time for cut_time, source_time, _, _ in group) / len(group)
        edl.append(
            {
                "source": group[0][3],
                "cut_start": max(0.0, cut_start),
                "cut_end": cut_end,
                "source_start": max(0.0, cut_start + avg_offset),
                "source_end": cut_end + avg_offset,
            }
        )
    return edl


def build_edl(
    source_wavs: list[str],
    cut_wav: str,
    env: dict[str, str],
    window_matches: list[list[tuple[float, float, float]]] | None = None,
) -> tuple[list[dict], list[tuple[float, float]]]:
    """A cut stitched together from more than one source episode is aligned
    against each of them separately; every window of the cut then goes to
    whichever source matched it best. Each block records which source (an
    index into source_wavs) it belongs to. Also returns the (start, end)
    stretches of the cut that none of the sources account for.

    window_matches caches each source's per-window results between calls, so
    adding a source later only costs aligning that one.
    """
    if window_matches is None:
        window_matches = []
    for source_wav in source_wavs[len(window_matches):]:
        window_matches.append(_windowed_matches(source_wav, cut_wav, env))
    best: dict[float, tuple[float, float, float, int]] = {}
    for source, matches in enumerate(window_matches):
        for cut_time, source_time, score in matches:
            if cut_time not in best or score > best[cut_time][2]:
                best[cut_time] = (cut_time, source_time, score, source)
    edl = resolve_edl(list(best.values()))
    if not edl:
        die("Could not align any part of the cut against the source episode(s)")
    inserts, unaccounted = _find_short_inserts(source_wavs, cut_wav, edl, env)
    if inserts:
        log(f"Found {len(inserts)} short insert(s) the alignment windows missed")
    return sorted(edl + inserts, key=lambda block: block["cut_start"]), unaccounted


def _find_short_inserts(
    source_wavs: list[str], cut_wav: str, edl: list[dict], env: dict[str, str]
) -> tuple[list[dict], list[tuple[float, float]]]:
    """A stretch of the cut shorter than an alignment window never dominates
    one, so it gets no block of its own, and if it comes from an episode
    nothing else in the cut uses, that episode isn't matched at all (seen on
    Marine Base G-8 02: a 5-second exchange taken from the end of the
    previous episode). This checks the cut in short windows against what the
    blocks predict, and searches every source in full for the windows they
    don't explain. Returns extra blocks, same shape as resolve_edl()'s, and
    the (start, end) stretches of the cut that still match nothing: either
    material that isn't from the series at all (a title card, re-mixed
    audio) or a source episode that wasn't given.
    """
    python_code = (
        "import json\n"
        "import sys\n"
        "\n"
        "import numpy as np\n"
        "from scipy.io import wavfile\n"
        "from scipy.signal import decimate, fftconvolve\n"
        "\n"
        "cut_wav, edl_json, window_seconds, explained_score, insert_score = sys.argv[1:6]\n"
        "source_wavs = sys.argv[6:]\n"
        "edl = json.loads(edl_json)\n"
        "window_seconds = float(window_seconds)\n"
        "explained_score = float(explained_score)\n"
        "insert_score = float(insert_score)\n"
        "\n"
        "def load_mono(path):\n"
        "    sr, data = wavfile.read(path)\n"
        "    if data.dtype != np.float32:\n"
        "        data = data.astype(np.float32) / 32768.0\n"
        "    return sr, data\n"
        "\n"
        "def match_scores(region, segment):\n"
        "    segment = segment - segment.mean()\n"
        "    corr = fftconvolve(region, segment[::-1], mode='valid')\n"
        "    squares = np.concatenate(([0.0], np.cumsum(region.astype(np.float64) ** 2)))\n"
        "    energy = squares[len(segment):] - squares[:-len(segment)]\n"
        "    return corr / (np.sqrt(energy * float(np.sum(segment ** 2))) + 1e-9)\n"
        "\n"
        "sr, cut = load_mono(cut_wav)\n"
        "window = int(window_seconds * sr)\n"
        "starts = list(range(0, max(len(cut) - window, 0), sr))\n"
        "# Near-silence can't be told apart from anything, so it counts as explained.\n"
        "explained = [1.0 if np.sqrt(np.mean(cut[s:s + window] ** 2)) < 0.005 else 0.0 for s in starts]\n"
        "\n"
        "# Pass 1: does each window hold what one of its nearby blocks predicts?\n"
        "margin, radius = 15.0, 6.0\n"
        "for source, source_wav in enumerate(source_wavs):\n"
        "    blocks = [block for block in edl if block['source'] == source]\n"
        "    if not blocks:\n"
        "        continue\n"
        "    _, src = load_mono(source_wav)\n"
        "    for i, start in enumerate(starts):\n"
        "        t = start / sr\n"
        "        for block in blocks:\n"
        "            if explained[i] >= explained_score:\n"
        "                break\n"
        "            if t + window_seconds < block['cut_start'] - margin or t > block['cut_end'] + margin:\n"
        "                continue\n"
        "            predicted = t + block['source_start'] - block['cut_start']\n"
        "            lo = max(0, int((predicted - radius) * sr))\n"
        "            region = src[lo:int((predicted + radius) * sr) + window]\n"
        "            if len(region) > window:\n"
        "                explained[i] = max(explained[i], float(match_scores(region, cut[start:start + window]).max()))\n"
        "\n"
        "# Pass 2: search every source in full for the unexplained windows, at a\n"
        "# quarter of the sample rate to keep that affordable.\n"
        "unexplained = [i for i, score in enumerate(explained) if score < explained_score]\n"
        "hits = {}\n"
        "if unexplained:\n"
        "    cut_low = decimate(cut, 4).astype(np.float32)\n"
        "    low_sr = sr // 4\n"
        "    low_window = window // 4\n"
        "    for source, source_wav in enumerate(source_wavs):\n"
        "        _, src = load_mono(source_wav)\n"
        "        src_low = decimate(src, 4).astype(np.float32)\n"
        "        for i in unexplained:\n"
        "            low_start = starts[i] // 4\n"
        "            scores = match_scores(src_low, cut_low[low_start:low_start + low_window])\n"
        "            idx = int(np.argmax(scores))\n"
        "            if scores[idx] >= insert_score and (i not in hits or scores[idx] > hits[i][2]):\n"
        "                hits[i] = (source, idx / low_sr - starts[i] / sr, float(scores[idx]))\n"
        "\n"
        "# Consecutive windows from the same place in the same source are one insert.\n"
        "blocks = []\n"
        "for i in sorted(hits):\n"
        "    source, offset, _ = hits[i]\n"
        "    t = starts[i] / sr\n"
        "    last = blocks[-1] if blocks else None\n"
        "    if last and last['source'] == source and abs(last['offset'] - offset) < 0.5 and t <= last['cut_end']:\n"
        "        last['cut_end'] = t + window_seconds\n"
        "        continue\n"
        "    blocks.append({'source': source, 'offset': offset, 'cut_start': t, 'cut_end': t + window_seconds})\n"
        "for block in blocks:\n"
        "    offset = block.pop('offset')\n"
        "    block['source_start'] = block['cut_start'] + offset\n"
        "    block['source_end'] = block['cut_end'] + offset\n"
        "\n"
        "# What's left: runs of windows found in no source.\n"
        "unaccounted = []\n"
        "for i in unexplained:\n"
        "    if i in hits:\n"
        "        continue\n"
        "    t = starts[i] / sr\n"
        "    if unaccounted and t <= unaccounted[-1][1]:\n"
        "        unaccounted[-1][1] = t + window_seconds\n"
        "    else:\n"
        "        unaccounted.append([t, t + window_seconds])\n"
        "print(json.dumps({'blocks': blocks, 'unaccounted': unaccounted}))\n"
    )
    args = [
        "uvx",
        "--from",
        "scipy",
        "python",
        "-c",
        python_code,
        cut_wav,
        json.dumps(edl),
        str(INSERT_WINDOW_SECONDS),
        str(INSERT_EXPLAINED_SCORE),
        str(INSERT_MATCH_SCORE),
        *source_wavs,
    ]
    result = json.loads(run_command(args, env=env, capture=True).stdout)
    unaccounted = [
        (start, end) for start, end in result["unaccounted"] if end - start >= MIN_UNACCOUNTED_SECONDS
    ]
    return result["blocks"], unaccounted


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
# A line pinned to a spot on screen or drawn as a shape is typesetting (a
# title card, a sign), laid out for the source's own resolution and fonts.
# Other override tags are ordinary dialogue styling and are kept: italics for
# thoughts, \an8 to move a line to the top, \q2 for wrapping. Dropping every line
# that starts with a tag lost 28 spoken lines across two episodes.
TYPESET_TAG_PATTERN = re.compile(r"\\(?:pos|move|org|i?clip|p[1-9])")
# Font overrides name fonts from the source release that aren't muxed here.
FONT_TAG_PATTERN = re.compile(r"\\fn[^\\}]*")
OPENING_WINDOW_SECONDS = 130
# See _lyrics_shift(). Lyric lines are several seconds apart, so this is
# tight enough not to pair a line with its neighbour.
LYRICS_PAIRING_TOLERANCE_SECONDS = 0.5
# See _find_short_inserts(). A window straddling a splice still half-matches
# its block (around 0.5), so "explained" is set below that; a full-source
# search of a couple of seconds of audio peaks around 0.2-0.45 on noise, so a
# real insert has to clear well above it.
INSERT_WINDOW_SECONDS = 2.5
INSERT_EXPLAINED_SCORE = 0.35
INSERT_MATCH_SCORE = 0.6
# Shorter unmatched stretches than this aren't reported: a single window can
# fail just for sitting on a splice or a crossfade.
MIN_UNACCOUNTED_SECONDS = 4.0
# See _exact_reference_offset().
SCENE_CHANGE_SCORE = 0.3
SCENE_SNAP_SECONDS = 0.5
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


def _locate_cues(source_wav: str, cut_wav: str, requests: list[dict], env: dict[str, str]) -> list[list[float] | None]:
    """For each request ({"start", "end", "predictions"}), takes the source
    audio under that cue and searches the cut for it around every predicted
    cut start time. Returns the best [cut_start, score] per request (None if
    there was nothing to search). The score is a normalized cross-correlation
    of the cue's own (unpadded) audio at the position found: close to 1 when
    the cut holds that exact audio, close to 0 when it doesn't.
    Same `uvx --from scipy` subprocess pattern as _windowed_matches().
    """
    python_code = (
        "import json\n"
        "import sys\n"
        "\n"
        "import numpy as np\n"
        "from scipy.io import wavfile\n"
        "from scipy.signal import fftconvolve\n"
        "\n"
        "source_wav, cut_wav, requests_path, radius, min_segment = sys.argv[1:6]\n"
        "radius = float(radius)\n"
        "min_segment = float(min_segment)\n"
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
        "with open(requests_path, encoding='utf-8') as handle:\n"
        "    requests = json.load(handle)\n"
        "\n"
        "def best_match(region, segment):\n"
        "    segment_energy = float(np.sum(segment ** 2))\n"
        "    if len(region) < len(segment) or segment_energy < 1e-6:\n"
        "        return None\n"
        "    corr = fftconvolve(region, segment[::-1], mode='valid')\n"
        "    squares = np.concatenate(([0.0], np.cumsum(region.astype(np.float64) ** 2)))\n"
        "    energy = squares[len(segment):] - squares[:-len(segment)]\n"
        "    score = corr / (np.sqrt(energy * segment_energy) + 1e-9)\n"
        "    idx = int(np.argmax(score))\n"
        "    return idx, float(score[idx])\n"
        "\n"
        "# How far the cue's own audio may sit from where the padded search put it.\n"
        "wiggle = int(0.02 * sr)\n"
        "\n"
        "results = []\n"
        "for request in requests:\n"
        "    start, end = request['start'], request['end']\n"
        "    pad = max(0.25, (min_segment - (end - start)) / 2)\n"
        "    first = max(0, int((start - pad) * sr))\n"
        "    last = min(len(src), int((end + pad) * sr))\n"
        "    segment = src[first:last]\n"
        "    segment = segment - segment.mean() if len(segment) else segment\n"
        "    core_first = int(start * sr)\n"
        "    core = src[core_first:min(len(src), int(end * sr))]\n"
        "    core = core - core.mean() if len(core) else core\n"
        "    lead = start - first / sr\n"
        "    located = None\n"
        "    for prediction in request['predictions']:\n"
        "        lo = max(0, int((prediction - lead - radius) * sr))\n"
        "        hi = min(len(cut), int((prediction - lead + radius) * sr) + len(segment))\n"
        "        match = best_match(cut[lo:hi], segment)\n"
        "        if match is not None and (located is None or match[1] > located[1]):\n"
        "            located = (lo + match[0], match[1])\n"
        "    best = None\n"
        "    if located is not None:\n"
        "        core_at = located[0] + core_first - first\n"
        "        core_match = best_match(cut[max(0, core_at - wiggle):core_at + len(core) + wiggle], core)\n"
        "        best = [located[0] / sr + lead, core_match[1] if core_match else 0.0]\n"
        "    results.append(best)\n"
        "\n"
        "print(json.dumps(results))\n"
    )

    with tempfile.TemporaryDirectory() as tmp:
        requests_path = Path(tmp) / "requests.json"
        requests_path.write_text(json.dumps(requests), encoding="utf-8")
        args = [
            "uvx",
            "--from",
            "scipy",
            "python",
            "-c",
            python_code,
            source_wav,
            cut_wav,
            str(requests_path),
            str(CUE_SEARCH_RADIUS_SECONDS),
            str(CUE_MIN_SEGMENT_SECONDS),
        ]
        result = run_command(args, env=env, capture=True)
    return json.loads(result.stdout)


def _place_cues(
    cues: list[tuple[float, float]],
    edl: list[dict],
    source_wav: str,
    cut_wav: str,
    env: dict[str, str],
) -> list[float | None]:
    """Decides where each source cue (start, end) starts on the cut's
    timeline, or None if it didn't survive the cut. The blocks only narrow
    down where to look; the cue's own audio decides.
    """
    scores: list[float] = []
    requests = []
    for start, end in cues:
        predictions: list[float] = []
        for block in edl:
            if end <= block["source_start"] - CUE_CANDIDATE_MARGIN_SECONDS:
                continue
            if start >= block["source_end"] + CUE_CANDIDATE_MARGIN_SECONDS:
                continue
            prediction = start + block["cut_start"] - block["source_start"]
            if all(abs(prediction - other) > CUE_SEARCH_RADIUS_SECONDS / 2 for other in predictions):
                predictions.append(prediction)
        requests.append({"start": start, "end": end, "predictions": predictions})

    placements: list[float | None] = []
    for (start, end), located in zip(cues, _locate_cues(source_wav, cut_wav, requests, env)):
        scores.append(located[1] if located is not None else 0.0)
        if located is not None and located[1] >= MIN_CUE_MATCH_SCORE:
            placements.append(located[0])
            continue
        interior = next(
            (
                block
                for block in edl
                if block["source_start"] + BLOCK_INTERIOR_SECONDS <= start
                and end <= block["source_end"] - BLOCK_INTERIOR_SECONDS
            ),
            None,
        )
        placements.append(start + interior["cut_start"] - interior["source_start"] if interior else None)

    confident = [
        (start, end, placement)
        for (start, end), placement, score in zip(cues, placements, scores)
        if placement is not None and score >= CONFIDENT_CUE_MATCH_SCORE
    ]
    for index, ((start, end), placement, score) in enumerate(zip(cues, placements, scores)):
        if placement is None or score >= WEAK_CUE_MATCH_SCORE:
            continue
        for other_start, other_end, other_placement in confident:
            overlapped_in_source = start < other_end and other_start < end
            shared = min(placement + end - start, other_placement + other_end - other_start) - max(placement, other_placement)
            if not overlapped_in_source and shared > (end - start) / 2:
                placements[index] = None
                break
    return placements


def _retime_line(parts: list[str], new_start_s: float, style_name: str) -> tuple[float, str, float]:
    start = parse_ass_time(parts[1])
    end = parse_ass_time(parts[2])
    parts[1] = format_ass_time(new_start_s)
    parts[2] = format_ass_time(new_start_s + end - start)
    parts[3] = style_name
    return (new_start_s, ",".join(parts), start)


def _resolve_overlaps(entries: list[tuple[float, str, float]]) -> list[tuple[float, str, float]]:
    """A cut that trims the pause between two lines leaves the earlier one
    still on screen when the later one starts, so the earlier line's end is
    pulled back to make room. The later line's start is left alone: it was
    placed against the audio, moving it would put it out of sync. Song-style
    lines (Karaoke, Translation) are left overlapping intentionally, and so
    are lines that already overlapped in the source (e.g. an announcement
    at the top of the screen over dialogue at the bottom). Entries are
    (cut_start, line, source_start) triples sorted by source time."""
    if len(entries) < 2:
        return entries
    song_styles = {"Karaoke", "Translation"}
    resolved: list[tuple[float, str, float]] = [entries[0]]
    for i in range(1, len(entries)):
        prev_cut, prev_line, prev_src = resolved[-1]
        curr_cut, curr_line, curr_src = entries[i]
        prev_parts = prev_line.split(",", 9)
        curr_style = curr_line.split(",", 9)[3]
        prev_end = parse_ass_time(prev_parts[2])
        overlapping = prev_cut < curr_cut < prev_end
        # Still at its source duration here, so this is its source end.
        overlapped_in_source = curr_src < prev_src + (prev_end - prev_cut) - 0.01
        if (
            overlapping
            and not overlapped_in_source
            and prev_parts[3] not in song_styles
            and curr_style not in song_styles
            and curr_cut - prev_cut >= MIN_CUE_DURATION_SECONDS
        ):
            prev_parts[2] = format_ass_time(curr_cut)
            resolved[-1] = (prev_cut, ",".join(prev_parts), prev_src)
        resolved.append(entries[i])
    return resolved


def _reference_lyric_lines(op_raw: str) -> list[tuple[list[str], str]]:
    """Pulls the plain lyric lines out of a One Pace opening file: the
    per-line karaoke source lines (kept there as comments, next to the
    templates and the generated per-syllable effect lines), with their \\k
    timing tags stripped. Returns (parts, style) pairs, style being
    Translation (English) or Karaoke (romaji)."""
    lyric_lines = []
    for line in op_raw.splitlines():
        if not (line.startswith("Comment:") or line.startswith("Dialogue:")):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10 or parts[3] not in ("Translation", "Karaoke"):
            continue
        effect = parts[8].strip()
        if effect != "karaoke" and not (effect == "" and re.search(r"\\k\d", parts[9])):
            continue
        text = re.sub(r"\{\\k\d+\}", "", parts[9]).strip()
        if not text:
            continue
        parts[0] = "Dialogue: 0"
        parts[8] = ""
        parts[9] = text
        lyric_lines.append((parts, parts[3]))
    return lyric_lines


def _reference_effect_lines(op_raw: str) -> list[list[str]]:
    """The generated per-syllable karaoke effect lines of a One Pace opening
    file: what One Pace's own releases actually show. They're positioned for
    the same 1440x1080 script resolution this project's style reference uses,
    so they can be reused as they are, only shifted in time."""
    effect_lines = []
    for line in op_raw.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) == 10 and parts[3] in ("Translation", "Karaoke") and parts[8].strip() == "fx":
            effect_lines.append(parts)
    return effect_lines


def _reference_sync_time(op_raw: str) -> float | None:
    """One Pace opening files carry a comment line named "sync" that starts
    on a specific scene change of the opening (described in its text)."""
    for line in op_raw.splitlines():
        if not line.startswith("Comment:"):
            continue
        parts = line.split(",", 9)
        if len(parts) == 10 and parts[4].strip().lower() == "sync":
            return parse_ass_time(parts[1])
    return None


def _scene_changes(video: str, until_seconds: float, env: dict[str, str]) -> tuple[list[float], float]:
    """Frame times of the scene changes in the first until_seconds of the
    video, and the video's frame rate."""
    probe = run_command(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", video,
        ],
        env=env,
        capture=True,
    )
    numerator, _, denominator = probe.stdout.strip().split(",")[0].partition("/")
    fps = float(numerator) / float(denominator or 1)
    result = run_command(
        [
            "ffmpeg", "-nostdin", "-t", f"{until_seconds:.3f}", "-i", video, "-map", "0:v:0",
            "-vf", f"select='gt(scene,{SCENE_CHANGE_SCORE})',showinfo", "-f", "null", "-",
        ],
        env=env,
        capture=True,
    )
    # Snapped back onto the frame grid: depending on the ffmpeg version, a
    # frame's reported time is its own or half a frame later (3.587
    # from ffmpeg 9 and 3.608 from ffmpeg 7 for the same frame).
    return [
        math.floor(float(value) * fps + 0.25) / fps for value in re.findall(r"pts_time:([0-9.]+)", result.stderr)
    ], fps


def _exact_reference_offset(op_raw: str, rough_offset: float, cut_video: str, env: dict[str, str]) -> float:
    """rough_offset (cut time minus reference time) comes from where the
    lyric lines landed, which is only good to a few tenths of a second: too
    loose for per-syllable effects. The reference's sync point is a scene
    change, so the offset is snapped to put it on the nearest scene change
    actually found in the cut, which is exact to the frame."""
    sync = _reference_sync_time(op_raw)
    if sync is None:
        log("Warning: the OP reference has no sync line, the OP effects may be slightly out of sync")
        return rough_offset
    changes, fps = _scene_changes(cut_video, sync + rough_offset + 2 * SCENE_SNAP_SECONDS, env)
    # A line's start time is written just before the frame it starts on.
    sync_frame = math.ceil(sync * fps - 1e-6) / fps
    nearest = min(changes, key=lambda change: abs(change - (sync_frame + rough_offset)), default=None)
    if nearest is None or abs(nearest - (sync_frame + rough_offset)) > SCENE_SNAP_SECONDS:
        log("Warning: no scene change found at the OP reference's sync point, the OP effects may be slightly out of sync")
        return rough_offset
    return nearest - sync_frame


def _lyrics_shift(source_starts: list[float], reference_starts: list[float]) -> float | None:
    """How much to add to the reference's times to get the source's: the
    shift under which the most lyric lines of both start together. The same
    song is subtitled line by line in both, though not always split into the
    same lines, and never timed identically, so this takes the median over
    the lines that do pair up."""
    best: tuple[int, float, float] | None = None
    for source_start in source_starts:
        for reference_start in reference_starts:
            shift = source_start - reference_start
            deviations = sorted(
                min((start - (other + shift) for other in reference_starts), key=abs) for start in source_starts
            )
            paired = [deviation for deviation in deviations if abs(deviation) <= LYRICS_PAIRING_TOLERANCE_SECONDS]
            if len(paired) < 3:
                continue
            spread = paired[-1] - paired[0]
            if best is None or (len(paired), -spread) > (best[0], -best[1]):
                best = (len(paired), spread, shift + paired[len(paired) // 2])
    return best[2] if best else None


def _retime_source_lines(
    source_ass: str,
    edl: list[dict],
    source_wav: str,
    cut_wav: str,
    env: dict[str, str],
    dialogue_style: str,
    music_style: str,
    op_from_ass: str | None = None,
    op_offsets: list[float] | None = None,
) -> list[tuple[float, str, float]]:
    """Retimes one source episode's own subtitle lines against that source's
    own blocks. Returns (cut_start, line, source_start) triples. If
    op_offsets is given, the OP reference's lyric lines are only located, not
    returned: where each one landed relative to its own time in the reference
    is appended to op_offsets instead (see _reference_effect_lines())."""
    raw = Path(source_ass).read_text(encoding="utf-8-sig", errors="replace")
    keep_styles = usable_source_styles(raw)
    song_styles = classify_source_styles(raw, keep_styles, dialogue_style)

    pending: list[tuple[list[str], str]] = []
    op_reference_starts: dict[int, float] = {}
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
        if TYPESET_TAG_PATTERN.search(parts[9]):
            continue
        parts[9] = FONT_TAG_PATTERN.sub("", parts[9]).replace("{}", "")
        if op_from_ass and source_style in song_styles:
            start = parse_ass_time(parts[1])
            end = parse_ass_time(parts[2])
            if end <= OPENING_WINDOW_SECONDS and start < OPENING_WINDOW_SECONDS:
                continue
        style_name = music_style if source_style in song_styles else dialogue_style
        pending.append((parts, style_name))

    if op_from_ass:
        op_raw = Path(op_from_ass).read_text(encoding="utf-8-sig", errors="replace")
        op_lines = _reference_lyric_lines(op_raw)
        source_lyric_starts = [
            parse_ass_time(parts[1])
            for parts in (line.split(",", 9) for line in raw.splitlines() if line.startswith("Dialogue:"))
            if len(parts) == 10 and parts[3] in song_styles and parse_ass_time(parts[2]) <= OPENING_WINDOW_SECONDS
        ]
        op_shift = _lyrics_shift(
            source_lyric_starts,
            [parse_ass_time(parts[1]) for parts, style_name in op_lines if style_name == "Translation"],
        )
        if op_shift is None:
            log("Warning: could not line up the OP reference with the source's own lyrics, leaving the OP out")
            op_lines = []

        # English translation at the bottom, Japanese romaji at the top.
        for parts, style_name in op_lines:
            op_reference_starts[id(parts)] = parse_ass_time(parts[1])
            shifted_start = parse_ass_time(parts[1]) + op_shift
            shifted_end = parse_ass_time(parts[2]) + op_shift
            if shifted_start > OPENING_WINDOW_SECONDS:
                continue
            parts[1] = format_ass_time(shifted_start)
            parts[2] = format_ass_time(shifted_end)
            pending.append((parts, style_name))

    pending = [
        (parts, style_name)
        for parts, style_name in pending
        if parse_ass_time(parts[2]) - parse_ass_time(parts[1]) >= MIN_CUE_DURATION_SECONDS
    ]
    cues = [(parse_ass_time(parts[1]), parse_ass_time(parts[2])) for parts, _ in pending]
    placements = _place_cues(cues, edl, source_wav, cut_wav, env)
    kept_dialogue = []
    for (parts, style_name), new_start_s in zip(pending, placements):
        if new_start_s is None:
            continue
        if op_offsets is not None and id(parts) in op_reference_starts:
            op_offsets.append(new_start_s - op_reference_starts[id(parts)])
            continue
        kept_dialogue.append(_retime_line(parts, new_start_s, style_name))
    kept_dialogue.sort(key=lambda item: item[2])
    return _resolve_overlaps(kept_dialogue)


def retime_and_restyle_ass(
    source_asses: list[str],
    source_wavs: list[str],
    cut_wav: str,
    edl: list[dict],
    output_ass: str,
    env: dict[str, str],
    style_reference_ass: str | None,
    op_from_ass: str | None = None,
    cut_video: str | None = None,
    manual_lines_ass: str | None = None,
) -> int:
    """Slices each source subtitle file down to only the cues whose audio
    survived into the cut, retiming each into the cut's own timeline, and
    restyles surviving lines onto this project's own One Pace-style reference
    rather than keeping whatever styling the source release shipped with.
    """
    style_ref_path = Path(style_reference_ass) if style_reference_ass else None
    op_ref_path = Path(op_from_ass) if op_from_ass else None
    header = resolve_ass_header(style_ref_path, op_ref_path)
    dialogue_style = resolve_dialogue_style(style_ref_path)
    music_style = resolve_music_style(header, dialogue_style)

    # With an OP reference that carries One Pace's karaoke effect lines (and
    # the cut's video to sync them against), those are shown instead of the
    # plain lyric lines.
    op_raw = op_ref_path.read_text(encoding="utf-8-sig", errors="replace") if op_ref_path else ""
    op_effect_lines = _reference_effect_lines(op_raw) if cut_video else []
    op_offsets: list[float] | None = [] if op_effect_lines else None

    kept_dialogue: list[tuple[float, str, float]] = []
    for source, source_ass in enumerate(source_asses):
        source_edl = [block for block in edl if block["source"] == source]
        if not source_edl:
            continue
        kept_dialogue.extend(
            _retime_source_lines(
                source_ass, source_edl, source_wavs[source], cut_wav, env,
                dialogue_style, music_style, op_from_ass, op_offsets,
            )
        )

    # Source episodes share some audio (the opening's spoken intro, a recap
    # of the previous episode), and each one's own line for it gets placed on
    # the same spot. Only the first is kept.
    seen: list[tuple[float, str]] = []
    unique: list[tuple[float, str, float]] = []
    for entry in kept_dialogue:
        text = entry[1].split(",", 9)[9]
        if any(text == other_text and abs(entry[0] - other_start) < 1.0 for other_start, other_text in seen):
            continue
        seen.append((entry[0], text))
        unique.append(entry)
    kept_dialogue = unique

    if op_offsets:
        rough_offset = sorted(op_offsets)[len(op_offsets) // 2]
        op_offset = _exact_reference_offset(op_raw, rough_offset, cut_video, env)
        log(f"Placing the OP reference's karaoke effects {op_offset:+.3f}s from their own timing")
        for parts in op_effect_lines:
            start = parse_ass_time(parts[1]) + op_offset
            end = parse_ass_time(parts[2]) + op_offset
            parts[1] = format_ass_time(start)
            parts[2] = format_ass_time(end)
            kept_dialogue.append((start, ",".join(parts), start))

    # Hand-written lines for what no source subtitle covers (e.g. the cut
    # re-edited the dialogue itself), already timed to the cut.
    if manual_lines_ass:
        manual_raw = Path(manual_lines_ass).read_text(encoding="utf-8-sig", errors="replace")
        for line in manual_raw.splitlines():
            parts = line.split(",", 9)
            if line.startswith("Dialogue:") and len(parts) == 10:
                start = parse_ass_time(parts[1])
                kept_dialogue.append((start, line, start))

    # Clip to the cut's own duration: drop any subtitle that starts past
    # its end, and clip lines that extend past it. Not the last block's end:
    # the last alignment window stops short of the end of the cut, and lines
    # in that tail are placed by their own audio like any other.
    if edl:
        with wave.open(cut_wav, "rb") as handle:
            cut_end = handle.getnframes() / handle.getframerate()
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


def _format_clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def retime_episode_subtitles(
    source_videos: list[str],
    cut_video: str,
    output_ass: str,
    env: dict[str, str],
    style_reference_ass: str | None = None,
    op_from_ass: str | None = None,
    manual_lines_ass: str | None = None,
) -> bool:
    """Top-level entry point: pull the embedded subtitle stream out of each
    uncut source episode, extract Japanese audio from every video to align
    them, and retime + restyle the source subtitles onto the cut's timeline.
    """
    with tempfile.TemporaryDirectory() as tmp:
        source_asses: list[str] = []
        source_wavs: list[str] = []

        def add_source(source_video: str) -> None:
            source = len(source_wavs)
            source_ass = str(Path(tmp) / f"source{source}.ass")
            log(f"Extracting embedded subtitles from {Path(source_video).name}")
            extract_source_subtitles(source_video, source_ass, env)
            source_asses.append(source_ass)

            source_wav = str(Path(tmp) / f"source{source}.wav")
            log(f"Extracting Japanese audio from {Path(source_video).name}")
            extract_alignment_audio(source_video, source_wav, env)
            source_wavs.append(source_wav)

        for source_video in source_videos:
            add_source(source_video)

        cut_wav = str(Path(tmp) / "cut.wav")
        log(f"Extracting Japanese audio from {Path(cut_video).name}")
        extract_alignment_audio(cut_video, cut_wav, env)

        log("Cross-correlating audio to find kept scene ranges")
        window_matches: list[list[tuple[float, float, float]]] = []
        while True:
            edl, unaccounted = build_edl(source_wavs, cut_wav, env, window_matches)
            if not unaccounted:
                break
            total = sum(end - start for start, end in unaccounted)
            log(f"{total:.0f}s of the cut match none of the source episodes given:")
            for start, end in unaccounted:
                log(f"  {_format_clock(start)} - {_format_clock(end)}")
            log("These are either not from the series (a title card, re-mixed audio) or from a missing source episode.")
            if not sys.stdin.isatty():
                break
            sys.stderr.write("Path to another source episode to try (Enter to continue without): ")
            sys.stderr.flush()
            answer = sys.stdin.readline().strip().strip("'\"")
            if not answer:
                break
            extra_video = str(Path(answer).expanduser())
            if not Path(extra_video).is_file():
                log(f"Not a file: {extra_video}")
                continue
            if any(Path(extra_video).resolve() == Path(video).resolve() for video in source_videos):
                log("That episode is already one of the sources.")
                continue
            source_videos = [*source_videos, extra_video]
            add_source(extra_video)
        log(f"Found {len(edl)} kept scene block(s)")
        for source, source_video in enumerate(source_videos):
            if not any(block["source"] == source for block in edl):
                log(f"Warning: no part of the cut matched {Path(source_video).name}")

        kept = retime_and_restyle_ass(source_asses, source_wavs, cut_wav, edl, output_ass, env, style_reference_ass, op_from_ass, cut_video, manual_lines_ass)
        log(f"Retimed {kept} subtitle cue(s) to {output_ass}")
    return kept > 0
