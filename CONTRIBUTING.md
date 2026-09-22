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

You need Python 3.10+ and FFmpeg on `PATH`. `dev.bat test` runs pytest. The `smoke` tests need `ffmpeg`.

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

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
