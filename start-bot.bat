@echo off
setlocal enabledelayedexpansion

echo ========================================
echo Macro Telegram Bot - Auto Setup
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH
    echo.
    echo Please install Python from: https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation
    echo.
    pause
    exit /b 1
)

echo [OK] Python found
python --version
echo.

REM Install requirements
echo [INSTALLING] Python dependencies...
pip install -q yfinance requests beautifulsoup4 schedule pytz
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies
    pause
    exit /b 1
)
echo [OK] Dependencies installed
echo.

REM Require a private local .env file; never embed credentials in this script.
if not exist .env (
    echo [ERROR] .env file is missing.
    echo Create it using the instructions in README.md.
    pause
    exit /b 1
)
echo [OK] Private .env file found
echo.
REM Delete old offset file to start fresh
if exist telegram_update_offset.json (
    del telegram_update_offset.json
    echo [OK] Reset message offset (will process pending messages)
)
echo.

REM Run the bot
echo ========================================
echo Starting bot...
echo ========================================
echo Send /usd in Telegram and watch this terminal
echo Press Ctrl+C to stop
echo.
python main.py

pause
