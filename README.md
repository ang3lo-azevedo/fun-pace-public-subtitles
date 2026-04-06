# Fun Pace Subtitle Pipeline

This repo provides a Nix-flake-backed workflow for generating English subtitles for custom-cut One Piece MKVs.
It is intended to be used alongside the public One Pace subtitle mirror at https://github.com/one-pace/one-pace-public-subtitles as the reference base for naming conventions, terminology, and subtitle style.

## Folder structure

- [input/](input/): source MKVs, style reference ASS files, and fonts.
- [input/fonts/](input/fonts/): fonts attached during mux.
- [output/subtitles/](output/subtitles/): generated ASS subtitles.
- [output/](output/): final muxed MKVs.

Example output paths:
- [output/subtitles/[Episode Name with [AI Subs]].ass](output/subtitles/)
- [output/[Episode Name with [AI Subs]].mkv](output/)

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

For AMD GPUs (ROCm), the script now uses `faster-whisper` directly with the ROCm CTranslate2 wheel so transcription can run on the GPU without WhisperX's Torch alignment stack.
You can still override the runtime with `--device`, `--compute-type`, and `--batch-size` for non-ROCm paths.

## Dependencies

### Managed by Nix (from `flake.nix`)

- `python3` (plus `nltk` in the dev shell)
- `uv` / `uvx` (used to run `whisperx` and `faster-whisper` tools)
- `ffmpeg` + `ffprobe` (`ffmpeg_7`)
- `mkvtoolnix`
- core shell tooling: `coreutils`, `gawk`, `gnused`
- runtime libraries: `zlib`, `zstd`, `stdenv.cc.cc.lib`
- ROCm runtime libs wired into `LD_LIBRARY_PATH`:
	- `rocmPackages.clr`
	- `rocmPackages.rocm-runtime`
	- `rocmPackages.hipblas`
	- `rocmPackages.hiprand`
	- `rocmPackages.rocblas`
	- `rocmPackages.hipsparse`
	- `rocmPackages.hipsolver`
	- `rocmPackages.miopen`

### Python packages resolved dynamically by `uvx`

- `faster-whisper` (preferred ROCm transcription path)
- `whisperx` (fallback / non-ROCm path)
- ROCm `ctranslate2` wheel downloaded from OpenNMT releases and cached under:
	- `~/.cache/fun-pace-subs/ctranslate2-rocm/`

### System prerequisites (outside this repo)

- On Linux with AMD GPU:
	- ROCm-capable kernel/driver stack must be installed and working (`rocminfo` should list your GPU).
	- `/dev/kfd` access is required for GPU execution.

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
python3 scripts/fun-pace-subs mux "input.mkv" "output/subtitles/<episode with [AI Subs]>.ass"
```

Extract source ASS from an MKV for style comparison (optional):

```text
python3 scripts/fun-pace-subs extract-ass "input/[One Pace][127-129] Little Garden 05 [1080p][51105EBB].mkv" "output/little-garden.source.ass"
```

Generate a matched-style ASS from SRT using a chosen style block:

```text
python3 scripts/fun-pace-subs assify "output/episode.styled.srt" "output/subtitles/episode [AI Subs].ass" --style-from-ass "input/alabasta 18 en.ass"
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
- On ROCm systems, the script prefers `faster-whisper` via `uvx --from faster-whisper python` with the ROCm CTranslate2 wheel.
- The first `uvx` run will be slower because it resolves and prepares the needed environment.
