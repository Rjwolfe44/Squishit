@echo off
setlocal

:: FFmpeg full-GPL build — includes libsvtav1, libaom, libvpx, libx265, libopus, libvorbis, etc.
:: Pin to release-full so encoder availability is predictable across machines.
set "FFMPEG_URL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-full.zip"
set "OUT_DIR=vendor\ffmpeg"
if not exist "%OUT_DIR%" mkdir "%OUT_DIR%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; $zip='vendor\\ffmpeg\\ffmpeg.zip'; Invoke-WebRequest -Uri '%FFMPEG_URL%' -OutFile $zip; Expand-Archive -Path $zip -DestinationPath 'vendor\\ffmpeg\\_extract' -Force; $ffmpeg=Get-ChildItem 'vendor\\ffmpeg\\_extract' -Recurse -Filter ffmpeg.exe | Select-Object -First 1; $ffprobe=Get-ChildItem 'vendor\\ffmpeg\\_extract' -Recurse -Filter ffprobe.exe | Select-Object -First 1; Copy-Item $ffmpeg.FullName 'vendor\\ffmpeg\\ffmpeg.exe' -Force; Copy-Item $ffprobe.FullName 'vendor\\ffmpeg\\ffprobe.exe' -Force"

echo FFmpeg (full-GPL build) fetched into vendor\ffmpeg.
