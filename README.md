# Fun Pace Subtitle Pipeline

This repo provides a Nix-flake-backed workflow for getting English subtitles onto Fun Pace episodes.
Fun Pace is a filler-focused companion project to [One Pace](https://github.com/one-pace/one-pace-public-subtitles), and it's not limited to any one mini-series (Straw Hats Daily is just one of several Fun Pace releases). Fun Pace episodes ship as Dual Audio (English dub + Japanese) but with no subtitle track at all (`[Subs Missing]`).
See [this One Pace + Fun Pace viewing guide](https://gist.github.com/ang3lo-azevedo/0e50cdc0954347854919aa9df24fbf6b) for the broader context this project fits into, and the public One Pace subtitle mirror at https://github.com/one-pace/one-pace-public-subtitles as the reference base for naming conventions, terminology, and subtitle style.

There are two ways this repo fills that gap, and they're not equally preferred:
- **Subtitle retiming** (the main approach): most Fun Pace releases are trimmed-down cuts of an episode that already has a perfectly good, human-translated subtitle track somewhere, just timed to the wrong (uncut) version of the video. Retiming that existing track onto the cut reuses a real translation instead of generating a new one, so it's the default whenever a matching uncut source episode is available. See "Subtitle retiming" below.
- **Generating subtitles from scratch** (the fallback): when no existing subtitles cover a scene at all, there's nothing to retime, so this transcribes the Japanese audio with Whisper and translates it to English instead. See "Generating subtitles from scratch" below.

## Folder structure

- [input/episodes/](input/episodes/): Fun Pace source MKVs. `run` also symlinks the generated ASS here next to its video so media players auto-load it.
- [input/source-episodes/](input/source-episodes/): the uncut original episode(s) a Fun Pace release was cut down from, only kept for releases we actually have a matching one for. Their own embedded subtitle track is what gets retimed (see "Subtitle retiming" below). No separate subtitle file is needed.
- [input/styles/](input/styles/): style reference ASS files.
- [input/fonts/](input/fonts/): fonts attached during mux.
- [output/episodes/](output/episodes/): one folder per episode holding both the generated ASS and the muxed MKV.
- [scripts/fun-pace-subs.py](scripts/fun-pace-subs.py): CLI entrypoint (argument parsing and orchestration only).
- [scripts/lib/](scripts/lib/): the actual pipeline logic, split by concern:
	- `retiming.py`: the main approach. Aligns a cut against its uncut source and retimes the source's own existing subtitles onto it (see "Subtitle retiming" below).
	- `audio.py`: picks the right Dual Audio track and extracts it with ffmpeg.
	- `transcription.py`: the fallback approach's engine: faster-whisper/WhisperX (GPU transcription, CPU alignment, per-segment translation). See "Generating subtitles from scratch" below.
	- `rephrasing.py`: the fallback approach's local LLM naturalness pass (see "Rephrasing pass" below).
	- `muxing.py`: style reference resolution and mkvmerge muxing.
	- `common.py`: small shared helpers (logging, subprocess wrapper, etc).
	- `normalize_srt.py`, `style_srt.py`, `srt_to_ass.py`: also usable standalone via the `normalize`/`style`/`assify` subcommands.

Example output path:
- [output/episodes/[Episode Name with [AI Subs]]/[Episode Name with [AI Subs]].ass and .mkv](output/episodes/)

## Subtitle retiming (the main approach)

Most Fun Pace releases are trimmed-down cuts of an episode that already has a perfectly good, human-translated subtitle track, just timed to the wrong (uncut) version of the video. `scripts/lib/retiming.py` takes advantage of that: instead of transcribing anything, it figures out which parts of the uncut source survived into the cut, and retimes the source episode's own existing subtitles onto those surviving parts. No separate subtitle file is needed. DVD/BD-sourced releases like this ship their subtitles embedded directly in the video, and that embedded track is exactly what gets used.

How it works:
1. Pull the English subtitle stream straight out of the uncut source episode's own MKV.
2. That stream almost always carries more than plain dialogue: karaoke-timed opening/ending lyrics, typeset logo effects, sometimes a romanized (not translated) lyrics track. None of those are usable as a normal caption, so any style is dropped if its lines carry per-syllable karaoke timing, switch into vector-drawing mode for a typeset effect, use the subtitle format's Effect field (conventionally reserved for exactly this kind of styling), or if the style's name plainly says "romaji". What's left is plain dialogue, on-screen text, and any already-translated (not transliterated) lyric lines - confirmed directly against a real release before settling on this rule: it landed on exactly the small, clean set of styles actually worth keeping, out of several thousand karaoke-timing lines in the same file.
3. Extract the Japanese audio from both the uncut source episode and the Fun Pace cut.
4. Slide a window across the cut's audio and cross-correlate each window against the full source audio track to find its best-matching position. A cut only removes footage, it doesn't alter the audio of what's kept, so this match is exact wherever both tracks share the same content.
5. Group consecutive windows that share the same offset (source time minus cut time) into a block. A jump in that offset marks a cut boundary between one kept scene and the next. This reconstructs an edit decision list automatically, without needing one to already exist.
6. Slice the surviving subtitle lines down to only the ones that fall inside a kept block, and shift each one by that block's offset so it lands correctly on the cut's own timeline. A cue that spans a cut boundary is clipped to whichever side it mostly belongs to. One that barely survives the cut at all is dropped rather than left flickering on screen for a fraction of a second.
7. Restyle every surviving line onto this project's own One Pace-style reference (the same one `run` uses), rather than keeping whatever styling the source release shipped with, so retimed episodes look consistent with generated ones. Which of the surviving styles is an opening/ending lyric line (styled as Karaoke, same as the fallback approach below) versus regular dialogue is decided from the source's own style name, not from where a cue happens to land on the cut's timeline: a title card or a narration line can sit right at the edge of an episode too, exactly like a song does, so guessing from timing alone isn't reliable once cues have already been shifted around by retiming.

This was confirmed directly against real files before being built out into `retiming.py`: the offset blocks came out clean and stable, with clear jumps exactly where a cut boundary would be expected, and correlation scores that stayed well above noise across the whole episode.

Usage:

```text
scripts/fun-pace-subs.py retime "input/source-episodes/<uncut episode>.mkv" "input/episodes/<fun pace cut>.mkv" "output.ass"
```

`input/source-episodes/` only needs to hold the specific uncut episode(s) a given Fun Pace release actually draws from. There's no reason to keep an entire series' worth of source video around when only a handful of episodes are in use for a given release.

## Generating subtitles from scratch (the fallback approach)

Retiming only works when a matching uncut source episode is available. When no existing subtitles cover a scene at all, there's nothing to retime, so this transcribes the Japanese audio with Whisper and translates it to English instead:

1. Extracts the Japanese audio track from a Dual Audio MKV (override with `--language en` to use the English dub track instead).
2. Transcribes that audio **in its own language first** (not translated yet) via `faster-whisper` on the ROCm GPU path, or WhisperX as a CPU fallback.
3. Refines segment timestamps with a CPU-only WhisperX forced-alignment pass. This only works because step 2's transcript is still in the source language: alignment matches transcript words to audio phonemes, so translated text can't be aligned against source-language audio.
4. Translates each aligned segment to English independently, keeping its precise timestamp from step 3 (see "Why two stages" below).
5. Normalizes One Piece terminology such as `Zolo -> Zoro` and `Gold Roger -> Gol D. Roger`.
6. Rephrases each cue with a small local LLM to read naturally instead of like a literal machine translation (see "Rephrasing pass" below). Skip with `--no-rephrase`.
7. Styles and wraps subtitles to 1-2 lines per cue, merging any back-to-back duplicate cues from transcription hallucinations.
8. Converts SRT to ASS using a reference style set, with OP/ED cues styled as karaoke text distinct from dialogue.
9. Muxes subtitles + fonts into a new MKV (enabled by default for `run`).

### Why two stages (transcribe+align, then translate)

Earlier versions of this pipeline ran Whisper's `translate` task directly over the full episode in one pass. That has two real problems, both confirmed in practice:
- **Timing**: Whisper's own segmentation during `translate` is unreliable over long audio. Some cues ended up spanning 30-50+ seconds of screen time while several lines of actual dialogue happened underneath.
- **Context**: translating short, isolated clips (needed to fix the timing problem) loses the surrounding-dialogue context a longer pass would have, which shows up as leftover untranslated Japanese words, dropped honorifics, and character names getting mistranslated (e.g. "Nami" as "Minami").

The current pipeline transcribes in the source language first (accurate segmentation, since there's no cross-language ambiguity), aligns those segments to the audio (precise, audio-locked timestamps), and only then translates each segment. A rolling window of the last couple of translated lines, plus a character-name glossary, gets fed in as context, which meaningfully improves name/terminology consistency without re-merging segments and breaking the timing fix.

This is a real, ongoing quality tradeoff of translating fully offline with Whisper rather than a dedicated MT/LLM translation step, and the reason retiming above is preferred whenever it's available. Expect occasional literal or awkward phrasing on idioms Whisper doesn't have context to translate naturally. `data/one-piece-terms.tsv` patches the most common recurring cases (see the `senchou -> Captain`, `mr. nami -> Nami-san` entries for examples of that pattern).

### Rephrasing pass

Whisper's `translate` task gets the meaning right but reads stiff and overly literal, since it's a speech model's built-in translation head, not a fluency-tuned language model. `scripts/lib/rephrasing.py` runs a small local instruction-tuned LLM (`Qwen2.5-3B-Instruct`, quantized GGUF) over each already-translated, already-terminology-normalized cue and asks it to rewrite the line more naturally while keeping the meaning, names, and honorifics unchanged. A short rolling window of already-rewritten lines is fed back in as prior conversation turns so nearby cues stay stylistically consistent instead of each being rewritten in isolation.

It runs via `llama-cpp-python`, built from source with the ROCm/HIP backend when a GPU is available (falling back to a plain CPU build otherwise), and the model itself is downloaded once from Hugging Face and cached under `~/.cache/fun-pace-subs/rephrase-model/`. The build itself is cached in a dedicated venv under `~/.cache/fun-pace-subs/rephrase-venv/`, keyed by the build variant (GPU vs CPU) rather than left to `uv`'s own tool cache, which turned out to key only off the package version and not the build flags used - it kept silently reusing a stale CPU-only build even after the flags changed. Skip the whole pass with `--no-rephrase` if you want the faster, more literal output instead.

## Usage

This section covers the `run` command, i.e. the fallback approach (generating subtitles from scratch). See "Subtitle retiming" above for the `retime` command, used whenever a matching uncut source episode is available.

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
- `llama-cpp-python` (the rephrasing pass, see above. Built into a dedicated venv rather than run via a bare `uvx` call.)
- `scipy` (the audio cross-correlation used by subtitle retiming)
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
