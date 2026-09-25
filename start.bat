@echo off
rem WWP Launch Radar - starts the dashboard and the collector (single process).
rem Collection runs only while this window is open and the computer is awake.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [BLAD] Brak .venv - uruchom najpierw setup.bat
  exit /b 1
)
title WWP Launch Radar
".venv\Scripts\python.exe" -m wwp_radar serve
endlocal
