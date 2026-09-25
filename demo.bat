@echo off
rem Generates demo data into a SEPARATE database (demo.sqlite3) and opens the dashboard in demo mode.
rem The real database (radar.sqlite3) is not touched and no collector runs in demo mode.
setlocal
cd /d "%~dp0"
set WWP_DEMO=1
".venv\Scripts\python.exe" -m wwp_radar demo || exit /b 1
".venv\Scripts\python.exe" -m wwp_radar serve
endlocal
