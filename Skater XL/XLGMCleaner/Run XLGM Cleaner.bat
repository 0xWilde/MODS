@echo off
title XLGM Cleaner
cd /d "%~dp0"
set "PY=%~dp0python\python.exe"
if exist "%PY%" goto run
where python >nul 2>nul
if errorlevel 1 (
  echo The bundled python folder is missing and Python is not installed.
  echo Re-download the XLGMCleaner zip, or install Python from https://www.python.org/downloads/
  pause
  exit /b 1
)
set "PY=python"
:run
"%PY%" -I "%~dp0xlgm_cleaner.py" %*
echo.
pause
