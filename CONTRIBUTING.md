# Contributing

SquishIt is the Windows desktop media compressor in this repository.

- Installers: [GitHub Releases](https://github.com/Rjwolfe44/Squishit/releases)
- Bugs and feature requests: [GitHub Issues](https://github.com/Rjwolfe44/Squishit/issues)

## Setup

Windows, from the repository root:

```powershell
setup.bat
dev.bat test
```

You need Python 3.10+ and FFmpeg on `PATH`. `dev.bat test` runs pytest. The `smoke` tests need `ffmpeg`. The desktop shell is PySide6, installed with the rest of the requirements. `SQUISHIT_UI=ctk` opens the previous CustomTkinter windows after `pip install -e ".[legacy-ui]"`.

On Linux or macOS:

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

## Pull requests

- Branch from `main`.
- Keep the change focused.
- Run `python -m pytest` before opening a pull request.
- Do not commit tokens, `.env` files, private keys, or `.github_token`.

## License

SquishIt is source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE). Copyright (c) 2026 Rjwolfe44.

By contributing, you license your contribution under those terms for noncommercial use, and you grant Rjwolfe44 the exclusive right to sell, sublicense, and otherwise commercially exploit your contribution as part of SquishIt. Third parties receive no right to profit from the work, including by selling it, rebranding it for commercial use, or offering a commercial product or service based on it.
