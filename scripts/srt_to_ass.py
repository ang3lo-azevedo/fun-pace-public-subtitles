#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import re
from collections import Counter

ASS_HEADER = """[Script Info]
Title: Fun Pace Styled Subtitles
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: 1440
PlayResY: 1080
LayoutResX: 1440
LayoutResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main-207-,Impress BT Pace,82,&H00FFFFFF,&H00002EFF,&H00000000,&H78000000,0,0,0,0,100,100,0,0,1,3.8,3.8,2,180,180,27,1
Style: Top,Impress BT Pace,82,&H00FFFFFF,&H00002EFF,&H00000000,&H78000000,0,0,0,0,100,100,0,0,1,3.8,3.8,8,180,180,27,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def extract_ass_section(raw: str, section_name: str) -> str | None:
    lines = raw.splitlines()
    capture = False
    collected: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if stripped.lower() == section_name.lower():
                capture = True
                collected = [line]
                continue
            if capture:
                break
        if capture:
            collected.append(line)
    if not collected:
        return None
    return "\n".join(collected).strip()


def resolve_ass_header(style_from_ass: pathlib.Path | None) -> str:
    if style_from_ass is None:
        return ASS_HEADER

    raw = style_from_ass.read_text(encoding="utf-8-sig", errors="replace")
    script_info = extract_ass_section(raw, "[Script Info]")
    styles = extract_ass_section(raw, "[V4+ Styles]")

    if not script_info or not styles:
        return ASS_HEADER

    events = "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    return f"{script_info}\n\n{styles}\n\n{events}\n"


def parse_style_names_from_styles_block(styles_block: str) -> list[str]:
    style_names: list[str] = []
    for line in styles_block.splitlines():
        if line.startswith("Style:"):
            raw_fields = line[len("Style:") :].strip()
            parts = [part.strip() for part in raw_fields.split(",")]
            if parts:
                style_names.append(parts[0])
    return style_names


def pick_reference_dialogue_style(raw_ass: str, styles_block: str) -> str:
    styles_available = set(parse_style_names_from_styles_block(styles_block))
    if not styles_available:
        return "Main-207-"

    counts: Counter[str] = Counter()
    for line in raw_ass.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        # Dialogue format starts with: Dialogue: Layer,Start,End,Style,...
        parts = line.split(",", 4)
        if len(parts) < 4:
            continue
        style_name = parts[3].strip()
        if style_name in styles_available:
            counts[style_name] += 1

    if not counts:
        if "Main-207-" in styles_available:
            return "Main-207-"
        if "Default" in styles_available:
            return "Default"
        return next(iter(styles_available))

    # Prefer the dominant regular dialogue style and avoid helper/song/credits styles.
    excluded_pattern = re.compile(
        r"warning|sign|song|karaoke|op|ed|ending|translation|romaji|credits|paper johnny",
        re.IGNORECASE,
    )
    filtered = [
        (name, count)
        for name, count in counts.most_common()
        if not excluded_pattern.search(name)
    ]
    if filtered:
        main_styles = [item for item in filtered if re.search(r"^main[-_ ]", item[0], re.IGNORECASE)]
        if main_styles:
            return main_styles[0][0]

        # Prefer obvious "main dialogue" style names when available.
        preferred_main = [
            item for item in filtered if re.search(r"^main|dialog|default", item[0], re.IGNORECASE)
        ]
        if preferred_main:
            return preferred_main[0][0]
        return filtered[0][0]

    return counts.most_common(1)[0][0]


def resolve_dialogue_style(style_from_ass: pathlib.Path | None) -> str:
    if style_from_ass is None:
        return "Main-207-"

    raw = style_from_ass.read_text(encoding="utf-8-sig", errors="replace")
    styles = extract_ass_section(raw, "[V4+ Styles]")
    if not styles:
        return "Main-207-"
    return pick_reference_dialogue_style(raw, styles)


def srt_to_ass_time(value: str) -> str:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value}")
    h, m, s, ms = match.groups()
    centiseconds = int(ms) // 10
    return f"{int(h)}:{m}:{s}.{centiseconds:02d}"


def escape_ass_text(text: str) -> str:
    text = text.replace("\\", r"\\")
    text = text.replace("{", r"\{")
    text = text.replace("}", r"\}")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return r"\N".join(lines)


def parse_srt(raw: str) -> list[tuple[str, str, str]]:
    blocks = re.split(r"\r?\n\r?\n", raw.strip())
    cues: list[tuple[str, str, str]] = []

    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        if "-->" not in lines[1]:
            continue

        start_raw, end_raw = [part.strip() for part in lines[1].split("-->")]
        start = srt_to_ass_time(start_raw)
        end = srt_to_ass_time(end_raw)
        text = escape_ass_text("\n".join(lines[2:]))
        if not text:
            continue
        cues.append((start, end, text))

    return cues


def write_ass(output_path: pathlib.Path, cues: list[tuple[str, str, str]], header: str, dialogue_style: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(header)
        for start, end, text in cues:
            handle.write(f"Dialogue: 0,{start},{end},{dialogue_style},,0,0,0,,{text}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert SRT subtitles to styled ASS format.")
    parser.add_argument("--input", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--style-from-ass", type=pathlib.Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raw = args.input.read_text(encoding="utf-8-sig")
    cues = parse_srt(raw)
    header = resolve_ass_header(args.style_from_ass)
    dialogue_style = resolve_dialogue_style(args.style_from_ass)
    write_ass(args.output, cues, header, dialogue_style)


if __name__ == "__main__":
    main()
