@echo off
REM SquishIt - Quick Run Script for Windows
REM Activates the virtual environment and starts the application

set "VENV_DIR=.venv"

REM Check if venv exists
if not exist "%VENV_DIR%" (
    echo [ERROR] Virtual environment not found!
    echo Please run setup.bat first to install the application.
    pause
    exit /b 1
)

REM Activate virtual environment
call "%VENV_DIR%\Scripts\activate.bat"

REM Start the application
python -m video_compressor