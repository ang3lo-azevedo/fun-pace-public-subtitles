"""Small, dependency-free helpers shared by every other lib module (and by
fun-pace-subs.py itself) so each of them doesn't have to redefine logging,
subprocess handling, etc.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def die(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def log(message: str) -> None:
    """Everything user-facing goes to stderr, keeping stdout free for any
    command that wants to pipe actual output (none currently do, but it
    matches the convention set by the underlying CLI tools we shell out to).
    """
    print(message, file=sys.stderr)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run_command(args: list[str], *, env: dict[str, str] | None = None, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """check=True: any failed subprocess raises CalledProcessError immediately
    rather than silently continuing with a bad exit code.
    """
    return subprocess.run(
        args,
        check=True,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def stem_for(path: str) -> str:
    return Path(path).stem
