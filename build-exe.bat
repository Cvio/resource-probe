@echo off
REM Builds resource_probe.exe - a single portable file that needs no Python
REM on the machines you measure. Run this once, on a machine you control.
REM
REM Output: dist\resource_probe.exe  (copy this to your USB drive)

setlocal

echo.
echo Building portable resource_probe.exe
echo ====================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: python not found on PATH.
    echo Install Python 3.8+ from python.org, then run this again.
    exit /b 1
)

echo Installing build dependencies...
python -m pip install --quiet --upgrade pyinstaller psutil
if errorlevel 1 (
    echo ERROR: failed to install pyinstaller/psutil.
    exit /b 1
)

echo Building (this takes a minute)...
python -m PyInstaller --onefile --name resource_probe --clean ^
    --console resource_probe.py
if errorlevel 1 (
    echo ERROR: build failed.
    exit /b 1
)

echo.
echo ====================================
echo Done.
echo.
echo   dist\resource_probe.exe
echo.
echo Copy that single file to your USB drive. On any Windows machine:
echo.
echo   E:\resource_probe.exe record --label myapp --duration 120
echo.
echo No Python, no admin rights, nothing installed on the host.
echo.
echo Note: the exe is Windows/x64-specific. Build separately for ARM,
echo macOS, or Linux if you need those.
echo.

endlocal
