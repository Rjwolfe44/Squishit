@echo off
setlocal

call .venv\Scripts\activate.bat
if not exist vendor\ffmpeg mkdir vendor\ffmpeg
python -m PyInstaller squishit.spec --noconfirm
if errorlevel 1 exit /b 1

echo Build staging complete for installer packaging.
