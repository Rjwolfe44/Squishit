@echo off
setlocal

set "VENV_DIR=.venv"
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Virtual environment missing. Run tools\install_dev.bat first.
    exit /b 1
)

call "%VENV_DIR%\Scripts\activate.bat"

if "%~1"=="" goto help
if /I "%~1"=="run" goto run
if /I "%~1"=="cli" goto cli
if /I "%~1"=="lint" goto lint
if /I "%~1"=="format" goto format
if /I "%~1"=="test" goto test
if /I "%~1"=="check-ffmpeg" goto checkffmpeg
if /I "%~1"=="register" goto register
if /I "%~1"=="unregister" goto unregister
if /I "%~1"=="build" goto build
if /I "%~1"=="installer" goto installer
if /I "%~1"=="fetch-ffmpeg" goto fetchffmpeg
if /I "%~1"=="bench" goto bench
if /I "%~1"=="register-startup" goto registerstartup
if /I "%~1"=="unregister-startup" goto unregisterstartup
if /I "%~1"=="tray" goto tray
if /I "%~1"=="clean" goto clean
goto help

:run
python -m video_compressor
goto end

:cli
shift
python cli.py %*
goto end

:lint
python -m ruff check .
python -m mypy video_compressor cli.py
goto end

:format
python -m black video_compressor cli.py tools
goto end

:test
python -m pytest
goto end

:build
call tools\build.bat
goto end

:installer
call tools\make_installer.bat
goto end

:fetchffmpeg
call tools\fetch_ffmpeg.bat
goto end

:bench
python tools\benchmark.py
goto end

:checkffmpeg
python tools\check_ffmpeg.py
goto end

:register
python tools\register_context_menu.py
goto end

:unregister
python tools\unregister_context_menu.py
goto end

:registerstartup
python tools\register_startup.py
goto end

:unregisterstartup
python tools\unregister_startup.py
goto end

:tray
python -m video_compressor --tray
goto end

:clean
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist benchmark_output rmdir /s /q benchmark_output
goto end

:help
echo SquishIt dev commands:
echo   dev.bat run
echo   dev.bat cli ^<args^>
echo   dev.bat lint
echo   dev.bat format
echo   dev.bat test
echo   dev.bat build
echo   dev.bat installer
echo   dev.bat fetch-ffmpeg
echo   dev.bat bench
echo   dev.bat check-ffmpeg
echo   dev.bat register
echo   dev.bat unregister
echo   dev.bat register-startup
echo   dev.bat unregister-startup
echo   dev.bat tray
echo   dev.bat clean
exit /b 1

:end
endlocal
