#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import re
from dataclasses import dataclass

MAX_LINE_LENGTH = 42
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
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r"([,.;:!?])(\S)", r"\1 \2", t)
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


def wrap_subtitle(text: str, max_line_len: int = MAX_LINE_LENGTH, max_lines: int = MAX_LINES_PER_CUE) -> str:
    words = text.split()
    if not words:
        return ""

    lines: list[str] = []
    current = words[0]

    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) <= max_line_len:
            current = candidate
        else:
            lines.append(current)
            current = word

    lines.append(current)

    if len(lines) <= max_lines:
        return "\n".join(lines)

    # Rebalance into exactly two lines without dropping content.
    if max_lines == 2:
        best_left = text
        best_right = ""
        best_score = float("inf")
        tokens = text.split()
        for i in range(1, len(tokens)):
            left = " ".join(tokens[:i])
            right = " ".join(tokens[i:])
            overflow_penalty = max(0, len(left) - max_line_len) + max(0, len(right) - max_line_len)
            balance_penalty = abs(len(left) - len(right))
            score = overflow_penalty * 10 + balance_penalty
            if score < best_score:
                best_left = left
                best_right = right
                best_score = score
        return f"{best_left}\n{best_right}"

    merged = lines[: max_lines - 1]
    merged.append(" ".join(lines[max_lines - 1 :]))
    return "\n".join(merged)


def stylize_text(text: str, replacements: list[tuple[str, str]]) -> str:
    out = clean_spacing_and_punctuation(text)
    out = apply_common_fixes(out)
    out = apply_terms(out, replacements)
    out = clean_spacing_and_punctuation(out)
    return out


def chunk_text(text: str, max_chars: int) -> list[str]:
    words = text.split()
    if not words:
        return []

    chunks: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = word
    chunks.append(current)
    return chunks


def stylize_cue(cue: Cue, replacements: list[tuple[str, str]]) -> list[Cue]:
    base = stylize_text(cue.text, replacements)
    if not base:
        return [Cue(index=cue.index, timecode=cue.timecode, text="")]

    max_chars_per_cue = MAX_LINE_LENGTH * MAX_LINES_PER_CUE
    chunks = chunk_text(base, max_chars_per_cue)

    start_ms, end_ms = split_timecode(cue.timecode)
    duration = max(1, end_ms - start_ms)
    count = max(1, len(chunks))

    styled: list[Cue] = []
    for i, chunk in enumerate(chunks):
        chunk_text_clean = chunk.strip().rstrip(",;: ")
        if i == count - 1:
            chunk_text_clean = ensure_end_punctuation(chunk_text_clean)
        wrapped = wrap_subtitle(chunk_text_clean)

        seg_start = start_ms + (duration * i) // count
        seg_end = start_ms + (duration * (i + 1)) // count
        if seg_end <= seg_start:
            seg_end = seg_start + 1

        styled.append(Cue(index=cue.index, timecode=join_timecode(seg_start, seg_end), text=wrapped))

    return styled


def stylize_cues(cues: list[Cue], replacements: list[tuple[str, str]]) -> list[Cue]:
    output: list[Cue] = []
    for cue in cues:
        output.extend(stylize_cue(cue, replacements))
    return output


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
    write_srt(args.output, styled_cues)


if __name__ == "__main__":
    main()
