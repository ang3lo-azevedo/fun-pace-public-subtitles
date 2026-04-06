# Fun Pace Subtitle Pipeline

This repo provides a Nix-flake-backed workflow for generating English subtitles for custom-cut One Piece MKVs.
It is intended to be used alongside the public One Pace subtitle mirror at https://github.com/one-pace/one-pace-public-subtitles as the reference base for naming conventions, terminology, and subtitle style.

Current output behavior:
- `run` writes only a final `.ass` subtitle artifact.
- Subtitle artifacts are placed under `subtitles/<episode-name>/<episode-name>.ass`.
- A symlink is created in the media folder next to the MKV (`<episode-name>.ass`) pointing to that generated ASS file.

## What it does

1. Extracts the English dub audio track from an MKV.
2. Sends that audio through WhisperX to generate an SRT.
3. Normalizes One Piece terminology such as `Zolo -> Zoro` and `Lufi -> Luffy`.
4. Optionally remuxes the subtitles into a new MKV.

## Usage

Cross-platform CLI entrypoint:

```text
python3 scripts/fun-pace-subs run <input.mkv>
```

If you want reproducible tool dependencies via Nix on Linux/macOS:

```text
nix develop path:$PWD --no-write-lock-file -c python3 scripts/fun-pace-subs run "media/[FunPace] Straw Hats Daily 01 - Chopper's Concoctions [Dual Audio][Subs Missing][1080p].mkv" --model large-v3 --mux
```

For AMD GPUs (ROCm), the script now auto-selects GPU-friendly WhisperX settings (`device=cuda`, `compute_type=float16`, `batch_size=16`) when ROCm is available, and falls back to CPU-safe defaults otherwise.
You can override these at runtime with `--device`, `--compute-type`, and `--batch-size`.

If you want to step through the pipeline manually:

```text
python3 scripts/fun-pace-subs extract "input.mkv"
python3 scripts/fun-pace-subs transcribe "input.wav"
python3 scripts/fun-pace-subs normalize "input.srt"
python3 scripts/fun-pace-subs style "input.srt"
python3 scripts/fun-pace-subs assify "input.srt"
python3 scripts/fun-pace-subs mux "input.mkv" "input.srt"
```

Extract source ASS from an MKV for style comparison:

```text
python3 scripts/fun-pace-subs extract-ass "media/[One Pace][127-129] Little Garden 05 [1080p][51105EBB].mkv" "output/little-garden.source.ass"
```

Generate a matched-style ASS from SRT using that extracted style block:

```text
python3 scripts/fun-pace-subs assify "output/[FunPace] Straw Hats Daily 01 - Chopper's Concoctions [Dual Audio][Subs Missing][1080p].srt" "output/[FunPace] Straw Hats Daily 01 - Chopper's Concoctions [Dual Audio][Subs Missing][1080p].matched-style.ass" --style-from-ass "output/little-garden.source.ass"
```

## Notes

- Paths with spaces are handled by quoting in the scripts.
- The default terminology map lives in [data/one-piece-terms.tsv](data/one-piece-terms.tsv).
- The public One Pace subtitle mirror is the source to mine for additional terminology and subtitle-specific naming conventions.
- The flake uses `uv` and the script can run WhisperX via `uvx --from whisperx whisperx` when a direct `whisperx` binary is not available.
- The first `uvx` run will be slower because it resolves and prepares the WhisperX environment.
- For `uvx` fallback, the script picks an explicit torch backend (`rocm6.4` on AMD+ROCm, `cpu` otherwise) and you can override it with `FUN_PACE_UV_TORCH_BACKEND`.
