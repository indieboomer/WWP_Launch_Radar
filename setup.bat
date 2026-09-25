@echo off
rem WWP Launch Radar - one-time setup: creates .venv and installs the locked dependencies.
setlocal
cd /d "%~dp0"

set "PY="
for %%V in (3.13 3.12 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [BLAD] Nie znaleziono Pythona 3.10-3.13. Zainstaluj go z https://www.python.org/downloads/windows/
  echo        Zaznacz "Add python.exe to PATH" oraz "py launcher".
  exit /b 1
)
echo Uzywam: %PY%

if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || (echo [BLAD] Nie udalo sie utworzyc .venv & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --upgrade pip || exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt || exit /b 1

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Utworzono plik .env z .env.example - mozesz go edytowac.
)
echo.
echo Gotowe. Uruchom start.bat, a potem otworz http://127.0.0.1:8765
endlocal
