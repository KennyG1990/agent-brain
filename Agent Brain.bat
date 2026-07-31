@echo off
REM Agent Brain - double-click to launch. Pin this to your taskbar.
REM Uses pythonw so no console window sits behind the app.

setlocal
cd /d "%~dp0"

where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw "%~dp0app.py"
    goto :eof
)

where python >nul 2>&1
if %errorlevel%==0 (
    echo pythonw not found - running with python so you can see any error...
    python "%~dp0app.py"
    pause
    goto :eof
)

echo.
echo   Python is not on PATH, so Agent Brain cannot start.
echo.
echo   Install Python 3.10+ from https://www.python.org/downloads/
echo   and TICK BOTH BOXES in the installer:
echo       [x] Add python.exe to PATH
echo       [x] tcl/tk and IDLE
echo.
pause
