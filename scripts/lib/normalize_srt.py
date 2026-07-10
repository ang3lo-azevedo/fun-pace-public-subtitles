#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import re
from typing import Iterable

DEFAULT_REPLACEMENTS = [
    ("zolo", "Zoro"),
    ("lufi", "Luffy"),
    ("grand line", "Grand Line"),
    ("all blue", "All Blue"),
    ("going merry", "Going Merry"),
]


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
            replacements.append((parts[0].strip(), parts[1].strip()))

    return replacements


def apply_replacements(text: str, replacements: Iterable[tuple[str, str]]) -> str:
    output = text
    for source, target in replacements:
        pattern = re.compile(rf"(?i)\b{re.escape(source)}\b")
        output = pattern.sub(target, output)
    return output


def normalize_srt(input_path: pathlib.Path, output_path: pathlib.Path, replacements: Iterable[tuple[str, str]]) -> None:
    raw = input_path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\r?\n\r?\n", raw.strip())
    normalized_blocks: list[str] = []

    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3:
            normalized_blocks.append(block)
            continue

        header = lines[:2]
        body = [apply_replacements(line, replacements) for line in lines[2:]]
        normalized_blocks.append("\n".join(header + body))

    output_path.write_text("\n\n".join(normalized_blocks).rstrip() + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize subtitle terminology in an SRT file.")
    parser.add_argument("--input", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--terms-file", type=pathlib.Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    replacements = load_replacements(args.terms_file)
    normalize_srt(args.input, args.output, replacements)


if __name__ == "__main__":
    main()
