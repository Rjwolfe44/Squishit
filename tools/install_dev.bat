@echo off
setlocal

set "VENV_DIR=.venv"

if not exist "%VENV_DIR%" (
    python -m venv "%VENV_DIR%"
)

call "%VENV_DIR%\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .[dev]

echo Dev environment ready.
