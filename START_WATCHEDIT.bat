@echo off
setlocal enabledelayedexpansion
title 9router_WatchEdit

cd /d "%~dp0\9router_WatchEdit"

:: 1. Check Python executable
where python >nul 2>nul
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; [System.Windows.Forms.MessageBox]::Show('Python is not installed or not in your system PATH.\n\nPlease install Python 3.10+ to run 9router_WatchEdit.', '9router_WatchEdit Launch Error', 'OK', 'Error')"
    exit /b 1
)

:: 2. Check dependencies (PySide6, httpx)
python -c "import PySide6; import httpx" >nul 2>nul
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; [System.Windows.Forms.MessageBox]::Show('Required dependencies are missing (PySide6 or httpx).\n\nPlease run:\npip install -r ../requirements.txt', '9router_WatchEdit Dependency Error', 'OK', 'Warning')"
    exit /b 1
)

:: 3. Launch application in background
start "" pythonw run.py
exit /b 0
