"""Publishes an episode's subtitle file as a GitHub release (via the `gh`
CLI), following the naming convention described in the README's "Releasing an
episode" section. Everything is derived from the Fun Pace cut's file name.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from lib.common import command_exists, die, log, run_command

# "[FunPace] Marine Base G-8 01 - The Ghosting Merry [Dual Audio][...]" ->
# series "Marine Base G-8", number "01", title "The Ghosting Merry".
EPISODE_NAME_PATTERN = re.compile(r"^(?P<series>.+?) (?P<number>\d+) - (?P<title>.+)$")


def parse_episode_name(cut_video: str) -> tuple[str, str, str]:
    name = re.sub(r"\[[^\]]*\]", "", Path(cut_video).stem).strip()
    match = EPISODE_NAME_PATTERN.match(name)
    if not match:
        die(f"Could not read series, episode number and title from: {Path(cut_video).name}")
    return match["series"].strip(), match["number"], match["title"].strip()


def slugify(text: str, separator: str) -> str:
    return re.sub(r"[^a-z0-9]+", separator, text.lower().replace("'", "")).strip(separator)


def find_episode_subtitles(cut_video: str, episodes_root: Path) -> tuple[Path, bool]:
    """Returns the episode's generated ASS and whether it's a retimed one.
    A retimed file wins over an AI-generated one if both exist."""
    stem = Path(cut_video).stem
    for tag, retimed in (("[Retimed Subs]", True), ("[AI Subs]", False)):
        name = re.sub(r"\[Subs Missing\]", tag, stem)
        candidate = episodes_root / name / f"{name}.ass"
        if candidate.is_file():
            return candidate, retimed
    die(f"No generated subtitles found under {episodes_root} for: {Path(cut_video).name}")


def release_episode(
    cut_video: str,
    episodes_root: Path,
    env: dict[str, str],
    subs: str | None = None,
    dry_run: bool = False,
) -> None:
    series, number, title = parse_episode_name(cut_video)
    if subs:
        subs_path, retimed = Path(subs), "[AI Subs]" not in Path(subs).name
        if not subs_path.is_file():
            die(f"Subtitle file not found: {subs_path}")
    else:
        subs_path, retimed = find_episode_subtitles(cut_video, episodes_root)

    tag = f"{slugify(series, '-')}-{number}-{slugify(title, '-')}"
    release_title = f"{series} {number}: {title}"
    kind = "retimed" if retimed else "AI-generated"
    notes = f"{series} Episode {number} with {kind} English subtitles and ASS styling."
    asset_name = f"{slugify(series, '_')}_{number}_{slugify(title, '_')}_{'retimed' if retimed else 'ai'}_subs.ass"

    log(f"Tag:   {tag}")
    log(f"Title: {release_title}")
    log(f"Notes: {notes}")
    log(f"Asset: {asset_name} <- {subs_path}")
    if dry_run:
        log("Dry run: nothing was published")
        return
    if not command_exists("gh"):
        die("Missing required command: gh")

    with tempfile.TemporaryDirectory() as tmp:
        asset = Path(tmp) / asset_name
        shutil.copy2(subs_path, asset)
        try:
            run_command(["gh", "release", "view", tag], env=env, capture=True)
            exists = True
        except subprocess.CalledProcessError:
            exists = False
        if exists:
            log(f"Release {tag} already exists, replacing {asset_name} on it")
            run_command(["gh", "release", "upload", tag, str(asset), "--clobber"], env=env)
        else:
            run_command(
                ["gh", "release", "create", tag, str(asset), "--title", release_title, "--notes", notes],
                env=env,
            )
    log(f"Published {asset_name} to release {tag}")
