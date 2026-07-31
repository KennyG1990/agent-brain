@echo off
REM Double-click this. It keeps the window open no matter what happens.
title Agent Brain - publish to GitHub
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0publish.ps1"
echo.
echo ============================================================
echo   Window kept open so you can read the messages above.
echo   A copy was also saved to:  publish-log.txt
echo ============================================================
echo.
pause
