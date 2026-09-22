@echo off
setlocal

set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup compiler not found.
    exit /b 1
)

call tools\build.bat || exit /b 1
"%ISCC%" tools\squishit.iss || exit /b 1

if exist dist\SquishIt rmdir /s /q dist\SquishIt
if exist dist\SquishIt.exe del /f /q dist\SquishIt.exe

echo Installer build complete.
