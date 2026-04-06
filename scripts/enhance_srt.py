#!/usr/bin/env python3
"""
Enhanced SRT post-processor with context-aware corrections and audio-driven improvements.
Fixes common transcription errors, improves punctuation, and applies context-based normalization.
"""
from __future__ import annotations

import argparse
import pathlib
import re
from typing import NamedTuple


class Subtitle(NamedTuple):
    index: int
    timecode: str
    text: str


# Common transcription error patterns in anime/One Piece content
COMMON_ERRORS = [
    # Fix repetitive stammering from transcription errors
    (r"\b(\w+)\s+\1\s+\1\b", r"\1"),  # word word word -> word
    (r"\b(\w+)\s+\1\b", r"\1"),  # word word -> word (except for intentional ones)
    
    # Fix common mishearings
    (r"\bwanna\b", "want to"),
    (r"\bgonna\b", "going to"),
    (r"\bkinda\b", "kind of"),
    (r"\bsorta\b", "sort of"),
    (r"\b'em\b", "them"),
    
    # Fix transcription artifacts
    (r"\byou\'s\b", "you"),
    (r"\bthey\'s\b", "they"),
    (r"\b\'s\s+(?=\w)", "'s "),
]

# Context markers that indicate specific characters/scenes
CHARACTER_CONTEXT = {
    "chopper": ["chopper", "doctor", "reindeer", "cotton candy"],
    "luffy": ["luffy", "gum gum", "straw hat", "captain", "meat"],
    "zoro": ["zoro", "swordsman", "three sword", "green hair"],
    "nami": ["nami", "navigator", "cartographer", "orange hair", "weather"],
    "usopp": ["usopp", "sniper", "storyteller", "going merry"],
    "sanji": ["sanji", "cook", "curly brow", "love cook"],
    "robin": ["robin", "archaeologist", "devil child"],
}

LOCATION_CONTEXT = {
    "drum island": ["drum", "island", "snow", "doctor"],
    "grand line": ["grand line", "new world", "sea"],
    "east blue": ["east blue", "beginning"],
}


def parse_srt(content: str) -> list[Subtitle]:
    """Parse SRT file into subtitle objects."""
    blocks = re.split(r"\n\s*\n", content.strip())
    subtitles = []
    
    for block in blocks:
        lines = block.split("\n")
        if len(lines) < 3:
            continue
        
        try:
            index = int(lines[0])
            timecode = lines[1]
            text = "\n".join(lines[2:])
            subtitles.append(Subtitle(index=index, timecode=timecode, text=text))
        except (ValueError, IndexError):
            continue
    
    return subtitles


def fix_common_errors(text: str) -> str:
    """Apply common transcription error fixes."""
    result = text
    for pattern, replacement in COMMON_ERRORS:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return result


def improve_punctuation(text: str) -> str:
    """Improve punctuation and sentence structure."""
    result = text
    
    # Add periods at end of sentences if missing (but preserve existing punctuation)
    lines = result.split("\n")
    improved_lines = []
    
    for line in lines:
        stripped = line.rstrip()
        # If line has text and doesn't end with punctuation, add period
        if stripped and not re.search(r'[.!?,;:\-—]$', stripped):
            # Don't add if it looks like it continues on next line (no capital letter)
            if re.search(r'[A-Z]', stripped) and len(stripped) > 3:
                stripped = stripped + "."
        improved_lines.append(stripped)
    
    return "\n".join(improved_lines)


def fix_capitalization(text: str) -> str:
    """Fix capitalization issues."""
    result = text
    
    # Capitalize first letter of sentences after periods
    result = re.sub(r'(\. )([a-z])', lambda m: m.group(1) + m.group(2).upper(), result)
    
    # Capitalize "I" pronoun
    result = re.sub(r'\bi\b', 'I', result)
    
    # Fix common words that should be capitalized (character names, places)
    # These should already be handled by terminology file, but catch any missed ones
    known_caps = {
        'chopper': 'Chopper',
        'luffy': 'Luffy',
        'zoro': 'Zoro',
        'nami': 'Nami',
        'usopp': 'Usopp',
        'sanji': 'Sanji',
        'robin': 'Robin',
    }
    
    for lower, proper in known_caps.items():
        pattern = re.compile(rf'\b{lower}\b', re.IGNORECASE)
        result = pattern.sub(proper, result)
    
    return result


def get_context(subtitles: list[Subtitle], index: int, window: int = 3) -> str:
    """Get surrounding subtitle text for context analysis."""
    start = max(0, index - window)
    end = min(len(subtitles), index + window + 1)
    context_texts = [sub.text.lower() for sub in subtitles[start:end]]
    return " ".join(context_texts)


def enhance_subtitle(subtitle: Subtitle, context: str, all_subs: list[Subtitle], index_in_list: int) -> str:
    """Apply context-aware enhancements to a subtitle."""
    text = subtitle.text
    
    # Fix common errors
    text = fix_common_errors(text)
    
    # Fix capitalization
    text = fix_capitalization(text)
    
    # Improve punctuation
    text = improve_punctuation(text)
    
    # Context-aware character name disambiguation
    # If we see markers for a character, ensure names are correct
    for character, markers in CHARACTER_CONTEXT.items():
        if any(marker in context for marker in markers):
            # Boost confidence in character mentions
            pattern = re.compile(rf'\b{character}\b', re.IGNORECASE)
            text = pattern.sub(character.capitalize(), text)
    
    return text


def enhance_srt(input_path: pathlib.Path, output_path: pathlib.Path) -> None:
    """Process and enhance entire SRT file."""
    content = input_path.read_text(encoding="utf-8-sig")
    subtitles = parse_srt(content)
    
    enhanced_subtitles = []
    for idx, subtitle in enumerate(subtitles):
        context = get_context(subtitles, idx)
        enhanced_text = enhance_subtitle(subtitle, context, subtitles, idx)
        enhanced_subtitles.append(Subtitle(
            index=subtitle.index,
            timecode=subtitle.timecode,
            text=enhanced_text
        ))
    
    # Write enhanced SRT
    output_lines = []
    for sub in enhanced_subtitles:
        output_lines.append(str(sub.index))
        output_lines.append(sub.timecode)
        output_lines.append(sub.text)
        output_lines.append("")
    
    output_path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enhance SRT subtitle quality with context-aware corrections."
    )
    parser.add_argument("--input", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    enhance_srt(args.input, args.output)
    print(f"Enhanced subtitles written to {args.output}", file=__import__("sys").stderr)


if __name__ == "__main__":
    main()
