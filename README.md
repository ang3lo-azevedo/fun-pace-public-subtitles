# Fun Pace Subtitle Pipeline

This repo provides a Nix-flake-backed workflow for getting English subtitles onto Fun Pace episodes.
Fun Pace is a filler-focused companion project to [One Pace](https://github.com/one-pace/one-pace-public-subtitles), and it's not limited to any one mini-series (Straw Hats Daily is just one of several Fun Pace releases). Fun Pace episodes ship as Dual Audio (English dub + Japanese) but with no subtitle track at all (`[Subs Missing]`).
See [this One Pace + Fun Pace viewing guide](https://gist.github.com/ang3lo-azevedo/0e50cdc0954347854919aa9df24fbf6b) for the broader context this project fits into, and the public One Pace subtitle mirror at https://github.com/one-pace/one-pace-public-subtitles as the reference base for naming conventions, terminology, and subtitle style.

There are two ways this repo fills that gap, and they're not equally preferred:
- **Subtitle retiming** (the main approach): most Fun Pace releases are trimmed-down cuts of an episode that already has a human-translated subtitle track somewhere, just timed to the wrong (uncut) version of the video. Retiming that existing track onto the cut reuses that translation instead of generating a new one, so it's the default whenever a matching uncut source episode is available. See "Subtitle retiming" below.
- **Generating subtitles from scratch** (the fallback): when no existing subtitles cover a scene at all, there's nothing to retime, so this transcribes the Japanese audio with Whisper and translates it to English instead. See "Generating subtitles from scratch" below.

## Contents

- [Folder structure](#folder-structure)
- [Subtitle retiming (the main approach)](#subtitle-retiming-the-main-approach)
  - [Retiming configuration](#retiming-configuration)
  - [Per-line placement](#per-line-placement)
  - [Overlap resolution](#overlap-resolution)
  - [Short inserts](#short-inserts)
  - [Missing source episodes](#missing-source-episodes)
  - [Manual lines](#manual-lines)
  - [Multiple source episodes](#multiple-source-episodes)
  - [OP (Opening) handling](#op-opening-handling)
  - [Known manual fixes (episode 01 only)](#known-manual-fixes-episode-01-only)
  - [Subtitle track naming](#subtitle-track-naming)
  - [Adding a new episode](#adding-a-new-episode)
  - [Releasing an episode](#releasing-an-episode)
  - [Retime usage](#retime-usage)
- [Generating subtitles from scratch (the fallback approach)](#generating-subtitles-from-scratch-the-fallback-approach)
  - [Why two stages (transcribe+align, then translate)](#why-two-stages-transcribealign-then-translate)
  - [Rephrasing pass](#rephrasing-pass)
- [Usage](#usage)
- [Dependencies](#dependencies)
  - [Managed by Nix (from `flake.nix`)](#managed-by-nix-from-flakenix)
  - [Python packages resolved dynamically by `uvx`](#python-packages-resolved-dynamically-by-uvx)
  - [System prerequisites (outside this repo)](#system-prerequisites-outside-this-repo)
- [Style reference behavior](#style-reference-behavior)
- [Output naming](#output-naming)
- [Notes](#notes)

## Folder structure

- [input/episodes/](input/episodes/): Fun Pace source MKVs. `run` and `retime` also symlink the generated ASS here next to its video so media players auto-load it.
- [input/source-episodes/](input/source-episodes/): the uncut original episode(s) a Fun Pace release was cut down from, only kept for releases we actually have a matching one for. Their own embedded subtitle track is what gets retimed (see "Subtitle retiming" below). No separate subtitle file is needed.
- [input/styles/](input/styles/): style reference ASS files.
  - `jaya 01 en.ass`: default style reference for dialogue (`Main-207-`, `Narrator-207-`, etc.). No OP styles here.
  - `Hikari e.ass`, `Bon Voyage.ass`: optional OP lyrics sources for `retime --op-from` (one per opening), from [One Pace](https://github.com/one-pace/one-pace-public-subtitles/), providing both the translated text (romaji + English) and the `Karaoke`/`Translation` style definitions for song lyrics.
- [input/fonts/](input/fonts/): fonts attached during mux. All sourced from the One Pace repo (`main/Other/Common Fonts/`).
- [output/episodes/](output/episodes/): one folder per episode holding both the generated ASS and the muxed MKV.
- `data/manual-lines/`: hand-written subtitle lines per cut, for the few spots `retime` can't cover (see "Manual lines" below). Create it when first needed.
- [scripts/fun-pace-subs.py](scripts/fun-pace-subs.py): CLI entrypoint (argument parsing and orchestration only).
- [scripts/lib/](scripts/lib/): the actual pipeline logic, split by concern:
	- `retiming.py`: the main approach. Aligns a cut against its uncut source episode(s) and retimes the source's own existing subtitles onto it (see "Subtitle retiming" below).
	- `audio.py`: picks the right Dual Audio track and extracts it with ffmpeg.
	- `transcription.py`: the fallback approach's engine: faster-whisper/WhisperX (GPU transcription, CPU alignment, per-segment translation). See "Generating subtitles from scratch" below.
	- `rephrasing.py`: the fallback approach's local LLM naturalness pass (see "Rephrasing pass" below).
	- `muxing.py`: style reference resolution and mkvmerge muxing.
	- `releasing.py`: publishes an episode's subtitle file as a GitHub release (see "Releasing an episode" below).
	- `common.py`: small shared helpers (logging, subprocess wrapper, etc).
	- `normalize_srt.py`, `style_srt.py`, `srt_to_ass.py`: also usable standalone via the `normalize`/`style`/`assify` subcommands.

Example output path:
- [output/episodes/[Episode Name with [AI Subs]]/[Episode Name with [AI Subs]].ass and .mkv](output/episodes/) for `run`
- [output/episodes/[Episode Name with [Retimed Subs]]/[Episode Name with [Retimed Subs]].ass and .mkv](output/episodes/) for `retime`

## Subtitle retiming (the main approach)

Most Fun Pace releases are trimmed-down cuts of an episode that already has a human-translated subtitle track, just timed to the wrong (uncut) version of the video. `scripts/lib/retiming.py` takes advantage of that: instead of transcribing anything, it figures out which parts of the uncut source survived into the cut, and retimes the source episode's own existing subtitles onto those surviving parts. No separate subtitle file is needed. DVD/BD-sourced releases like this ship their subtitles embedded directly in the video, and that embedded track is exactly what gets used.

How it works:
1. Pull the English subtitle stream straight out of each uncut source episode's own MKV.
2. That stream almost always carries more than plain dialogue: karaoke-timed opening/ending lyrics, typeset logo effects, sometimes a romanized (not translated) lyrics track. None of those are usable as a normal caption, so any style is dropped if its lines carry per-syllable karaoke timing, switch into vector-drawing mode for a typeset effect, use the subtitle format's Effect field (conventionally reserved for exactly this kind of styling), or if the style's name plainly says "romaji". What's left is plain dialogue, on-screen text, and any already-translated (not transliterated) lyric lines.
   Within the styles that are kept, a line pinned to a position on screen or drawn as a shape (`\pos`, `\move`, `\clip`, `\p1`) is typesetting and is dropped too. Other override tags are ordinary dialogue styling and stay: italics for thoughts, `\an8` for a line at the top, `\q2` for wrapping. Font overrides are stripped, since they name fonts that aren't muxed here.
3. Extract the Japanese audio from each uncut source episode and from the Fun Pace cut.
4. Slide a window across the cut's audio and cross-correlate each window against each source's full audio track to find its best-matching position. A cut only removes footage, it doesn't alter the audio of what's kept, so this match is exact wherever both tracks share the same content.
5. Group consecutive windows that share the same offset (source time minus cut time) into a block. A jump in that offset marks a cut boundary between one kept scene and the next. This reconstructs an edit decision list automatically, without needing one to already exist.
6. Place each subtitle line on the cut's own timeline by searching for the audio under that line near where the blocks predict it (see "Per-line placement" below). A line whose audio isn't in the cut is dropped.
7. Restyle every surviving line onto this project's own One Pace-style reference (the same one `run` uses), rather than keeping whatever styling the source release shipped with, so retimed episodes look consistent with generated ones. Which of the surviving styles is an opening/ending lyric line (styled as Karaoke, same as the fallback approach below) versus regular dialogue is decided from the source's own style name, not from where a cue happens to land on the cut's timeline: a title card or a narration line can sit right at the edge of an episode too, exactly like a song does, so guessing from timing alone isn't reliable once cues have already been shifted around by retiming.

### Retiming configuration

The cross-correlation and subtitle placement use these constants in `retiming.py`:

| Constant | Value | Purpose |
|----------|-------|---------|
| `ALIGNMENT_WINDOW_SECONDS` | 15 | Window size for audio cross-correlation |
| `ALIGNMENT_STEP_SECONDS` | 10 | Step between windows |
| `BLOCK_OFFSET_TOLERANCE_SECONDS` | 2.0 | Max offset jitter before declaring a new block |
| `MIN_BLOCK_SECONDS` | 5.0 | Minimum block duration (shorter = noise) |
| `MIN_MATCH_SCORE` | 0.02 | Minimum correlation score (lower = more false positives) |
| `MIN_CUE_DURATION_SECONDS` | 0.15 | Minimum subtitle duration to keep |
| `CUE_SEARCH_RADIUS_SECONDS` | 6.0 | How far from a block's prediction a line's audio is searched for |
| `CUE_MIN_SEGMENT_SECONDS` | 4.0 | Short lines are padded to this much audio for the search |
| `CUE_CANDIDATE_MARGIN_SECONDS` | 20.0 | How close to a block a line must be for that block to predict it |
| `MIN_CUE_MATCH_SCORE` | 0.2 | Minimum match of a line's own audio to keep it |
| `BLOCK_INTERIOR_SECONDS` | 10.0 | How deep inside a block an unmatched line must sit to be kept anyway |
| `INSERT_WINDOW_SECONDS` | 2.5 | Window size of the short-insert pass |
| `INSERT_EXPLAINED_SCORE` | 0.35 | From this up, a window is explained by its block |
| `INSERT_MATCH_SCORE` | 0.6 | Minimum full-source match for an unexplained window to become a block |
| `MIN_UNACCOUNTED_SECONDS` | 4.0 | Shortest unmatched stretch that is reported as possibly missing a source |
| `WEAK_CUE_MATCH_SCORE` | 0.5 | Below this, a match gives way to a confident one it collides with |
| `CONFIDENT_CUE_MATCH_SCORE` | 0.8 | From this up, a match displaces a weak one it collides with |

The first six are the original values from the author. Lowering `MIN_MATCH_SCORE` or `MIN_BLOCK_SECONDS` introduces false-positive EDL blocks that map cut-content scenes, which then produce incorrect subtitles (tested: 0.001/0.5 added ~40 false lines). Raising them drops legitimate short scenes. These values worked best in testing. The per-line values were set on Marine Base G-8 01 (see "Per-line placement" below).

### Per-line placement

The blocks only say roughly where a line should land: a block's boundary is no more precise than the alignment step, and its offset is an average over windows that can differ slightly. Fun Pace cuts also trim pauses between lines, which shifts everything after by a fraction of a second each time. So every subtitle line is placed individually:

1. The source audio under the line (padded to at least 4 seconds) is searched for in the cut, within 6 seconds of where each nearby block predicts it.
2. At the position found, the line's own audio (without the padding) is compared against the cut. If it matches (normalized cross-correlation of at least 0.2), the line is placed at exactly that position. Audio that survived into the cut scores close to 1, audio that was cut scores close to 0, and a line trimmed partway through lands in between. The padding is left out of this check because it can match on a neighbouring line's audio alone, when the line itself was cut out right next to it.
3. A line whose audio can't be matched at all (e.g. a sign over silence) is kept at its block's offset only if it sits at least 10 seconds inside the block. Otherwise it's dropped: its scene didn't survive the cut.
4. A weak match (below 0.5) is dropped if it lands on top of a confident one (0.8 or more) that it didn't overlap in the source. Two lines can't both be spoken there, and the confident one is.

This is what keeps lines from removed scenes out of the output. `_BLOCKED_TEXTS` in `retiming.py` remains as a manual blocklist for any line that still slips through.

### Overlap resolution

When the cut trims the pause between two lines, the earlier one would still be on screen when the later one starts. The earlier line's end is pulled back to make room. The later line's start is never moved, since it was placed against the audio. Song-style lines (`Karaoke`, `Translation`) are excluded from this, since they're designed to overlap. Lines that already overlapped in the source are left alone too (e.g. an announcement at the top of the screen over dialogue at the bottom).

### Short inserts

A stretch of the cut shorter than an alignment window (15 seconds) never dominates one, so it gets no block of its own. If it comes from an episode nothing else in the cut uses, that episode isn't matched at all. Marine Base G-8 02 does this: one 5-second exchange at 3:52 is taken from the end of episode 197, in the middle of footage from 198.

After the blocks are built, a second pass catches these:

1. The cut is checked in 2.5-second windows against what the nearby blocks predict. A window that matches (0.35 or more) or is near-silent is explained.
2. Every unexplained window is searched for across each source episode in full. A match of 0.6 or more becomes a small block of its own, and consecutive windows from the same place are joined.

Lines are then placed against these blocks like any other.

### Missing source episodes

Whatever the short-insert pass still can't find in any source is reported, if it runs for 4 seconds or more, and `retime` asks for another episode to try:

```text
8s of the cut match none of the source episodes given:
  3:51 - 3:58
These are either not from the series (a title card, re-mixed audio) or from a missing source episode.
Path to another source episode to try (Enter to continue without):
```

Give the path of an episode (drag the file into the terminal) and it's added to the sources and aligned, without redoing the others. The question comes back as long as something is still unaccounted for; press Enter to go on without, which leaves those stretches unsubtitled. When the command isn't run from a terminal, the stretches are only listed.

### Manual lines

Sometimes a cut re-edits the dialogue itself: it swaps in a line from elsewhere, or keeps only the first word of a line, over re-mixed audio. No source subtitle fits that, so nothing is placed there. For those spots, `retime` picks up hand-written lines from `data/manual-lines/<cut file name>.ass` if that file exists, and adds its `Dialogue:` lines to the output as they are. They must already be timed to the cut and use this project's styles (`Main-207-` for dialogue):

```text
Dialogue: 0,0:03:54.90,0:03:55.90,Main-207-,,0,0,0,,What's going on?
```

Lines starting with `;` are comments; use them to note why each line is there.

### Multiple source episodes

A Fun Pace cut can be stitched together from more than one uncut episode. Pass every source episode it draws from: the cut is aligned against each of them separately, and every window of the cut goes to whichever source matched it best, so each kept block knows which episode it came from. Each source's subtitles are then retimed against that source's own blocks only.

### OP (Opening) handling

By default the source's own translated OP lyric lines are kept and restyled as `Karaoke`, same as any other song line. That usually leaves gaps: DVD releases keep the romaji (and any English words sung in the song, like "BON VOYAGE!") in a karaoke-effect track that can't be reused, and position some translated lines on screen.

Pass `--op-from` with a One Pace opening file to get exactly what One Pace's own releases show instead: their animated per-syllable karaoke, with the Japanese romaji at the top (`Karaoke` style) and the English translation at the bottom (`Translation` style). The effect lines and both style definitions are taken from that file as they are, only shifted in time. The dialogue style reference (`jaya 01 en.ass`) remains untouched, and the source's own OP lyric lines are left out.

| Opening | Episodes | Reference |
|---------|----------|-----------|
| Hikari E | 116-168 | `input/styles/Hikari e.ass` |
| BON VOYAGE! | 169-206 | `input/styles/Bon Voyage.ass` |

Other openings are in the One Pace repo under `main/Other/Opening/`.

How the reference is lined up with the cut:

1. Roughly, through the lyrics themselves. The reference's plain lyric lines (the per-line karaoke source lines One Pace keeps as comments) are shifted onto the source episode's timeline by the shift under which the most of them start together with the source's own translated OP lines, then located in the cut by their audio like any other line. This is only good to a few tenths of a second, because the two releases don't time their lyric lines identically.
2. Exactly, through the video. One Pace opening files carry a comment line named `sync` that starts on a specific scene change of the opening. The offset from step 1 is snapped so that this point lands on the nearest scene change actually found in the cut (within 0.5 s), which is exact to the frame.

If the reference has no `sync` line, or no scene change is found there, the rough offset is used and a warning is printed. If the source has no translated OP lines to line up with, the OP is left out with a warning. A reference without effect lines falls back to its plain lyric lines.

### Known manual fixes (episode 01 only)

Two subtitle lines in episode 01 could not be resolved automatically. Both were found with the earlier block-offset placement and have not been re-checked against per-line placement:

1. **"page 1,254" → "which you said was the most difficult!"**: the EDL maps the source time for "page 1,254" to the correct cut position, but the adjacent "which you said" line is the correct one for that scene. A sed replacement is applied post-generation.
2. **"But it was a pretty good day"**: this line is in the source's preview chapter (1385-1418s) which has no matching EDL block. It's manually appended at the correct cut timestamp.

Both fixes were applied by hand after generation.

### Subtitle track naming

The muxed MKV track is labeled "English subtitles" (originally "English AI subtitles").

### Adding a new episode

1. Put the Fun Pace cut in `input/episodes/`.
2. Put every uncut source episode it draws from in `input/source-episodes/`. Each one needs an embedded English subtitle track and a Japanese audio track.
3. Run `retime` with every source episode first and the cut last (see "Retime usage" below), adding `--op-from` with the One Pace file for that episode's opening (see "OP (Opening) handling"). It takes a few minutes.
4. The subtitled MKV and the ASS land in `output/episodes/<episode name with [Retimed Subs]>/`.

If it's not clear which source episodes a cut uses, start with the obvious ones: `retime` lists any part of the cut it can't find in them and asks for another episode (see "Missing source episodes"). Or pass the likely neighbours up front, including the episode before the first obvious one: a cut can open on, or cut back to, the closing scene of the previous episode (Marine Base G-8 02 takes one exchange from the end of episode 197). The run prints `Warning: no part of the cut matched <file>` for any source it didn't need. A stretch of the cut with dialogue but no subtitles usually means a source episode is missing. A cut longer than a single source episode always draws from more than one.

### Releasing an episode

Each episode gets its own GitHub release with the subtitle file attached (no video). The tag and the title carry the series name, since episode numbers restart with each Fun Pace series. The five Straw Hats Daily releases predate this and keep their `episode-<NN>-<title>` tags:

| Field | Format | Example |
|-------|--------|---------|
| Tag | `<series>-<NN>-<title>`, all in lowercase, words joined by dashes, no apostrophes | `marine-base-g-8-01-the-ghosting-merry` |
| Title | `<Series> <NN>: <Title>` | `Marine Base G-8 01: The Ghosting Merry` |
| Notes | `<Series> Episode <NN> with retimed English subtitles and ASS styling.` | `Marine Base G-8 Episode 01 with retimed English subtitles and ASS styling.` |
| Asset | `<series>_<NN>_<title>_retimed_subs.ass`, all in lowercase, words joined by underscores | `marine_base_g_8_01_the_ghosting_merry_retimed_subs.ass` |

For an episode made with `run` instead of `retime`, the notes say "AI-generated" instead of "retimed" and the asset ends in `_ai_subs.ass`.

Once the episode has been watched through, the `release` command does all of this from the cut's file name. It needs the [GitHub CLI](https://cli.github.com/) (`gh`), logged in:

```text
scripts/fun-pace-subs.py release "input/episodes/<fun pace cut>.mkv" --dry-run
scripts/fun-pace-subs.py release "input/episodes/<fun pace cut>.mkv"
```

`--dry-run` only prints the tag, title, notes and asset it would publish. Without it, the release is created with the episode's generated ASS from `output/episodes/` attached (the retimed one if both a retimed and an AI-generated file exist). Running it again for the same episode replaces the attached file on the existing release, which is how to publish a fix. `--subs <path>` attaches a different file.

### Retime usage

```text
scripts/fun-pace-subs.py retime "input/source-episodes/<uncut episode>.mkv" ["input/source-episodes/<another uncut episode>.mkv" ...] "input/episodes/<fun pace cut>.mkv"
```

The last argument is the cut; everything before it is a source episode. For example, Marine Base G-8 01 draws from episodes 196 and 197:

```text
nix develop . --no-write-lock-file -c scripts/fun-pace-subs.py retime \
  "input/source-episodes/[A&C] One Piece - 0196 [DVDrip] [Multi-Audio-Subs] [E7E032A4].mkv" \
  "input/source-episodes/[A&C] One Piece - 0197 [DVDrip] [Multi-Audio-Subs] [380C63A1].mkv" \
  "input/episodes/[FunPace] Marine Base G-8 01 - The Ghosting Merry [Dual Audio][Subs Missing][1080p].mkv" \
  --op-from "input/styles/Bon Voyage.ass"
```

| Option | Purpose |
|--------|---------|
| `--no-mux` | Only write the ASS, skip the MKV |
| `--output-ass <path>` | Write the ASS somewhere other than the episode's output folder |
| `--op-from <ass>` | Replace the source's OP lyrics with a One Pace reference (see "OP (Opening) handling") |
| `--style-reference-ass <ass>` | Use a different dialogue style reference |

Like `run`, this writes the ASS to the episode's own folder under `output/episodes/` (named with `[Retimed Subs]` in place of `[Subs Missing]`), symlinks it next to the cut, and muxes subtitles + fonts into a new MKV there.

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

Earlier versions of this pipeline ran Whisper's `translate` task directly over the full episode in one pass. That has two problems:
- **Timing**: Whisper's own segmentation during `translate` is unreliable over long audio. Some cues ended up spanning 30-50+ seconds of screen time while several lines of actual dialogue happened underneath.
- **Context**: translating short, isolated clips (needed to fix the timing problem) loses the surrounding-dialogue context a longer pass would have, which shows up as leftover untranslated Japanese words, dropped honorifics, and character names getting mistranslated (e.g. "Nami" as "Minami").

The current pipeline transcribes in the source language first (accurate segmentation, since there's no cross-language ambiguity), aligns those segments to the audio (precise, audio-locked timestamps), and only then translates each segment. A rolling window of the last couple of translated lines, plus a character-name glossary, gets fed in as context, which improves name/terminology consistency without re-merging segments and breaking the timing fix.

This is a quality tradeoff of translating fully offline with Whisper rather than a dedicated MT/LLM translation step, and the reason retiming above is preferred whenever it's available. Expect occasional literal or awkward phrasing on idioms Whisper doesn't have context to translate naturally. `data/one-piece-terms.tsv` patches the most common recurring cases (see the `senchou -> Captain`, `mr. nami -> Nami-san` entries for examples of that pattern).

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

Use `nix develop .`, not `nix develop path:$PWD`. The `path:` form copies the whole project folder into the Nix store on every run, videos included (about 20 GB each time with a full set of episodes), and those copies stay there until garbage-collected. The `.` form only copies the files tracked by git.

```text
nix develop . --no-write-lock-file -c scripts/fun-pace-subs.py run "input/episodes/[FunPace] Straw Hats Daily 01 - Chopper's Concoctions [Dual Audio][Subs Missing][1080p].mkv" --model large-v3
```

If you only want ASS output (skip mux):

```text
nix develop . --no-write-lock-file -c scripts/fun-pace-subs.py run "input/episodes/[FunPace] Straw Hats Daily 01 - Chopper's Concoctions [Dual Audio][Subs Missing][1080p].mkv" --no-mux
```

To transcribe the source language without translating (e.g. Japanese subtitles for Japanese audio, or English subtitles for the English dub track):

```text
scripts/fun-pace-subs.py run "input/episodes/episode.mkv" --language en --task transcribe
```

For AMD GPUs (ROCm), the script uses `faster-whisper` directly with the ROCm CTranslate2 wheel so transcription can run on the GPU without WhisperX's Torch decode stack.
Two fixes are built in for this hardware path:
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

Default style reference for `run` and `retime`:
- [input/styles/jaya 01 en.ass](input/styles/jaya%2001%20en.ass)

`retime` keeps the source's own OP lyrics by default. Optional One Pace OP lyrics reference, used only with `--op-from`:
- [input/styles/Hikari e.ass](input/styles/Hikari%20e.ass)
- [input/styles/Bon Voyage.ass](input/styles/Bon%20Voyage.ass)

Override per run:

```text
scripts/fun-pace-subs.py run "input/episodes/episode.mkv" --style-reference-ass "input/styles/another-style.ass"
scripts/fun-pace-subs.py retime <source>... <cut> --op-from "input/styles/Hikari e.ass"
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

When muxing via `run` (AI-generated), filenames are rewritten from `[Subs Missing]` to `[AI Subs]`. `retime` uses `[Retimed Subs]` instead, for the folder, the ASS and the MKV. The standalone `mux` command always uses `[AI Subs]` unless given an explicit output path.

Example:
- Input: `[FunPace] ... [Subs Missing][1080p].mkv`
- Run output: `[FunPace] ... [AI Subs][1080p].mkv`
- Retime output: `[FunPace] ... [Retimed Subs][1080p].mkv`

## Notes

- Paths with spaces are handled by quoting in the scripts.
- The default terminology map lives in [data/one-piece-terms.tsv](data/one-piece-terms.tsv).
- The public One Pace subtitle mirror is the source to mine for additional terminology and subtitle-specific naming conventions.
- The flake uses `uv` and the script can run WhisperX via `uvx --from whisperx whisperx` when a direct `whisperx` binary is not available.
- On ROCm systems, the script prefers `faster-whisper` via `uvx --from faster-whisper python` with the ROCm CTranslate2 wheel.
- The first `uvx` run will be slower because it resolves and prepares the needed environment.
