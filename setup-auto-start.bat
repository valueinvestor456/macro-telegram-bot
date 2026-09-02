@echo off
REM This script sets up Windows Task Scheduler to run the bot automatically on startup

setlocal enabledelayedexpansion

echo Setting up automatic bot startup...
echo.

REM Get full path to RUN-BOT.bat
cd /d "%~dp0"
set BOT_PATH=%cd%\RUN-BOT.bat

echo Bot path: %BOT_PATH%
echo.

REM Create Task Scheduler task to run bot on startup
schtasks /create /tn "MacroTelegramBot" /tr "%BOT_PATH%" /sc onstart /ru SYSTEM /f

if errorlevel 1 (
    echo ERROR: Failed to create task. Make sure you run this as Administrator.
    pause
    exit /b 1
)

echo SUCCESS! Bot will now start automatically on Windows startup.
echo.
echo To stop the bot:
echo   - Open Task Scheduler
echo   - Find "MacroTelegramBot"
echo   - Right-click and select "End"
echo.
echo Or run: schtasks /delete /tn "MacroTelegramBot" /f
echo.
pause
