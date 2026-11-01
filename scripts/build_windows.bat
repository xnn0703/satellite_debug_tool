@echo off
REM One-click build for Windows. Auto-installs pyinstaller if missing.
REM Prereq: Python 3.11 build baseline; current dir is project root (with .venv or system python).
REM Usage: build_windows.bat user^|ops^|both (default ops)
REM Output: release\satellite_terminal_manager-EDITION-windows-ARCH-vVERSION.zip

setlocal enabledelayedexpansion
cd /d "%~dp0.."

set "HERE=%CD%"
set "RELEASE_DIR=%HERE%\release"
if not exist "%RELEASE_DIR%" mkdir "%RELEASE_DIR%"

REM ---- pick python ----
set "PY="
if exist "%HERE%\.venv\Scripts\python.exe" set "PY=%HERE%\.venv\Scripts\python.exe"
if "%PY%"=="" set "PY=python"

echo [build_windows] Using Python: %PY%
"%PY%" --version || goto :err

REM ---- restore the constrained build environment ----
"%PY%" -m pip install -r requirements\build-windows.txt || goto :err

REM ---- validate translation catalogs and compiled QM ----
echo [build_windows] Validating TS/QM translation resources...
set "EDITION=%~1"
if "%EDITION%"=="" set "EDITION=ops"
"%PY%" scripts\build_editions.py "%EDITION%" || goto :err
goto :eof

:err
echo [build_windows] BUILD FAILED.
exit /b 1
