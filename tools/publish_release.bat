@echo off
:: SquishIt Release Publisher
:: Usage: publish_release.bat [options]
::   -n "release notes"     pass notes directly
::   -f notes.md            read notes from a file
::   --draft                create as draft
::   --version X.Y.Z        override version
::   --help                 show all options
::
:: Auth: set GH_TOKEN=ghp_... in your environment,
::       or place your token in .github_token at the project root.

setlocal
cd /D "%~dp0.."
call .venv\Scripts\activate.bat 2>nul
python tools\publish_release.py %*
endlocal
