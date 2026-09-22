# SquishIt

A modern Windows desktop media compression app with a sleek dark-themed GUI. Built with Python, FFmpeg, and CustomTkinter.

## Features

### Video Codecs
- **HEVC (H.265)** — Excellent quality-to-size ratio
- **H.264 (AVC)** — Maximum compatibility
- **VP9** — Web-friendly fallback when you specifically want WebM output

### Output Containers
MP4, MKV, WebM, MOV, AVI — with automatic codec/container compatibility filtering.

### Image Compression
- Input: JPEG, PNG, WebP, BMP, TIFF, GIF
- Output: WebP, AVIF, JPEG, PNG, JXL
- Automatic format selection based on profile

### Hardware Acceleration
- NVIDIA NVENC (H.264, HEVC)
- Intel Quick Sync Video (QSV)
- AMD AMF (H.264, HEVC — including RX 9070 XT)

### Compression Profiles
| Profile | Description | Use Case |
|---------|-------------|----------|
| **Fast** | Quick compression, minimal quality loss | Time-sensitive encoding |
| **Balanced** | Good size vs quality compromise | General use |
| **Max** | SVT-AV1 in MKV with Opus audio and hardware off | Smaller archive-friendly files |
| **YouTube Upload** | Optimized for social media | Content creators |
| **Mobile** | Smaller files for mobile | Mobile viewing |
| **Streaming** | Fast encoding for live streams | Streamers |

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
| `-p, --profile` | Compression profile (fast/balanced/max/youtube/mobile/streaming) |
| `-c, --codec` | Video codec (hevc/h264/vp9) |
| `--crf` | Constant Rate Factor (0-51, lower = better quality) |
| `--preset` | Encoding speed (ultrafast to veryslow) |
| `-t, --target-size` | Target file size in MB |
| `--target-percent` | Target size as percentage of original |
| `-r, --resolution` | Max resolution (4k/1440p/1080p/720p/480p) |
| `-f, --fps` | Target frame rate |
| `--no-hw-accel` | Disable hardware acceleration |
| `-o, --output` | Output directory |

---

## Dev Tooling

GitHub Actions runs `pytest` on Ubuntu for Python 3.10 and 3.12. The suite does not open a GUI.

All dev commands go through `dev.bat`:

```powershell
dev.bat run             # Launch the GUI
dev.bat cli video.mp4   # Run CLI
dev.bat test            # Run pytest suite
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
├── test_profiles.py
└── test_target_size.py
```

---

## Requirements

- **Python** 3.10+
- **FFmpeg** with libx265, libx264, libvpx-vp9
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