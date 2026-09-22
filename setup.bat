@echo off
REM SquishIt - Windows Setup Script
REM This script creates a virtual environment, installs dependencies, and optionally starts the app.

echo ============================================
echo    SquishIt - Windows Setup
echo ============================================
echo.

set "VENV_DIR=.venv"

REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python 3.10 or later from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

REM Get Python version
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYTHON_VERSION=%%i
echo [INFO] Found Python %PYTHON_VERSION%

REM Check Python version is 3.10+
python -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python 3.10 or later is required.
    echo Your version: %PYTHON_VERSION%
    pause
    exit /b 1
)

REM Create virtual environment if it doesn't exist or is corrupted
if not exist "%VENV_DIR%" (
    echo [INFO] Creating virtual environment...
    python -m venv "%VENV_DIR%"
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created.
) else (
    echo [OK] Virtual environment already exists.
    
    REM Check if pip is working
    call "%VENV_DIR%\Scripts\python.exe" -m pip --version >nul 2>&1
    if %errorlevel% neq 0 (
        echo [WARNING] Virtual environment appears corrupted. Recreating...
        rmdir /s /q "%VENV_DIR%"
        python -m venv "%VENV_DIR%"
        if %errorlevel% neq 0 (
            echo [ERROR] Failed to create virtual environment.
            pause
            exit /b 1
        )
        echo [OK] Virtual environment recreated.
    )
)

REM Activate virtual environment
echo [INFO] Activating virtual environment...
call "%VENV_DIR%\Scripts\activate.bat"

REM Upgrade pip
echo [INFO] Upgrading pip...
python -m pip install --upgrade pip --quiet

REM Install dependencies
echo [INFO] Installing dependencies...
pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)
echo [OK] Dependencies installed.

REM Check for FFmpeg
echo.
echo [INFO] Checking for FFmpeg...
ffmpeg -version >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARNING] FFmpeg is not installed or not in PATH.
    echo.
    echo FFmpeg is required for video compression. Please install it:
    echo.
    echo Option 1 - Using winget (Windows 10/11):
    echo   winget install ffmpeg
    echo.
    echo Option 2 - Using Chocolatey:
    echo   choco install ffmpeg
    echo.
    echo Option 3 - Manual installation:
    echo   1. Download from https://www.gyan.dev/ffmpeg/builds/
    echo   2. Extract and add the 'bin' folder to your PATH
    echo.
    echo After installing FFmpeg, run this script again.
    pause
    exit /b 1
) else (
    echo [OK] FFmpeg is installed.
)

echo.
echo ============================================
echo    Setup Complete!
echo ============================================
echo.
echo To run the application:
echo   1. Activate the virtual environment:
echo      %VENV_DIR%\Scripts\activate
echo   2. Run the app:
echo      python -m video_compressor
echo.
echo Or use the provided run.bat script.
echo.

REM Ask if user wants to start the app
set /p START_APP="Start SquishIt now? (Y/n): "
if /i "%START_APP%" neq "n" (
    echo [INFO] Starting SquishIt...
    python -m video_compressor
)

pause