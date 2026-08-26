@echo off
setlocal enabledelayedexpansion

echo Starting Telegram Macro Bot...
echo This window will stay open and restart if the bot crashes.
echo Press Ctrl+C to stop.
echo.

:loop
echo.
echo [%date% %time%] Starting bot...
python main.py

REM If bot exits, wait 5 seconds and restart
echo.
echo [%date% %time%] Bot stopped unexpectedly. Restarting in 5 seconds...
timeout /t 5 /nobreak
goto loop

pause
