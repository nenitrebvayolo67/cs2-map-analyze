@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
title Mappio DIY

if not exist ".venv\Scripts\python.exe" (
  echo Aplikace jeste neni nainstalovana - spoustim SETUP.bat...
  call SETUP.bat
  if not exist ".venv\Scripts\python.exe" exit /b 1
)
if not exist ".env" (
  echo Chybi nastaveni - spoustim pruvodce...
  ".venv\Scripts\python.exe" -m app.setup_env
)

echo Spoustim Mappio DIY... prohlizec se otevre sam.
echo Toto okno nech otevrene. Zavrenim aplikaci ukoncis.
echo.
".venv\Scripts\python.exe" -m app.server
echo.
echo Aplikace skoncila.
pause
