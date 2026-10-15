@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
title Mappio DIY - instalace

echo.
echo === Mappio DIY - instalace ===
echo.

set "PY="
py -3.12 -c "import sys" >nul 2>nul && set "PY=py -3.12"
if not defined PY py -3.13 -c "import sys" >nul 2>nul && set "PY=py -3.13"
if not defined PY py -3.11 -c "import sys" >nul 2>nul && set "PY=py -3.11"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul && set "PY=python"

if not defined PY (
  echo Python 3.11+ nebyl nalezen. Zkousim ho nainstalovat pres winget...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  if errorlevel 1 (
    echo.
    echo Python se nepodarilo nainstalovat automaticky.
    echo Stahni ho z https://www.python.org/downloads/ ^(zaskrtni "Add python.exe to PATH"^)
    echo a spust SETUP.bat znovu.
    pause
    exit /b 1
  )
  echo.
  echo Python je nainstalovany. ZAVRI toto okno a spust SETUP.bat znovu.
  pause
  exit /b 0
)

echo Pouzivam: %PY%
if not exist ".venv\Scripts\python.exe" (
  echo Vytvarim virtualni prostredi...
  %PY% -m venv .venv
  if errorlevel 1 ( echo Nepodarilo se vytvorit virtualni prostredi. & pause & exit /b 1 )
)

echo Instaluji knihovny ^(prvne to muze trvat par minut^)...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Instalace knihoven selhala. Zkontroluj pripojeni k internetu a spust SETUP.bat znovu.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -c "import demoparser2, pandas, zstandard" 2>nul
if errorlevel 1 ( echo Knihovny se nenacetly spravne. & pause & exit /b 1 )

".venv\Scripts\python.exe" -m app.setup_env
echo.
echo Hotovo. Aplikaci spoustis souborem START.bat.
pause
