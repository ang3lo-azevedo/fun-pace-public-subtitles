#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import re
import textwrap
from dataclasses import dataclass

MAX_LINE_LENGTH = 34
MAX_LINES_PER_CUE = 2

DEFAULT_REPLACEMENTS = [
    ("zolo", "Zoro"),
    ("lufi", "Luffy"),
    ("grand line", "Grand Line"),
    ("all blue", "All Blue"),
    ("going merry", "Going Merry"),
]

COMMON_FIXES = [
    (r"\b(\w+)\s+\1\s+\1\b", r"\1"),
    (r"\b(\w{3,})\s+\1\b", r"\1"),
    (r"\bkinda\b", "kind of"),
    (r"\bsorta\b", "sort of"),
    (r"\bwanna\b", "want to"),
    (r"\bgonna\b", "going to"),
    (r"\bnani\b", "what"),
    (r"\bkirsten\b", "Kristen"),
]

BREAK_PUNCTUATION = {",", ";", ":", "?", "!", "...", "—", "-"}
BREAK_WORDS = {
    "and",
    "but",
    "so",
    "or",
    "then",
    "because",
    "while",
    "when",
    "if",
    "though",
    "although",
    "since",
    "therefore",
}


@dataclass
class Cue:
    index: str
    timecode: str
    text: str


def parse_timestamp(value: str) -> int:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if not match:
        raise ValueError(f"Invalid timestamp: {value}")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return (((hours * 60) + minutes) * 60 + seconds) * 1000 + millis


def format_timestamp(total_millis: int) -> str:
    if total_millis < 0:
        total_millis = 0
    millis = total_millis % 1000
    total_seconds = total_millis // 1000
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def split_timecode(value: str) -> tuple[int, int]:
    parts = [part.strip() for part in value.split("-->")]
    if len(parts) != 2:
        raise ValueError(f"Invalid timecode: {value}")
    return parse_timestamp(parts[0]), parse_timestamp(parts[1])


def join_timecode(start_ms: int, end_ms: int) -> str:
    return f"{format_timestamp(start_ms)} --> {format_timestamp(end_ms)}"


def load_replacements(terms_file: pathlib.Path | None) -> list[tuple[str, str]]:
    replacements = list(DEFAULT_REPLACEMENTS)
    if terms_file is None:
        return replacements

    with terms_file.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                raise SystemExit(f"Invalid terms row in {terms_file}: {raw_line.rstrip()!r}")
            source = parts[0].strip()
            target = parts[1].strip()
            if source and target:
                replacements.append((source, target))
    return replacements


def parse_srt(raw: str) -> list[Cue]:
    blocks = re.split(r"\r?\n\r?\n", raw.strip())
    cues: list[Cue] = []
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        cues.append(Cue(index=lines[0], timecode=lines[1], text=" ".join(lines[2:])))
    return cues


def apply_terms(text: str, replacements: list[tuple[str, str]]) -> str:
    output = text
    for source, target in replacements:
        pattern = re.compile(rf"(?i)\b{re.escape(source)}\b")
        output = pattern.sub(target, output)
    return output


def clean_spacing_and_punctuation(text: str) -> str:
    t = text.strip()
    t = re.sub(r"\s+", " ", t)
    t = t.replace(". . .", "...")
    t = t.replace(". ..", "...")
    t = t.replace(".. .", "...")
    t = re.sub(r"\s+([,;:!?])", r"\1", t)
    t = re.sub(r"([,;:!?])(\S)", r"\1 \2", t)
    t = re.sub(r"\s+'", "'", t)
    t = re.sub(r"'\s+", "'", t)
    return t.strip()


def apply_common_fixes(text: str) -> str:
    out = text
    for pattern, replacement in COMMON_FIXES:
        out = re.sub(pattern, replacement, out, flags=re.IGNORECASE)
    out = re.sub(r"\bi\b", "I", out)
    out = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: f"{m.group(1)}{m.group(2).upper()}", out)
    return out


def ensure_end_punctuation(text: str) -> str:
    if not text:
        return text

    text = re.sub(r"^([Ww]hat)\.$", r"\1?", text)
    text = re.sub(r"^([Ww]here)\.$", r"\1?", text)
    text = re.sub(r"^([Ww]ho)\.$", r"\1?", text)
    text = re.sub(r"^([Ww]hy)\.$", r"\1?", text)

    if re.search(r"[.!?]$", text):
        return text
    # Keep very short reaction cues snappy.
    word_count = len(text.split())
    if word_count <= 2:
        lower = text.lower()
        if lower == "what":
            return text + "?"
        if lower in {"yeah", "no", "okay", "ok", "huh", "hey"}:
            return text + "!"
    return text + "."


def choose_line_break(tokens: list[str], max_line_len: int) -> int:
    if len(tokens) <= 1:
        return 0

    best_index = 1
    best_score = float("inf")

    for index in range(1, len(tokens)):
        left = " ".join(tokens[:index])
        right = " ".join(tokens[index:])
        left_len = len(left)
        right_len = len(right)

        if not left or not right:
            continue

        overflow_penalty = max(0, left_len - max_line_len) + max(0, right_len - max_line_len)
        balance_penalty = abs(left_len - right_len)
        punctuation_bonus = 0
        break_word_bonus = 0

        prev_word = tokens[index - 1]
        next_word = tokens[index]
        if prev_word[-1:] in {",", ";", ":", "?", "!"} or prev_word.endswith("..."):
            punctuation_bonus = 12
        elif next_word.lower().strip("'\"()[]{}").rstrip(",;:?!") in BREAK_WORDS:
            break_word_bonus = 6

        # Avoid very short first lines unless the punctuation strongly suggests it.
        short_line_penalty = 0
        if left_len < max_line_len * 0.35:
            short_line_penalty = int((max_line_len * 0.35 - left_len) * 2)

        score = overflow_penalty * 20 + balance_penalty + short_line_penalty - punctuation_bonus - break_word_bonus
        if score < best_score:
            best_score = score
            best_index = index

    return best_index


def wrap_subtitle_lines(text: str, max_line_len: int = MAX_LINE_LENGTH, max_lines: int = MAX_LINES_PER_CUE) -> list[str]:
    words = text.split()
    if not words:
        return []

    if len(text) <= max_line_len:
        return [text]

    if max_lines != 2:
        return [text]

    break_index = choose_line_break(words, max_line_len)
    left = " ".join(words[:break_index]).strip()
    right = " ".join(words[break_index:]).strip()

    if not left or not right:
        return [text]

    if len(left) <= max_line_len and len(right) <= max_line_len:
        return [left, right]

    # Neither half of the balanced split fits (e.g. long OP/ED lines merged by
    # alignment) — fall back to a plain greedy wrap so no single line overflows
    # the video width and gets force-wrapped again by the renderer.
    return textwrap.wrap(text, width=max_line_len, break_long_words=False) or [text]


def stylize_text(text: str, replacements: list[tuple[str, str]]) -> str:
    out = clean_spacing_and_punctuation(text)
    out = apply_common_fixes(out)
    out = apply_terms(out, replacements)
    out = clean_spacing_and_punctuation(out)
    return out


def stylize_cue(cue: Cue, replacements: list[tuple[str, str]]) -> Cue:
    base = stylize_text(cue.text, replacements)
    if not base:
        return Cue(index=cue.index, timecode=cue.timecode, text="")

    cleaned = ensure_end_punctuation(base.strip().rstrip(",;: "))
    return Cue(index=cue.index, timecode=cue.timecode, text=cleaned)


def split_cue_for_length(
    cue: Cue, max_line_len: int = MAX_LINE_LENGTH, max_lines: int = MAX_LINES_PER_CUE
) -> list[Cue]:
    if not cue.text:
        return [cue]

    lines = wrap_subtitle_lines(cue.text, max_line_len, max_lines)
    if not lines:
        return [Cue(index=cue.index, timecode=cue.timecode, text="")]

    if len(lines) <= max_lines:
        return [Cue(index=cue.index, timecode=cue.timecode, text="\n".join(lines))]

    # More lines than fit on screen at once (e.g. a long OP/ED line merged by
    # alignment) — split across multiple cues instead of overflowing past
    # max_lines, timed proportionally to how much text each group holds.
    start_ms, end_ms = split_timecode(cue.timecode)
    total_duration = max(end_ms - start_ms, 1)
    groups = [lines[i : i + max_lines] for i in range(0, len(lines), max_lines)]
    total_chars = sum(len(line) for line in lines) or 1

    result: list[Cue] = []
    cursor_ms = start_ms
    consumed_chars = 0
    for i, group in enumerate(groups):
        consumed_chars += sum(len(line) for line in group)
        if i == len(groups) - 1:
            group_end_ms = end_ms
        else:
            group_end_ms = start_ms + int(total_duration * consumed_chars / total_chars)
            group_end_ms = max(group_end_ms, cursor_ms + 1)
        result.append(Cue(index=cue.index, timecode=join_timecode(cursor_ms, group_end_ms), text="\n".join(group)))
        cursor_ms = group_end_ms
    return result


def stylize_cues(cues: list[Cue], replacements: list[tuple[str, str]]) -> list[Cue]:
    output: list[Cue] = []
    for cue in cues:
        styled = stylize_cue(cue, replacements)
        output.extend(split_cue_for_length(styled))
    return output


# Whisper occasionally hallucinates the same line back-to-back (e.g. over music or
# silence). Merge those runs into a single cue instead of flashing duplicates.
DUPLICATE_MERGE_MAX_GAP_MS = 3000


def merge_consecutive_duplicates(cues: list[Cue]) -> list[Cue]:
    merged: list[Cue] = []
    for cue in cues:
        if merged and merged[-1].text and cue.text.strip().lower() == merged[-1].text.strip().lower():
            prev = merged[-1]
            prev_start, prev_end = split_timecode(prev.timecode)
            next_start, next_end = split_timecode(cue.timecode)
            if next_start - prev_end <= DUPLICATE_MERGE_MAX_GAP_MS:
                merged[-1] = Cue(index=prev.index, timecode=join_timecode(prev_start, next_end), text=prev.text)
                continue
        merged.append(cue)
    return merged


def write_srt(path: pathlib.Path, cues: list[Cue]) -> None:
    blocks = []
    for i, cue in enumerate(cues, start=1):
        blocks.append(f"{i}\n{cue.timecode}\n{cue.text}")
    path.write_text("\n\n".join(blocks).rstrip() + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply fan-sub style formatting to an SRT file.")
    parser.add_argument("--input", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--terms-file", type=pathlib.Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    replacements = load_replacements(args.terms_file)
    raw = args.input.read_text(encoding="utf-8-sig")
    cues = parse_srt(raw)

    styled_cues = stylize_cues(cues, replacements)
    styled_cues = merge_consecutive_duplicates(styled_cues)
    write_srt(args.output, styled_cues)


if __name__ == "__main__":
    main()
