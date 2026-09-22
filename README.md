# SquishIt

A modern Windows desktop media compression app with a sleek dark-themed GUI. Built with Python, FFmpeg, and CustomTkinter.

## Features

### Video Codecs
- **HEVC (H.265)** — Default speed lane. Quick Compress Max stays on HEVC
- **AV1 (SVT-AV1)** — User-available archival codec. Max / Archival uses this lane with hardware off
- **AV1 (libaom)** — User-available reference AV1 encoder
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
- AMD AMF (H.264, HEVC — including RX 9070 XT)
- Software libx264 when Quick Lite has no matching hardware encoder
- Target-size and exact-size jobs use that same order first. If the hardware encode misses the size band or fails, SquishIt does not switch to software until the caller confirms (`VideoCompressor.set_software_fallback_callback`, or `--allow-software-fallback`)

### Compression Profiles
| Profile | Description | Use Case |
|---------|-------------|----------|
| **Fast** | H.264 Quick rung (CRF 26, fast) | Time-sensitive encoding |
| **Balanced** | HEVC Balanced rung (CRF 28, medium) | General use |
| **Max / Archival** | SVT-AV1 Max rung (CRF 35, preset 6) in MKV, hardware off | Archive-friendly files |
| **YouTube Upload** | Optimized for social media | Content creators |
| **Mobile** | Smaller files for mobile | Mobile viewing |
| **Streaming** | Fast encoding for live streams | Streamers |

**Max / Archival** and **Quick Compress Max** are different settings. Archival Max is SVT-AV1 with hardware off. Quick Compress Max is the HEVC Max rung (CRF 30, slow) and is labeled **HEVC Max** in the context-menu window. **Quick Lite** is the fastest context-menu rung: H.264 CRF 26, preset fast, with hardware preferred in the order NVENC, then QSV, then AMF, then software libx264. AV1 is not that rung.

### Quality ladder
Quick, Balanced, and Max share one CRF/preset table. The GUI compression control and `squishit --profile` read that table, so they emit the same FFmpeg arguments.

| Lane | Quick | Balanced | Max |
|------|-------|----------|-----|
| **H.264** (Quick Lite) | CRF 26, fast — fastest Quick Compress rung | CRF 28, medium | CRF 32, slow |
| **HEVC** (Quick Compress) | CRF 24, veryfast | CRF 28, medium | CRF 30, slow — **HEVC Max** |
| **SVT-AV1** (archival, hardware off) | CRF 32, preset 10 | CRF 35, preset 8 | CRF 35, preset 6 — **Max / Archival** |
| **AV1 (libaom)** | CRF 26, cpu-used 8 | CRF 30, cpu-used 6 | CRF 34, cpu-used 4 |

Quick finishes sooner and makes larger files. Balanced is the default tradeoff. Max is the best compression in that lane. When a profile matches a rung and a hardware encoder is selected, the command uses that rung's CQ and vendor preset (NVENC, then QSV, then AMF). SVT-AV1 stays on the software encoder. The encode-speed slider keeps the numeric SVT-AV1 and libaom preset instead of applying an x264 name.

### Smart Threading
Per-codec CPU thread optimization:
- HEVC: physical cores
- H.264: 1.5× physical cores
- VP9: capped at 8 threads
- HW encoders: half physical cores

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
- Audio quality settings
- Optional trim before compress
- Audio extraction mode
- Custom FFmpeg passthrough for power users
- Preset import/export
- Folder recursion and animated GIF/WebP compression
- Stats dashboard and history tracking

---

## Quick Start (Windows)

### Option A — Run from Source
```powershell
# 1. Install Python 3.10+ from python.org (check "Add to PATH")
# 2. Install FFmpeg:
winget install ffmpeg

# 3. Set up the app:
setup.bat

# 4. Run:
run.bat
```

### Option B — Installer
Download `SquishIt-Setup-vX.X.X.exe`, install, and launch from Start Menu or Desktop shortcut.

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

# Show hardware info
python cli.py --hardware
```

### CLI Options
| Option | Description |
|--------|-------------|
| `-p, --profile` | Profile: fast, balanced, max, youtube, mobile, streaming. `max` is Max / Archival (SVT-AV1, hardware off). Quick Compress Max is `--profile max --codec hevc` |
| `-c, --codec` | Video codec: hevc, h264, vp9, svt-av1, av1 |
| `--crf` | Constant Rate Factor (0-51, lower = better quality) |
| `--preset` | Encoding speed (ultrafast to veryslow) |
| `-t, --target-size` | Target file size in MB |
| `--target-percent` | Target size as percentage of original |
| `-r, --resolution` | Max resolution (4k/1440p/1080p/720p/480p) |
| `-f, --fps` | Target frame rate |
| `--no-hw-accel` | Disable hardware acceleration |
| `--allow-software-fallback` | Target-size only: confirm a software retry after hardware misses the size or fails. Omitted, the hardware result stays |
| `-o, --output` | Output directory |

---

## Dev Tooling

GitHub Actions runs `pytest` on Ubuntu for Python 3.10 and 3.12. The suite does not open a GUI. CI installs FFmpeg, then runs the unit tests and a separate `smoke` step (`pytest -m smoke`). The smoke step checks that the installed `ffmpeg` binary can encode about one second of generated video, that `VideoCompressor` encodes that clip on the Quick Lite software path (`libx264`, not HEVC), and that Quick Lite plus both Max lanes (`libx265` and `libsvtav1`) stay inside soft encode-time, file-size, and SSIM bounds. Those bounds are documented in `tests/test_encode_ladder_regression.py`. Locally those tests skip when `ffmpeg` is not on `PATH`; in CI a missing binary fails the job. Run just that check with `python -m pytest -m smoke`.

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
# Output: dist\SquishIt-Setup-v2.0.0.exe
```

---

## Project Structure

```
video_compressor/          # Main package (launch: python -m video_compressor)
├── __init__.py
├── __main__.py             # CLI dispatch (--quick-compress, --open, --register)
├── config.py               # App config, paths, defaults
├── core/
│   ├── codecs.py           # Codec definitions, container compatibility matrix
│   ├── compressor.py       # Compression engine, FFmpeg integration
│   ├── eta.py              # Rolling-window ETA calculator
│   ├── hardware.py         # GPU/CPU detection
│   ├── profiles.py         # Compression profiles
│   └── utils.py            # Utility functions
└── gui/
    ├── main_window.py      # Main application window
    ├── quick_compress.py   # Minimal progress window for context menu
    └── widgets.py          # Custom UI widgets

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

tests/                      # Test suite (pytest; no display required)
├── test_cli.py
├── test_codecs.py
├── test_config.py
├── test_eta.py
├── test_ffmpeg_smoke.py    # Real FFmpeg and VideoCompressor encodes (marker: smoke)
├── test_profiles.py
└── test_target_size.py
```

---

## Requirements

- **Python** 3.10+
- **FFmpeg** with libx265, libsvtav1, libaom-av1, libx264, and libvpx-vp9
- **Windows 10/11** (context menu integration is Windows-only)

### Python Dependencies
- customtkinter, tkinterdnd2 — GUI
- Pillow — Image compression
- psutil — CPU info
- WMI — Windows hardware detection
- pyyaml — Configuration

---

## License

MIT License

---

## Acknowledgments

- [FFmpeg](https://ffmpeg.org/) — Video encoding backbone
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) — Modern Tkinter widgets