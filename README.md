# SquishIt

SquishIt compresses video and images on Windows. Drop in a file, pick a preset, and FFmpeg does the encode — HEVC, AV1, and H.264, with hardware encoders when the preset allows them.

**[Download the installer](https://github.com/Rjwolfe44/Squishit/releases)** from GitHub Releases on this repository.

Found a bug or want a feature? Open a [GitHub Issue](https://github.com/Rjwolfe44/Squishit/issues).

## Features

### Trim and clips
Press **Trim** on any video in the queue (or double-click it) to open the clip editor.

- **See what you keep.** A filmstrip timeline shows the whole video. Drag the edges of a blue box to trim, drag its middle to move it, or drag across empty space to add another clip. The mouse wheel zooms, Shift + wheel scrolls, and edges snap to the playhead and to other clips (hold Alt to turn snapping off).
- **Preview as you go.** Scrub, step frame by frame, or press Space to play. Playback has sound and the picture keeps in step with it; scrubbing and frame steps stay silent. SquishIt uses its bundled FFmpeg for the pictures and the sound, and Windows' own sound output, so there is nothing extra to install.
- **Sound on or off.** **Sound on / Sound off** next to the play buttons (or `M`) turns the preview sound on and off, and the slider beside it sets the volume. The editor remembers both for next time. A video without sound says "This video has no sound." and hides these controls. With no sound device, playback simply carries on silently.
- **Sound wave.** Tick **Sound wave** to show how loud the video is along the timeline, under the filmstrip. It zooms and scrolls with the timeline and is worked out in the background, at low priority, while you edit. The editor remembers whether it is shown.
- **Keep or drop the sound.** Untick **Include the sound in the saved clips** to save clips (separate files or a joined video) without any sound. The file's badge and the editor summary then say "no sound". This works on the whole video too, without trimming anything: **Use whole video** keeps the choice and saves the whole video without its sound. It is undoable like any other edit.
- **Many clips from one video.** Choose **Save each clip as its own file** (default) or **Join all clips into one video**. A size target applies to each output file.
- **Exact times.** Type times such as `1:30` or `90.5` in the clip table or the time box.
- **Safe by default.** SquishIt never replaces a file that is already there; it saves `name (2).mp4` instead. Your files and their clips stay in the list after a run, so you can run them again with other settings.

| Key | Action |
|-----|--------|
| `Space` | Play / pause |
| `M` | Sound on / off |
| `I` / `O` | Start / end the selected clip at the playhead |
| `S` | Split the clip at the playhead |
| `←` / `→` | Back / forward one frame |
| `Shift` + `←` / `→` | Back / forward one second |
| `Home` / `End` | Jump to the start / end |
| `Delete` | Remove the selected clip |
| `Ctrl+Z` / `Ctrl+Y` | Undo / redo |
| `Ctrl+=` / `Ctrl+-` / `Ctrl+0` | Zoom in / out / fit |
| `Enter` | Save the clips |

Progress, time left, and size targets are worked out from the clips, not the whole video. From the command line, use `--clip START-END` (see below).

**Checking the sound by hand** (the tests cannot listen): open a video with sound in the editor and press Space. You should hear it from the playhead, with lips and claps in step with the picture after several minutes, and again after pausing, seeking and pressing Play. `M` should silence it at once and bring it back from the playhead; the volume slider should change the loudness while playing and be remembered next time. Unplug or disable the sound device and play again: the picture keeps going and the editor says the preview is silent. A video without sound plays silently with the sound controls hidden. The volume slider scales the sound itself; it never moves SquishIt's slider in the Windows volume mixer. The automated tests never make a sound: they cannot open the real sound device unless `SQUISHIT_REAL_AUDIO=1` is set, and the two checks that then use it play only silence.

### Video Codecs
- **HEVC (H.265)** — Default speed lane. Quick Compress Max stays on HEVC
- **SVT-AV1** — Max / Archival. Best compression, on the CPU. Hardware encode stays off
- **Fast AV1** (`--codec av1`) — fast AV1 on any PC. It uses a graphics card only after that card passes a short AV1 test encode on this PC (NVENC, then QSV, then AMF, including `av1_amf` on an RX 9070 XT). Otherwise it runs SVT-AV1 at a preset this processor can keep near real time, then libaom in its realtime mode if FFmpeg has no SVT-AV1. If FFmpeg has no AV1 encoder at all, the same rung runs on HEVC (or H.264) and the result says so. This is not Max / Archival
- AV1 files need a player with AV1 support: VLC, Edge or Chrome, or Windows' free AV1 Video Extension
- **H.264 (AVC)** — Maximum compatibility. Quick Lite, the fastest Quick Compress rung, uses H.264 and prefers NVENC, then QSV, then AMF, then libx264
- **VP9** — Web-friendly fallback when you specifically want WebM output

### Output Containers
MP4, MKV, WebM, MOV, AVI — with automatic codec/container compatibility filtering.

### Image Compression
- Input: JPEG, PNG, WebP, BMP, TIFF, GIF
- Output: WebP, AVIF, JPEG, PNG, JXL
- Automatic format selection based on profile

### Hardware Acceleration
- NVIDIA NVENC (H.264, HEVC) — preferred when more than one vendor is available
- Intel Quick Sync Video (QSV)
- AMD AMF (H.264, HEVC, and AV1 — including `av1_amf` on an RX 9070 XT)
- **AV1 hardware is checked, not guessed.** FFmpeg lists `av1_nvenc`, `av1_qsv`, and `av1_amf` on every Windows PC, and a GPU name says little about the driver. Fast AV1 runs eight generated 640x360 frames through each listed encoder (no file written, 8-second limit, no console window) and only uses one that works. The window does this on its hardware-check thread at startup. Answers are saved in `%APPDATA%\SquishIt\av1_hardware_check.json`, keyed by encoder, FFmpeg build, and graphics adapters with driver versions, so a normal launch does not test again; a new driver, GPU, or FFmpeg tests again (a pass is kept 30 days, a failure 7). A failure that reads like a busy GPU ("out of memory", "busy", NVENC's session limit), a timeout, or a check that could not run is never saved, so the next launch tests again; a damaged entry in the file is ignored and tested again. Remote Desktop sessions keep their own answers, because AMF and QSV often cannot reach the GPU there. On a PC with an integrated and a discrete GPU the test uses the same default adapter the real encode will use. The window never runs or waits for a test on its own thread: until the startup check has an answer, the size preview and the start-up slot count treat Fast AV1 as running on the CPU, and both redraw when the check finishes. A test still running when SquishIt exits is stopped. `cli.py --hardware` runs the test fresh and prints each result
- AMF AV1 quality values are AV1 q-indexes (0-255), so the ladder's CQ is multiplied by 4 for `av1_amf` (CQ 30 → q-index 120). The old value was used as-is, which meant near-lossless files: on a 10 s 1080p clip Balanced went from 40.7 MB to 16.9 MB at the same speed, matching SVT-AV1 CRF 33-34 in VMAF
- Windows Max / Archival decode can use D3D11VA (`-hwaccel d3d11va`). If that fails, the same `libsvtav1` encode runs again with software decode. Encode is not switched to AMF
- Software libx264 when Quick Lite has no matching hardware encoder
- Target-size and exact-size jobs use that same order first. If the hardware encode misses the size band, fails, or finishes under an exact target where padding would apply, SquishIt does not switch to software until the caller confirms. Padding that short hardware file is ask-gated: the main window asks with the same Yes/No dialog; No or dismissing it keeps the unpadded hardware result and shows that on the result card. The CLI confirms with `--allow-software-fallback`. For Fast AV1 that software retry is SVT-AV1 (libaom without it). Quick Compress presets are not target-size jobs, so they do not ask: if a graphics card that passed the AV1 test still fails on one file, Fast AV1 re-encodes that file with SVT-AV1 and says so on the result. Leaving the callback unset also keeps the hardware result.

### Compression Profiles
| Profile | Description | Use Case |
|---------|-------------|----------|
| **Fast** | H.264 Quick rung (CRF 26, fast) | Time-sensitive encoding |
| **Balanced** | HEVC Balanced rung (CRF 28, medium) | General use |
| **Max / Archival** | SVT-AV1 Max rung (CRF 36, preset 6) in MKV, best compression, CPU, hardware encode off | Archive-friendly files |
| **YouTube / social upload** | Video file for YouTube and other social uploads (`--profile youtube`) | Social file uploads |
| **Mobile** | Smaller files for mobile | Mobile viewing |
| **Streaming upload** | Video file for Twitch, YouTube, or OBS upload (`--profile streaming`) | Upload-ready files |

**Streaming upload** encodes a finished video file for Twitch, YouTube, or OBS. It does not start a live RTMP broadcast.

**Max / Archival** and **Quick Compress Max** are different settings. Archival Max is SVT-AV1 with hardware off. Quick Compress Max is the HEVC Max rung (CRF 30, slow) and is labeled **HEVC Max** in the context-menu window. **Quick Lite** is the fastest context-menu rung: H.264 CRF 26, preset fast, with hardware preferred in the order NVENC, then QSV, then AMF, then software libx264. AV1 is not that rung.

### Quality ladder
Quick, Balanced, and Max share one CRF/preset table. The GUI compression control and `squishit --profile` read that table, so they emit the same FFmpeg arguments.

| Lane | Quick | Balanced | Max |
|------|-------|----------|-----|
| **H.264** (Quick Lite) | CRF 26, fast — fastest Quick Compress rung | CRF 28, medium | CRF 32, slow |
| **HEVC** (Quick Compress) | CRF 24, veryfast | CRF 28, medium | CRF 30, slow — **HEVC Max** |
| **SVT-AV1** (Max / Archival, best compression, CPU, hardware encode off) | CRF 32, preset 10 | CRF 35, preset 8 | CRF 36, preset 6 — **Max / Archival** |
| **Fast AV1** — on the CPU (SVT-AV1) | CRF 30, preset 10 | CRF 33, preset 9 | CRF 36, preset 8 |
| **Fast AV1** — on a graphics card that passed the AV1 test (NVENC, then QSV, then AMF) | CQ 26 | CQ 30 | CQ 34 |

Quick finishes sooner and makes larger files. Balanced is the default tradeoff. Max is the best compression in that lane. When a profile matches a rung and a hardware encoder is selected, the command uses that rung's CQ and vendor preset (NVENC, then QSV, then AMF). SVT-AV1 stays on the software encoder. The encode-speed slider keeps the numeric SVT-AV1 preset instead of applying an x264 name.

Fast AV1's rung preset is the most a strong CPU runs. On a smaller CPU or a bigger frame it goes faster, never slower, so the encode stays near real time. The count is the job's own share of the CPU (two jobs at once each get about half): counting a 4K frame as four 1080p frames, 8 or more logical CPUs per 1080p frame keep the rung preset, 4-7 use at least preset 9, 2-3 at least preset 10, and fewer use preset 12 (measured on 10 s of 1080p: preset 9 runs 1.5x real time on 2 CPUs, 3.0x on 4, 5.4x on 8). Preset 9 beat preset 10 at the same CRF in both size and VMAF, so Balanced uses it. Without SVT-AV1, libaom runs its realtime mode at cpu-used 8 with row and tile threading (3.6x real time on 32 threads, instead of 0.2x in its "good" mode) at the rung's CRF. A custom Fast AV1 CRF and preset (a saved profile, the profile editor, or `--crf`) are used exactly as given, with no speed floor. A profile file saved by 2.1.0 or older whose Fast AV1 numbers are exactly one of that version's libaom rungs (CRF 26/30/34 with cpu-used 8/6/4) takes the matching new rung when it is loaded; files saved now carry a `settings_version`, so the same numbers typed today stay as typed.

### Encode resource governor
One governor owns encode concurrency. It splits the CPU thread budget across the jobs that are actually running, and it caps how many encodes start at once: software jobs share half the logical CPUs, NVENC keeps 3 sessions, and QSV, AMF, and VideoToolbox keep 2. Each job gets one thread flag from that budget. libsvtav1 uses `lp=`. A single Max / Archival job on Resource Max sets `lp` to the logical CPU count. A single Fast AV1 job on the CPU gets every logical CPU on Auto whenever the PC has 8 GB of RAM or more; for a 4K frame on a PC with less than about 13 GB, its SVT-AV1 `lp` is capped so the encoder stays within about 45% of RAM (full parallelism peaks above 5 GB at 4K; 8 GB gets `lp=4`, 4 GB `lp=1`). libx264 and libx265 use a single `-threads` value and do not also set `pools` or `frame-threads`. Quick Lite, HEVC Max, and Archival SVT-AV1 keep their encoder choices.

### Right-Click Context Menu
- **Compress with SquishIt** — Quick compress with last-used profile
- **Open in SquishIt** — Open file in full GUI

### ETA System
Rolling 5-second window with exponential smoothing. Shows per-job FPS, MB/s, ETA, and total queue ETA.

### Advanced Controls
- Target file size mode (specify exact output size)
- Thread count control (auto-optimized per codec)
- Resolution scaling (4K, 1440p, 1080p, 720p, 480p)
- Frame rate adjustment
- Encoding speed/preset control
- Audio quality and audio track handling (keep, re-encode, or remove)
- Audio-only output
- Custom FFmpeg options for power users
- Custom profiles editor with YAML import and export
- Compare sample: try a few settings on a short piece of a video before the full run
- Run order (as listed, largest, smallest, longest, or shortest first)
- Folder recursion and animated GIF/WebP compression
- History and statistics windows, and an update notice when a newer version is out
- Shortcuts: `Ctrl+O` add files, `Ctrl+Shift+O` add a folder, `Ctrl+Enter` compress

---

## Quick Start (Windows)

### Option A — Run from Source
```powershell
# 1. Install Python 3.10+ from python.org (check "Add to PATH")
# 2. Install FFmpeg:
winget install ffmpeg

# 3. Set up the app:
setup.bat

# 4. Run the PySide6 shell:
run.bat
```

`run.bat` and `python -m video_compressor` open the Qt window. Quick Compress from a file is `python -m video_compressor --quick-compress path\to\video.mp4`. The older CustomTkinter shell is still in the tree for reference. Install it with `pip install -e ".[legacy-ui]"` and start it with `set SQUISHIT_UI=ctk` before `run.bat`. The packaged app does not include that shell.

### Option B — Installer
Download `SquishIt-Setup-vX.X.X.exe` from [GitHub Releases](https://github.com/Rjwolfe44/Squishit/releases), install, and launch from the Start Menu or Desktop shortcut.

---

## Command-Line Interface

```bash
# Compress a video
python cli.py video.mp4

# Compress with a profile
python cli.py video.mp4 --profile fast

# Compress with specific codec
python cli.py video.mp4 --codec hevc --preset slow

# Compress with target size
python cli.py video.mp4 --target-size 100

# Batch compress
python cli.py *.mp4 --output ./compressed/

# Cut clips: one file per clip, or --join to make one video
python cli.py talk.mp4 --clip 1:30-2:45 --clip 10:00-10:30
python cli.py talk.mp4 --clip 5-10 --clip 20-25 --join
python cli.py talk.mp4 --clip -30 --dry-run      # first 30 seconds; show the plan only

# Fast AV1: graphics card if it passes the AV1 test, else SVT-AV1 on the CPU
python cli.py video.mp4 --codec av1

# Show hardware info, and test each hardware AV1 encoder now
python cli.py --hardware
```

### CLI Options
| Option | Description |
|--------|-------------|
| `-p, --profile` | Profile: fast, balanced, max, youtube, mobile, streaming. `max` is Max / Archival (SVT-AV1, hardware off). Quick Compress Max is `--profile max --codec hevc` |
| `-c, --codec` | Video codec: hevc, h264, vp9, svt-av1, av1. `av1` is Fast AV1 (a graphics card that passes the AV1 test, else SVT-AV1, else libaom, else HEVC with a note); `svt-av1` is Max / Archival's encoder |
| `--hardware` | Print the detected CPU, RAM, and GPUs (with driver versions), run the Fast AV1 test encode on each hardware AV1 encoder FFmpeg lists, and say what `--codec av1` will use |
| `--crf` | Constant Rate Factor (0-51, lower = better quality) |
| `--preset` | Encoding speed (ultrafast to veryslow) |
| `-t, --target-size` | Target file size in MB |
| `--target-percent` | Target size as percentage of original |
| `-r, --resolution` | Max resolution (4k/1440p/1080p/720p/480p) |
| `-f, --fps` | Target frame rate |
| `--no-hw-accel` | Disable hardware acceleration |
| `--allow-software-fallback` | Target-size only: confirm a software retry after hardware misses the size, fails, or would need padding to hit an exact target. Omitted, the unpadded hardware result stays |
| `--clip START-END` | Keep only this part of a video. Repeat for more clips. Times are seconds or `[h:]mm:ss`; `-30` means the first 30 s, `1:00-` means 1:00 to the end. One file per clip unless `--join` |
| `--join` | Join all `--clip` ranges into one video, in the order given (needs two or more) |
| `-o, --output` | Output directory |

---

## Dev Tooling

GitHub Actions runs `pytest` on Ubuntu for Python 3.10 and 3.12. CI installs FFmpeg and the Qt platform libraries, then runs the unit tests — including an offscreen PySide6 launch check — and a separate `smoke` step (`pytest -m smoke`). The smoke step checks that the installed `ffmpeg` binary can encode about one second of generated video, that `VideoCompressor` encodes that clip on the Quick Lite software path (`libx264`, not HEVC), and that Quick Lite plus both Max lanes (`libx265` and `libsvtav1`) stay inside soft encode-time, file-size, and SSIM bounds. Those bounds are documented in `tests/test_encode_ladder_regression.py`. Locally those tests skip when `ffmpeg` is not on `PATH`; in CI a missing binary fails the job. Run just that check with `python -m pytest -m smoke`. `tests/test_fast_av1_smoke.py` encodes a second of video on every Fast AV1 path the FFmpeg has (SVT-AV1, libaom, a GPU that passes the AV1 test, and the HEVC fallback) and skips the paths it lacks. The repo's `vendor\ffmpeg` essentials build has no libsvtav1; set `SQUISHIT_SMOKE_FFMPEG` to a full build (for example the installed app's `_internal\vendor\ffmpeg\ffmpeg.exe`) to run the SVT-AV1 one. Unit tests never start a real hardware encoder or write the AV1 check file: `tests/conftest.py` swaps in a fake check.

All dev commands go through `dev.bat`:

```powershell
dev.bat run             # Launch the GUI
dev.bat cli video.mp4   # Run CLI
dev.bat test            # Run pytest suite (includes the FFmpeg smoke when ffmpeg is installed)
dev.bat lint            # Ruff linting
dev.bat format          # Black formatting
dev.bat build           # PyInstaller EXE build
dev.bat installer       # Inno Setup installer
dev.bat fetch-ffmpeg    # Download FFmpeg binaries
dev.bat bench           # Encoder benchmark
dev.bat check-ffmpeg    # Validate FFmpeg installation
dev.bat clean           # Remove build artifacts
```

### Building

```powershell
# Build EXE (requires PyInstaller)
dev.bat build
# Output: dist\SquishIt\SquishIt.exe

# Build installer (requires Inno Setup)
dev.bat installer
# Output: dist\SquishIt-Setup-v<version>.exe
```

---

## Project Structure

```
video_compressor/          # Main package (launch: python -m video_compressor)
├── __init__.py
├── __main__.py             # CLI dispatch (--quick-compress, --open, --register)
├── config.py               # App config, paths, defaults
├── core/
│   ├── clips.py            # Clip plans: trim, join, effective length, filter graph
│   ├── codecs.py           # Codec definitions, container compatibility matrix
│   ├── compressor.py       # Compression engine, FFmpeg integration
│   ├── eta.py              # Rolling-window ETA calculator
│   ├── fast_av1.py         # Fast AV1: hardware AV1 test encode and its cache, SVT-AV1 speed/memory policy
│   ├── hardware.py         # GPU/CPU detection
│   ├── preview.py          # Frames, filmstrip, playback with sound and the sound wave for the trim editor
│   ├── audio_out.py        # Sound output (Windows waveOut via ctypes, or silent) and the A/V sync clock
│   ├── profiles.py         # Compression profiles
│   └── utils.py            # Utility functions
└── gui/
    ├── launch.py           # Default Qt shell; SQUISHIT_UI=ctk for legacy
    ├── qt_shell/           # PySide6 main window, trim editor, dialogs, settings
    ├── timeline_model.py   # Pure timeline and clip-editing logic (no Qt)
    ├── main_window.py      # Legacy CustomTkinter window
    ├── quick_compress.py   # Legacy context-menu window
    └── widgets.py          # Legacy CustomTkinter widgets

tools/                      # Build & dev tools
├── register_context_menu.py
├── unregister_context_menu.py
├── build.bat               # PyInstaller build
├── make_installer.bat      # Inno Setup + portable zip
├── squishit.iss            # Inno Setup script
├── fetch_ffmpeg.bat        # Download FFmpeg
├── check_ffmpeg.py         # Validate FFmpeg
├── benchmark.py            # Encoder benchmark
└── install_dev.bat         # Dev environment setup

tests/                      # Test suite (pytest; Qt launch check is offscreen)
├── test_cli.py
├── test_codecs.py
├── test_config.py
├── test_eta.py
├── test_clips.py           # Clip plans, timecodes, output names
├── test_trim_engine.py     # Trim and join FFmpeg commands, progress and size maths
├── test_trim_smoke.py      # Real clip encodes checked with ffprobe (marker: smoke)
├── test_window_clips_smoke.py  # The window through the real engine (marker: smoke)
├── test_timeline_model.py  # Timeline geometry, clip editing, undo
├── test_trim_editor.py     # Trim editor and timeline widget (offscreen Qt)
├── test_audio_out.py       # Sound output (fake winmm), sync clock; one real-device check on Windows
├── test_preview_audio.py   # Sound playback, sound wave and sync with fakes, plus real-FFmpeg smoke
├── test_ffmpeg_smoke.py    # Real FFmpeg and VideoCompressor encodes (marker: smoke)
├── conftest.py             # Fake hardware AV1 check for every test (no GPU encode, no cache file)
├── test_fast_av1.py        # Fast AV1 check, fallback chain, presets, copy (fakes only)
├── test_fast_av1_smoke.py  # Each Fast AV1 path this FFmpeg has (marker: smoke)
├── test_profiles.py
└── test_target_size.py
```

---

## Requirements

- **Python** 3.10+
- **FFmpeg** with libx265, libsvtav1, libaom-av1, libx264, and libvpx-vp9 (Fast AV1 still works without libsvtav1, more slowly on libaom, and falls back to HEVC with neither)
- **Windows 10/11** (context menu integration is Windows-only)

### Python Dependencies
- PySide6 — desktop shell (Qt for Python, LGPL, separate from SquishIt's license)
- Pillow — Image compression
- psutil — CPU info
- WMI — Windows hardware detection
- pyyaml — Configuration

PySide6 replaces CustomTkinter in the installer. Qt libraries ship beside the app in the onedir build, so the installer grows by roughly the Qt runtime (on the order of 100 MB unpacked). CustomTkinter is no longer packaged.

---

## Help

Bugs, questions, and feature requests: [GitHub Issues](https://github.com/Rjwolfe44/Squishit/issues).

Development setup and pull requests: [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

SquishIt is source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE). Copyright (c) 2026 Rjwolfe44.

You may view this source and use, copy, and modify it for personal and other noncommercial purposes, including noncommercial distribution of those changes. Commercial rights are reserved exclusively to the copyright holder, Rjwolfe44. That includes selling or sublicensing SquishIt, hosting it as a paid or commercial service, and selling or commercially distributing derivatives, rebranded builds, or competing products and services based on this code.

FFmpeg, which SquishIt uses to encode media, remains under its own LGPL/GPL terms. Those terms apply to FFmpeg only and are separate from the license for this repository.

---

## Acknowledgments

- [FFmpeg](https://ffmpeg.org/) — Video encoding backbone
- [Qt for Python (PySide6)](https://doc.qt.io/qtforpython/) — Desktop shell