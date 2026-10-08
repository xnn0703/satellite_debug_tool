@echo off
REM One-click build for Windows. Auto-installs pyinstaller if missing.
REM Prereq: Python 3.9+ installed; current dir is project root (with .venv or system python).
REM Output:  release\SatelliteDebugTool-Windows-x86_64.zip

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

REM ---- ensure pyinstaller ----
"%PY%" -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [build_windows] Installing pyinstaller...
    "%PY%" -m pip install pyinstaller || goto :err
)

REM ---- ensure runtime deps ----
"%PY%" -c "import PySide6, pyqtgraph, serial, numpy, py7zr, psutil, OpenGL" 2>nul
if errorlevel 1 (
    echo [build_windows] Installing runtime deps...
    "%PY%" -m pip install -r satellite_debug_tool\requirements.txt || goto :err
)

REM ---- validate translation catalogs and compiled QM ----
echo [build_windows] Validating TS/QM translation resources...
"%PY%" scripts\update_translations.py check || goto :err

echo [build_windows] Validating bundled platform service...
"%PY%" scripts\verify_platform_service_assets.py "satellite_debug_tool\resources\vendor\lingjing_platform_service\2604" || goto :err

REM ---- clean ----
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

echo [build_windows] Building main app...
"%PY%" -m PyInstaller --noconfirm satellite_debug_tool.spec || goto :err

echo [build_windows] Verifying built-in 3D models...
"%PY%" scripts\verify_model_assets.py "dist\SatelliteDebugTool" || goto :err

echo [build_windows] Verifying packaged platform service...
"%PY%" scripts\verify_platform_service_assets.py "dist\SatelliteDebugTool\_internal\satellite_debug_tool\resources\vendor\lingjing_platform_service\2604" || goto :err

echo [build_windows] Building updater (M11)...
"%PY%" -m PyInstaller --noconfirm updater.spec || goto :err


REM ---- M11: embed onefile updater into main install dir ----
if exist "dist\updater.exe" if exist "dist\SatelliteDebugTool" (
    echo [build_windows] Embedding updater into main install dir...
    copy /Y "dist\updater.exe" "dist\SatelliteDebugTool\updater.exe" >nul
)

REM ---- bilingual operator documentation ----
if exist "dist\SatelliteDebugTool" (
    if exist "doc\WINDOWS_QUICK_START.md" copy /Y "doc\WINDOWS_QUICK_START.md" "dist\SatelliteDebugTool\README-Windows.md" >nul
    if exist "doc\user_manual.md" copy /Y "doc\user_manual.md" "dist\SatelliteDebugTool\user_manual.md" >nul
    if exist "doc\user_manual_en.md" copy /Y "doc\user_manual_en.md" "dist\SatelliteDebugTool\user_manual_en.md" >nul
)

REM ---- archive ----
set "APP_ZIP=%RELEASE_DIR%\SatelliteDebugTool-Windows-x86_64.zip"
if exist "%APP_ZIP%" del "%APP_ZIP%"

pushd dist
powershell -NoProfile -Command "Compress-Archive -Path 'SatelliteDebugTool' -DestinationPath '%APP_ZIP%' -Force" || goto :errpop
popd

echo.
echo ==== Done. Artifacts in %RELEASE_DIR% ====
dir /b "%RELEASE_DIR%"
echo.
echo Note: first run may trigger Windows SmartScreen (unsigned),
echo       click "More info" -^> "Run anyway".
goto :eof

:errpop
popd
:err
echo [build_windows] BUILD FAILED.
exit /b 1
