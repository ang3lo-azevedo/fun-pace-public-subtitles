# Fun Pace Subtitle Pipeline

This repo provides a Nix-flake-backed workflow for generating English subtitles for custom-cut One Piece MKVs.
It is intended to be used alongside the public One Pace subtitle mirror at https://github.com/one-pace/one-pace-public-subtitles as the reference base for naming conventions, terminology, and subtitle style.

## Folder structure

- [input/](input/): source MKVs and style reference ASS files.
- [output/fonts/](output/fonts/): fonts attached during mux.
- [output/subtitles/](output/subtitles/): generated ASS subtitles.
- [output/](output/): final muxed MKVs, organized per episode folder.

Example output paths:
- [output/subtitles/[Episode Name]/[Episode Name].ass](output/subtitles/)
- [output/[Episode Name]/[Episode Name with [AI Subs]].mkv](output/)

## What it does

1. Extracts the English dub audio track from an MKV.
2. Sends that audio through WhisperX to generate an SRT.
3. Normalizes One Piece terminology such as `Zolo -> Zoro` and `Lufi -> Luffy`.
4. Styles and wraps subtitles to 1-2 lines per cue.
5. Converts SRT to ASS using a reference style set.
6. Muxes subtitles + fonts into a new MKV (enabled by default for `run`).

## Usage

Cross-platform CLI entrypoint:

```text
python3 scripts/fun-pace-subs run <input.mkv>
```

If you want reproducible tool dependencies via Nix on Linux/macOS:

```text
nix develop path:$PWD --no-write-lock-file -c scripts/fun-pace-subs run "input/[FunPace] Straw Hats Daily 01 - Chopper's Concoctions [Dual Audio][Subs Missing][1080p].mkv" --model large-v3
```

If you only want ASS output (skip mux):

```text
nix develop path:$PWD --no-write-lock-file -c scripts/fun-pace-subs run "input/[FunPace] Straw Hats Daily 01 - Chopper's Concoctions [Dual Audio][Subs Missing][1080p].mkv" --no-mux
```

For AMD GPUs (ROCm), the script now auto-selects GPU-friendly WhisperX settings (`device=cuda`, `compute_type=float16`, `batch_size=16`) when ROCm is available, and falls back to CPU-safe defaults otherwise.
You can override these at runtime with `--device`, `--compute-type`, and `--batch-size`.

## Style reference behavior

Default style reference for `run`:
- [input/alabasta 18 en.ass](input/alabasta%2018%20en.ass)

Override per run:

```text
scripts/fun-pace-subs run "input/episode.mkv" --style-reference-ass "input/another-style.ass"
```

Fallback behavior if no explicit/default style reference is available:
- Tries to extract ASS style data from subtitle streams in the input MKV.

Music styling behavior:
- Opening/music cues (early timeline) are assigned to `Karaoke` style when available in the active style set.
- If `Karaoke` is not present, the converter falls back to `Translation`, then to the main dialogue style.

If you want to step through the pipeline manually:

```text
python3 scripts/fun-pace-subs extract "input.mkv"
python3 scripts/fun-pace-subs transcribe "input.wav"
python3 scripts/fun-pace-subs normalize "input.srt"
python3 scripts/fun-pace-subs style "input.srt"
python3 scripts/fun-pace-subs assify "input.srt"
python3 scripts/fun-pace-subs mux "input.mkv" "output/subtitles/<episode>/<episode>.ass"
```

Extract source ASS from an MKV for style comparison (optional):

```text
python3 scripts/fun-pace-subs extract-ass "input/[One Pace][127-129] Little Garden 05 [1080p][51105EBB].mkv" "output/little-garden.source.ass"
```

Generate a matched-style ASS from SRT using a chosen style block:

```text
python3 scripts/fun-pace-subs assify "output/episode.styled.srt" "output/subtitles/episode/episode.ass" --style-from-ass "input/alabasta 18 en.ass"
```

## Output naming

When muxing, filenames are rewritten from `[Subs Missing]` to `[AI Subs]`.

Example:
- Input: `[FunPace] ... [Subs Missing][1080p].mkv`
- Output: `[FunPace] ... [AI Subs][1080p].mkv`

## Notes

- Paths with spaces are handled by quoting in the scripts.
- The default terminology map lives in [data/one-piece-terms.tsv](data/one-piece-terms.tsv).
- The public One Pace subtitle mirror is the source to mine for additional terminology and subtitle-specific naming conventions.
- The flake uses `uv` and the script can run WhisperX via `uvx --from whisperx whisperx` when a direct `whisperx` binary is not available.
- The first `uvx` run will be slower because it resolves and prepares the WhisperX environment.
- For `uvx` fallback, the script picks an explicit torch backend (`rocm6.4` on AMD+ROCm, `cpu` otherwise) and you can override it with `FUN_PACE_UV_TORCH_BACKEND`.
