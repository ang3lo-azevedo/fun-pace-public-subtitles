#!/usr/bin/env python3
"""CLI entry point for the subtitle pipeline: argument parsing and orchestration
only. The actual work (audio extraction, transcription/translation, ASS
conversion, muxing) lives in scripts/lib/ - see lib/transcription.py for the
core engine and lib/audio.py + lib/muxing.py for the ffmpeg/mkvmerge glue.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_STYLE_REFERENCE = (PROJECT_ROOT / "input" / "styles" / "jaya 01 en.ass").resolve()
# Nix's build (see flake.nix) replaces these placeholder strings with real
# paths baked into the packaged script. Running straight from a checkout
# instead falls through to the FUN_PACE_DEFAULT_* env vars or, failing that,
# the project-relative defaults below.
DEFAULT_TERMS_PLACEHOLDER = "@DEFAULT_TERMS_FILE@"
DEFAULT_LD_LIBRARY_PATH_PLACEHOLDER = "@DEFAULT_LD_LIBRARY_PATH@"

# scripts/ itself onto sys.path so `lib` resolves as a plain package,
# regardless of the caller's own working directory.
sys.path.insert(0, str(SCRIPT_DIR))

from lib.audio import extract_audio, normalize_target_language
from lib.common import command_exists, die, log, run_command, stem_for
from lib.muxing import (
    create_media_symlink,
    extract_ass_from_mkv,
    mux_subtitles,
    resolve_style_reference_ass,
)
from lib.releasing import release_episode
from lib.rephrasing import rephrase_srt
from lib.retiming import retime_episode_subtitles
from lib.transcription import transcribe_audio


def resolve_default_terms_file() -> str | None:
    default_terms = os.environ.get("FUN_PACE_DEFAULT_TERMS_FILE", DEFAULT_TERMS_PLACEHOLDER)
    if default_terms == DEFAULT_TERMS_PLACEHOLDER:
        default_terms = str((SCRIPT_DIR / "../data/one-piece-terms.tsv").resolve())
    return default_terms if Path(default_terms).is_file() else None


def prepare_subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    default_ld_path = env.get("FUN_PACE_DEFAULT_LD_LIBRARY_PATH", DEFAULT_LD_LIBRARY_PATH_PLACEHOLDER)
    if default_ld_path == DEFAULT_LD_LIBRARY_PATH_PLACEHOLDER:
        default_ld_path = ""
    if default_ld_path:
        current = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{default_ld_path}:{current}" if current else default_ld_path
    return env


def normalize_srt(input_srt: str, output_srt: str, terms_file: str | None, env: dict[str, str]) -> None:
    """This, style_srt, and convert_srt_to_ass shell out to scripts/lib/*.py as
    separate processes (unlike the rest of the pipeline, which imports lib
    modules directly) because those three are also meant to be run standalone
    via `fun-pace-subs normalize|style|assify` - keeping them as independent
    CLI scripts avoids having two different code paths for the same behavior.
    """
    args = [
        sys.executable,
        str(SCRIPT_DIR / "lib" / "normalize_srt.py"),
        "--input",
        input_srt,
        "--output",
        output_srt,
    ]
    if terms_file:
        args.extend(["--terms-file", terms_file])
    run_command(args, env=env)


def style_srt(input_srt: str, output_srt: str, terms_file: str | None, env: dict[str, str]) -> None:
    args = [
        sys.executable,
        str(SCRIPT_DIR / "lib" / "style_srt.py"),
        "--input",
        input_srt,
        "--output",
        output_srt,
    ]
    if terms_file:
        args.extend(["--terms-file", terms_file])
    run_command(args, env=env)


def convert_srt_to_ass(input_srt: str, output_ass: str, env: dict[str, str], style_from_ass: str | None = None) -> None:
    args = [
        sys.executable,
        str(SCRIPT_DIR / "lib" / "srt_to_ass.py"),
        "--input",
        input_srt,
        "--output",
        output_ass,
    ]
    if style_from_ass:
        args.extend(["--style-from-ass", style_from_ass])
    run_command(args, env=env)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fun-pace-subs")
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run")
    run_cmd.add_argument("input_video")
    run_cmd.add_argument("--terms-file")
    run_cmd.add_argument("--model", default="large-v3")
    run_cmd.add_argument("--compute-type", default="auto")
    run_cmd.add_argument("--device", default="auto")
    run_cmd.add_argument("--batch-size", default="auto")
    run_cmd.add_argument("--language", default="ja", help="Source audio language (default: ja, the Japanese track)")
    run_cmd.add_argument(
        "--task", choices=["transcribe", "translate"], default="translate",
        help="'translate' outputs English text from the source-language audio (default). "
        "'transcribe' outputs text in the source language itself",
    )
    run_cmd.add_argument("--track-index", type=int)
    run_cmd.add_argument("--output-dir")
    run_cmd.add_argument("--style-reference-ass")
    run_cmd.add_argument("--no-mux", action="store_true")
    run_cmd.add_argument("--mux", action="store_true")
    run_cmd.add_argument("--keep-audio", action="store_true")
    run_cmd.add_argument(
        "--no-rephrase", action="store_true",
        help="Skip the LLM naturalness pass and keep Whisper's literal translation as-is",
    )

    extract_cmd = sub.add_parser("extract")
    extract_cmd.add_argument("input_video")
    extract_cmd.add_argument("output_audio", nargs="?")
    extract_cmd.add_argument("--track-index", type=int)
    extract_cmd.add_argument("--language", default="ja", help="Preferred audio track language (default: ja)")

    transcribe_cmd = sub.add_parser("transcribe")
    transcribe_cmd.add_argument("input_audio")
    transcribe_cmd.add_argument("output_dir", nargs="?")
    transcribe_cmd.add_argument("--model", default="large-v3")
    transcribe_cmd.add_argument("--compute-type", default="auto")
    transcribe_cmd.add_argument("--device", default="auto")
    transcribe_cmd.add_argument("--batch-size", default="auto")
    transcribe_cmd.add_argument("--language", default="ja", help="Source audio language (default: ja, the Japanese track)")
    transcribe_cmd.add_argument(
        "--task", choices=["transcribe", "translate"], default="translate",
        help="'translate' outputs English text from the source-language audio (default). "
        "'transcribe' outputs text in the source language itself",
    )
    transcribe_cmd.add_argument("--force-device", choices=["auto", "cuda", "cpu"], default="auto", help="Skip CPU fallback and fail if device unavailable")

    normalize_cmd = sub.add_parser("normalize")
    normalize_cmd.add_argument("input_srt")
    normalize_cmd.add_argument("output_srt", nargs="?")
    normalize_cmd.add_argument("--terms-file")

    style_cmd = sub.add_parser("style")
    style_cmd.add_argument("input_srt")
    style_cmd.add_argument("output_srt", nargs="?")
    style_cmd.add_argument("--terms-file")

    rephrase_cmd = sub.add_parser("rephrase")
    rephrase_cmd.add_argument("input_srt")
    rephrase_cmd.add_argument("output_srt", nargs="?")

    assify_cmd = sub.add_parser("assify")
    assify_cmd.add_argument("input_srt")
    assify_cmd.add_argument("output_ass", nargs="?")
    assify_cmd.add_argument("--style-from-ass")

    extract_ass_cmd = sub.add_parser("extract-ass")
    extract_ass_cmd.add_argument("input_video")
    extract_ass_cmd.add_argument("output_ass", nargs="?")
    extract_ass_cmd.add_argument("--stream-index", type=int)

    mux_cmd = sub.add_parser("mux")
    mux_cmd.add_argument("input_video")
    mux_cmd.add_argument("input_subs")
    mux_cmd.add_argument("output_mkv", nargs="?")

    retime_cmd = sub.add_parser("retime")
    retime_cmd.add_argument(
        "source_videos", nargs="+", metavar="source_video",
        help="Uncut source episode(s) with their own embedded subtitles (e.g. input/source-episodes/...). "
        "Pass every episode the cut draws from",
    )
    retime_cmd.add_argument("cut_video", help="The Fun Pace cut to retime the subtitles onto")
    retime_cmd.add_argument("--output-ass", help="Defaults to the episode's own folder under output/episodes/")
    retime_cmd.add_argument("--style-reference-ass", help="Defaults to the project's usual style reference")
    retime_cmd.add_argument(
        "--op-from",
        help="Path to ASS file whose OP lyrics replace the source's (e.g. input/styles/Hikari e.ass). "
        "Defaults to keeping the source's own translated OP lyrics",
    )
    retime_cmd.add_argument("--no-mux", action="store_true")

    release_cmd = sub.add_parser("release")
    release_cmd.add_argument("cut_video", help="The Fun Pace cut whose generated subtitles to publish as a GitHub release")
    release_cmd.add_argument("--subs", help="Defaults to the episode's generated ASS under output/episodes/")
    release_cmd.add_argument("--dry-run", action="store_true", help="Show what would be published without publishing")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    env = prepare_subprocess_env()

    terms_file = getattr(args, "terms_file", None) or resolve_default_terms_file()
    if terms_file and not Path(terms_file).is_file():
        die(f"Terms file not found: {terms_file}")

    if args.command == "run":
        # Full pipeline for one episode: extract audio -> transcribe/translate
        # -> normalize terminology -> wrap/style -> convert to ASS -> mux.
        # Each step's output feeds the next. See the individual `lib` modules
        # for what each stage actually does.
        input_video = args.input_video
        input_path = Path(input_video)
        mux_base_name = re.sub(r"\[Subs Missing\]", "[AI Subs]", input_path.stem)

        # Each episode gets its own folder holding both the generated ASS and the muxed MKV.
        episode_root = Path(args.output_dir).resolve() if args.output_dir else (PROJECT_ROOT / "output" / "episodes" / mux_base_name).resolve()
        episode_root.mkdir(parents=True, exist_ok=True)

        base_name = stem_for(input_video)
        ass_output_path = episode_root / f"{mux_base_name}.ass"
        ass_output = str(ass_output_path)

        media_symlink_path = input_path.with_suffix(".ass")
        muxed_output = str((episode_root / f"{mux_base_name}.mkv").resolve())
        fonts_dir = PROJECT_ROOT / "input" / "fonts"

        with tempfile.TemporaryDirectory() as tmp:
            extracted_audio = str(Path(tmp) / f"{base_name}.wav")
            normalized_srt = str(Path(tmp) / f"{base_name}.styled.srt")
            extract_audio(input_video, extracted_audio, args.track_index, env, normalize_target_language(args.language))

            raw_srt = transcribe_audio(
                extracted_audio,
                tmp,
                args.model,
                args.language,
                args.compute_type,
                args.device,
                args.batch_size,
                env,
                task=args.task,
            )

            normalize_srt(raw_srt, normalized_srt, terms_file, env)

            if not args.no_rephrase:
                # Runs after terminology normalization (clean names to rephrase around)
                # and before wrap/style (so line-length limits apply to the final,
                # rewritten text, not the pre-rewrite literal translation).
                if not rephrase_srt(normalized_srt, normalized_srt, env):
                    log("Continuing with the literal translation, unrephrased.")

            style_srt(normalized_srt, normalized_srt, terms_file, env)

            style_reference_ass = resolve_style_reference_ass(
                input_video,
                tmp,
                env,
                args.style_reference_ass,
                DEFAULT_STYLE_REFERENCE,
            )
            if style_reference_ass:
                log(f"Using MKV subtitle style reference: {Path(style_reference_ass).name}")

            convert_srt_to_ass(normalized_srt, ass_output, env, style_reference_ass)

            if args.keep_audio:
                shutil.copy2(extracted_audio, episode_root / f"{mux_base_name}.wav")

        log(f"Wrote ASS subtitles to {ass_output}")

        create_media_symlink(media_symlink_path, ass_output_path.resolve())
        log(f"Linked media subtitle path {media_symlink_path} -> {ass_output_path}")

        should_mux = not args.no_mux
        if should_mux:
            mux_subtitles(input_video, ass_output, muxed_output, env, fonts_dir=fonts_dir)
            log(f"Wrote muxed MKV to {muxed_output}")
        return

    if args.command == "extract":
        output_audio = args.output_audio or str(Path(args.input_video).with_suffix(".wav"))
        extract_audio(args.input_video, output_audio, args.track_index, env, normalize_target_language(args.language))
        log(f"Wrote extracted audio to {output_audio}")
        return

    if args.command == "transcribe":
        output_dir = args.output_dir or str(Path(args.input_audio).resolve().parent)
        raw_srt = transcribe_audio(
            args.input_audio,
            output_dir,
            args.model,
            args.language,
            args.compute_type,
            args.device,
            args.batch_size,
            env,
            force_device=args.force_device,
            task=args.task,
        )
        log(f"Wrote transcribed subtitles to {raw_srt}")
        return

    if args.command == "normalize":
        output_srt = args.output_srt or str(Path(args.input_srt).with_name(f"{stem_for(args.input_srt)}.normalized.srt"))
        normalize_srt(args.input_srt, output_srt, terms_file, env)
        log(f"Wrote normalized subtitles to {output_srt}")
        return

    if args.command == "style":
        output_srt = args.output_srt or str(Path(args.input_srt).with_name(f"{stem_for(args.input_srt)}.styled.srt"))
        style_srt(args.input_srt, output_srt, terms_file, env)
        log(f"Wrote styled subtitles to {output_srt}")
        return

    if args.command == "rephrase":
        output_srt = args.output_srt or str(Path(args.input_srt).with_name(f"{stem_for(args.input_srt)}.rephrased.srt"))
        if not rephrase_srt(args.input_srt, output_srt, env):
            die("LLM rephrasing failed")
        log(f"Wrote rephrased subtitles to {output_srt}")
        return

    if args.command == "assify":
        output_ass = args.output_ass or str(Path(args.input_srt).with_suffix(".ass"))
        convert_srt_to_ass(args.input_srt, output_ass, env, args.style_from_ass)
        log(f"Wrote ASS subtitles to {output_ass}")
        return

    if args.command == "extract-ass":
        output_ass = args.output_ass or str(Path(args.input_video).with_suffix(".source.ass"))
        extract_ass_from_mkv(args.input_video, output_ass, args.stream_index, env)
        log(f"Wrote extracted ASS subtitles to {output_ass}")
        return

    if args.command == "mux":
        mux_base_name = re.sub(r"\[Subs Missing\]", "[AI Subs]", stem_for(args.input_video))
        output_mkv = args.output_mkv or str((PROJECT_ROOT / "output" / "episodes" / mux_base_name / f"{mux_base_name}.mkv").resolve())
        mux_subtitles(args.input_video, args.input_subs, output_mkv, env, fonts_dir=PROJECT_ROOT / "input" / "fonts")
        log(f"Wrote muxed MKV to {output_mkv}")
        return

    if args.command == "retime":
        # Same output layout as `run`: one folder per episode holding both
        # the ASS and the muxed MKV.
        cut_path = Path(args.cut_video)
        if cut_path.suffix.lower() == ".ass":
            die("The last argument must be the Fun Pace cut; pass the output path with --output-ass")
        mux_base_name = re.sub(r"\[Subs Missing\]", "[Retimed Subs]", cut_path.stem)
        episode_root = (PROJECT_ROOT / "output" / "episodes" / mux_base_name).resolve()
        output_ass_path = Path(args.output_ass).resolve() if args.output_ass else episode_root / f"{mux_base_name}.ass"
        output_ass = str(output_ass_path)
        style_reference_ass = args.style_reference_ass or (str(DEFAULT_STYLE_REFERENCE) if DEFAULT_STYLE_REFERENCE.is_file() else None)
        op_from_ass = None
        if args.op_from:
            op_path = Path(args.op_from).expanduser().resolve()
            if not op_path.is_file():
                die(f"OP reference ASS not found: {op_path}")
            op_from_ass = str(op_path)
        # Hand-written lines for this cut, if any (see "Manual lines" in the README).
        manual_lines_path = PROJECT_ROOT / "data" / "manual-lines" / f"{cut_path.stem}.ass"
        manual_lines_ass = str(manual_lines_path) if manual_lines_path.is_file() else None
        if manual_lines_ass:
            log(f"Adding manual lines from {manual_lines_path}")
        if not retime_episode_subtitles(
            args.source_videos, args.cut_video, output_ass, env, style_reference_ass, op_from_ass, manual_lines_ass
        ):
            die("Retiming failed: no subtitle cues survived the alignment")
        log(f"Wrote retimed subtitles to {output_ass}")

        media_symlink_path = cut_path.with_suffix(".ass")
        create_media_symlink(media_symlink_path, output_ass_path)
        log(f"Linked media subtitle path {media_symlink_path} -> {output_ass_path}")

        if not args.no_mux:
            muxed_output = str(episode_root / f"{mux_base_name}.mkv")
            mux_subtitles(args.cut_video, output_ass, muxed_output, env, fonts_dir=PROJECT_ROOT / "input" / "fonts")
            log(f"Wrote muxed MKV to {muxed_output}")
        return

    if args.command == "release":
        release_episode(args.cut_video, PROJECT_ROOT / "output" / "episodes", env, args.subs, args.dry_run)
        return


if __name__ == "__main__":
    main()
