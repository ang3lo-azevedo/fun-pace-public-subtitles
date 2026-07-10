# Fun Pace Subtitle Pipeline

This repo provides a Nix-flake-backed workflow for generating English subtitles for Fun Pace episodes.
Fun Pace is a filler-focused companion project to [One Pace](https://github.com/one-pace/one-pace-public-subtitles), and it's not limited to any one mini-series (Straw Hats Daily is just one of several Fun Pace releases). Fun Pace episodes ship as Dual Audio (English dub + Japanese) but with no subtitle track at all (`[Subs Missing]`), so this pipeline transcribes the Japanese audio and translates it to English to fill that gap for sub viewers.
See [this One Pace + Fun Pace viewing guide](https://gist.github.com/ang3lo-azevedo/0e50cdc0954347854919aa9df24fbf6b) for the broader context this project fits into, and the public One Pace subtitle mirror at https://github.com/one-pace/one-pace-public-subtitles as the reference base for naming conventions, terminology, and subtitle style.

## Folder structure

- [input/episodes/](input/episodes/): source MKVs. `run` also symlinks the generated ASS here next to its video so media players auto-load it.
- [input/styles/](input/styles/): style reference ASS files.
- [input/fonts/](input/fonts/): fonts attached during mux.
- [output/episodes/](output/episodes/): one folder per episode holding both the generated ASS and the muxed MKV.
- [scripts/fun-pace-subs.py](scripts/fun-pace-subs.py): CLI entrypoint (argument parsing and orchestration only).
- [scripts/lib/](scripts/lib/): the actual pipeline logic, split by concern:
	- `audio.py`: picks the right Dual Audio track and extracts it with ffmpeg.
	- `transcription.py`: the faster-whisper/WhisperX engine (GPU transcription, CPU alignment, per-segment translation).
	- `muxing.py`: style reference resolution and mkvmerge muxing.
	- `common.py`: small shared helpers (logging, subprocess wrapper, etc).
	- `normalize_srt.py`, `style_srt.py`, `srt_to_ass.py`: also usable standalone via the `normalize`/`style`/`assify` subcommands.

Example output path:
- [output/episodes/[Episode Name with [AI Subs]]/[Episode Name with [AI Subs]].ass and .mkv](output/episodes/)

## What it does

1. Extracts the Japanese audio track from a Dual Audio MKV (override with `--language en` to use the English dub track instead).
2. Transcribes that audio **in its own language first** (not translated yet) via `faster-whisper` on the ROCm GPU path, or WhisperX as a CPU fallback.
3. Refines segment timestamps with a CPU-only WhisperX forced-alignment pass. This only works because step 2's transcript is still in the source language: alignment matches transcript words to audio phonemes, so translated text can't be aligned against source-language audio.
4. Translates each aligned segment to English independently, keeping its precise timestamp from step 3 (see "Why two stages" below).
5. Normalizes One Piece terminology such as `Zolo -> Zoro` and `Gold Roger -> Gol D. Roger`.
6. Styles and wraps subtitles to 1-2 lines per cue, merging any back-to-back duplicate cues from transcription hallucinations.
7. Converts SRT to ASS using a reference style set, with OP/ED cues styled as karaoke text distinct from dialogue.
8. Muxes subtitles + fonts into a new MKV (enabled by default for `run`).

### Why two stages (transcribe+align, then translate)

Earlier versions of this pipeline ran Whisper's `translate` task directly over the full episode in one pass. That has two real problems, both confirmed in practice:
- **Timing**: Whisper's own segmentation during `translate` is unreliable over long audio. Some cues ended up spanning 30-50+ seconds of screen time while several lines of actual dialogue happened underneath.
- **Context**: translating short, isolated clips (needed to fix the timing problem) loses the surrounding-dialogue context a longer pass would have, which shows up as leftover untranslated Japanese words, dropped honorifics, and character names getting mistranslated (e.g. "Nami" as "Minami").

The current pipeline transcribes in the source language first (accurate segmentation, since there's no cross-language ambiguity), aligns those segments to the audio (precise, audio-locked timestamps), and only then translates each segment. A rolling window of the last couple of translated lines, plus a character-name glossary, gets fed in as context, which meaningfully improves name/terminology consistency without re-merging segments and breaking the timing fix.

This is a real, ongoing quality tradeoff of translating fully offline with Whisper rather than a dedicated MT/LLM translation step. Expect occasional literal or awkward phrasing on idioms Whisper doesn't have context to translate naturally. `data/one-piece-terms.tsv` patches the most common recurring cases (see the `senchou -> Captain`, `mr. nami -> Nami-san` entries for examples of that pattern).

## Usage

Cross-platform CLI entrypoint:

```text
python3 scripts/fun-pace-subs.py run <input.mkv>
```

If you want reproducible tool dependencies via Nix on Linux/macOS:

```text
nix develop path:$PWD --no-write-lock-file -c scripts/fun-pace-subs.py run "input/episodes/[FunPace] Straw Hats Daily 01 - Chopper's Concoctions [Dual Audio][Subs Missing][1080p].mkv" --model large-v3
```

If you only want ASS output (skip mux):

```text
nix develop path:$PWD --no-write-lock-file -c scripts/fun-pace-subs.py run "input/episodes/[FunPace] Straw Hats Daily 01 - Chopper's Concoctions [Dual Audio][Subs Missing][1080p].mkv" --no-mux
```

To transcribe the source language without translating (e.g. Japanese subtitles for Japanese audio, or English subtitles for the English dub track):

```text
scripts/fun-pace-subs.py run "input/episodes/episode.mkv" --language en --task transcribe
```

For AMD GPUs (ROCm), the script uses `faster-whisper` directly with the ROCm CTranslate2 wheel so transcription can run on the GPU without WhisperX's Torch decode stack.
Two real, confirmed-in-practice fixes are baked in for this hardware path:
- `CT2_CUDA_ALLOCATOR=cub_caching` works around a ROCm LLVM codegen bug on RDNA4 GPUs (gfx1200/gfx1201) that otherwise crashes CTranslate2 with a memory access fault (see [OpenNMT/CTranslate2#2021](https://github.com/OpenNMT/CTranslate2/issues/2021)).
- The VAD (voice activity detection) threshold is tuned to `0.2` (down from faster-whisper's default `0.5`), because the default was silently dropping whole passages of quieter singing during OP/ED songs on full-length episodes.

If GPU transcription still fails for any other reason (e.g. out of VRAM), it automatically retries on CPU. You can override the runtime with `--device`, `--compute-type`, and `--batch-size` for non-ROCm paths.

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
- `whisperx` (fallback / non-ROCm path, and the CPU alignment step regardless of transcription path)
- ROCm `ctranslate2` wheel downloaded from OpenNMT releases and cached under:
	- `~/.cache/fun-pace-subs/ctranslate2-rocm/<version>/<python-abi-tag>/`

### System prerequisites (outside this repo)

- On Linux with AMD GPU:
	- ROCm-capable kernel/driver stack must be installed and working (`rocminfo` should list your GPU).
	- `/dev/kfd` access is required for GPU execution.

## Style reference behavior

Default style reference for `run`:
- [input/styles/alabasta 18 en.ass](input/styles/alabasta%2018%20en.ass)

Override per run:

```text
scripts/fun-pace-subs.py run "input/episodes/episode.mkv" --style-reference-ass "input/styles/another-style.ass"
```

Fallback behavior if no explicit/default style reference is available:
- Tries to extract ASS style data from subtitle streams in the input MKV.

Music styling behavior:
- Opening/music cues (early timeline) are assigned to `Karaoke` style when available in the active style set.
- If `Karaoke` is not present, the converter injects a small top-of-screen fallback Karaoke style rather than falling back to full-size dialogue styling.

If you want to step through the pipeline manually:

```text
python3 scripts/fun-pace-subs.py extract "input.mkv"
python3 scripts/fun-pace-subs.py transcribe "input.wav"
python3 scripts/fun-pace-subs.py normalize "input.srt"
python3 scripts/fun-pace-subs.py style "input.srt"
python3 scripts/fun-pace-subs.py assify "input.srt"
python3 scripts/fun-pace-subs.py mux "input.mkv" "output/episodes/<episode with [AI Subs]>/<episode with [AI Subs]>.ass"
```

Extract source ASS from an MKV for style comparison (optional):

```text
python3 scripts/fun-pace-subs.py extract-ass "input/episodes/[One Pace][127-129] Little Garden 05 [1080p][51105EBB].mkv" "output/little-garden.source.ass"
```

Generate a matched-style ASS from SRT using a chosen style block:

```text
python3 scripts/fun-pace-subs.py assify "output/episode.styled.srt" "output/episodes/episode [AI Subs]/episode [AI Subs].ass" --style-from-ass "input/styles/alabasta 18 en.ass"
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
