#!/usr/bin/env python3
"""Standalone CLI (also invoked as a subprocess from fun-pace-subs.py) that
converts a styled SRT into a One Pace-styled ASS file: reuses styling from
a reference ASS if one is given (see resolve_ass_header/resolve_dialogue_style),
and assigns OP/ED-window cues to a karaoke-style look distinct from regular
dialogue (see classify_style).
"""
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
Style: Karaoke,Duality,68,&H00292CC7,&H000019FF,&H00FFFFFF,&H00000000,0,0,0,0,100,100,4,0,1,6.8,0,8,23,23,23,1
Style: Translation,Duality,68,&H00292CC7,&H000019FF,&H00FFFFFF,&H00000000,0,0,0,0,100,100,4,0,1,6.8,0,2,23,23,34,1

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


# One Pace conventionally renders OP/ED lyrics as small top-of-screen karaoke
# text, distinct from bottom-center dialogue. Used when a style reference
# doesn't define its own Karaoke/Translation style.
FALLBACK_KARAOKE_STYLE = (
    "Style: Karaoke,Duality,68,&H00292CC7,&H000019FF,&H00FFFFFF,&H00000000,"
    "0,0,0,0,100,100,4,0,1,6.8,0,8,23,23,23,1"
)


def resolve_ass_header(style_from_ass: pathlib.Path | None, op_style_from_ass: pathlib.Path | None = None) -> str:
    if style_from_ass is None:
        return ASS_HEADER

    raw = style_from_ass.read_text(encoding="utf-8-sig", errors="replace")
    script_info = extract_ass_section(raw, "[Script Info]")
    styles = extract_ass_section(raw, "[V4+ Styles]")

    if not script_info or not styles:
        return ASS_HEADER

    style_names = set(parse_style_names_from_styles_block(styles))
    if "Karaoke" not in style_names or "Translation" not in style_names:
        if op_style_from_ass is not None:
            op_raw = op_style_from_ass.read_text(encoding="utf-8-sig", errors="replace")
            op_styles = extract_ass_section(op_raw, "[V4+ Styles]")
            if op_styles:
                for name in parse_style_names_from_styles_block(op_styles):
                    if name in ("Karaoke", "Translation") and name not in style_names:
                        style_line = next(
                            (l for l in op_styles.splitlines() if l.startswith(f"Style: {name},")), None
                        )
                        if style_line:
                            styles = f"{styles}\n{style_line}"
                            style_names.add(name)
        if "Karaoke" not in style_names and "Translation" not in style_names:
            styles = f"{styles}\n{FALLBACK_KARAOKE_STYLE}"

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
    """A reference ASS's dialogue style is never named consistently across
    releases, so this guesses it from usage: whichever non-song/non-credits
    style actually appears most often on Dialogue lines is almost certainly the
    main spoken-dialogue style.
    """
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


def srt_timestamp_to_ms(value: str) -> int:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value}")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return (((hours * 60) + minutes) * 60 + seconds) * 1000 + millis


def ms_to_ass_time(total_ms: int) -> str:
    if total_ms < 0:
        total_ms = 0
    centiseconds = (total_ms % 1000) // 10
    total_seconds = total_ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours}:{minutes:02}:{seconds:02}.{centiseconds:02}"


def escape_ass_text(text: str) -> str:
    text = text.replace("\\", r"\\")
    text = text.replace("{", r"\{")
    text = text.replace("}", r"\}")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return r"\N".join(lines)


def parse_srt(raw: str) -> list[tuple[int, int, str]]:
    blocks = re.split(r"\r?\n\r?\n", raw.strip())
    cues: list[tuple[int, int, str]] = []

    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        if "-->" not in lines[1]:
            continue

        start_raw, end_raw = [part.strip() for part in lines[1].split("-->")]
        start = srt_timestamp_to_ms(start_raw)
        end = srt_timestamp_to_ms(end_raw)
        text = escape_ass_text("\n".join(lines[2:]))
        if not text:
            continue
        cues.append((start, end, text))

    return cues


def classify_style(start_ms: int, end_ms: int, dialogue_style: str, music_style: str = "Karaoke") -> str:
    """Simple heuristic, not audio analysis: One Piece-style openings run well
    under 110s, so any cue fully inside that window is assumed to be OP lyrics
    rather than dialogue. Good enough in practice since actual spoken dialogue
    essentially never starts before the OP finishes.
    """
    if end_ms <= 110_000 and start_ms < 110_000:
        return music_style
    return dialogue_style


def resolve_music_style(header: str, dialogue_style: str) -> str:
    styles_block = extract_ass_section(header, "[V4+ Styles]")
    if not styles_block:
        return dialogue_style
    style_names = set(parse_style_names_from_styles_block(styles_block))
    if "Karaoke" in style_names:
        return "Karaoke"
    if "Translation" in style_names:
        return "Translation"
    return dialogue_style


def write_ass(output_path: pathlib.Path, cues: list[tuple[int, int, str]], header: str, dialogue_style: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    music_style = resolve_music_style(header, dialogue_style)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(header)
        for start_ms, end_ms, text in cues:
            style_name = classify_style(start_ms, end_ms, dialogue_style, music_style)
            start = ms_to_ass_time(start_ms)
            end = ms_to_ass_time(end_ms)
            handle.write(f"Dialogue: 0,{start},{end},{style_name},,0,0,0,,{text}\n")


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
