@echo off
rem ===========================================================================
rem  Stock Trader - double-click this file to set everything up and start.
rem  First run downloads what it needs (a few minutes); after that it's quick.
rem ===========================================================================
setlocal
cd /d "%~dp0"
title Stock Trader

rem ---- find Python (the "py" launcher comes with the python.org installer) --
set "PY=py -3"
py -3 --version >nul 2>nul
if errorlevel 1 (
    set "PY=python"
    python --version >nul 2>nul
)
if errorlevel 1 (
    echo.
    echo  Python is not installed yet. A download page will now open.
    echo.
    echo    1. Click the yellow "Download Python" button and run the installer
    echo    2. IMPORTANT: tick "Add python.exe to PATH" on the first screen
    echo    3. When it finishes, double-click start_bot.bat again
    echo.
    start "" https://www.python.org/downloads/
    pause
    exit /b 1
)

rem ---- first-time setup: private environment + libraries --------------------
if not exist ".venv\Scripts\python.exe" (
    echo First-time setup: preparing the app, this takes a few minutes...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo Setup failed while creating the environment.
        pause
        exit /b 1
    )
)
if not exist ".venv\installed.marker" (
    ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Installing libraries failed - check your internet connection and
        echo run this file again.
        pause
        exit /b 1
    )
    echo ok> ".venv\installed.marker"
)

rem ---- start the dashboard and open the browser -----------------------------
echo.
echo  Starting Stock Trader... your browser will open in a moment.
echo  Keep this black window open while you use it - closing it stops the bot.
echo.
start "" /min cmd /c "timeout /t 3 >nul && start http://127.0.0.1:8000"

:run
".venv\Scripts\python.exe" main.py serve
if %errorlevel%==42 (
    echo.
    echo  Update installed - restarting Stock Trader...
    echo.
    goto run
)
echo.
echo Stock Trader stopped.
pause
