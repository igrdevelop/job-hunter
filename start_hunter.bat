@echo off
title Job Hunter Bot
cd /d D:\LearningProject\Claude

:loop
echo.
echo [%date% %time%] ========================================
echo [%date% %time%] Starting Job Hunter Bot...
echo [%date% %time%] ========================================
echo.
python hunter.py
echo.
echo [%date% %time%] Bot stopped. Restarting in 30 seconds...
echo [%date% %time%] Press Ctrl+C to exit.
timeout /t 30 /nobreak
goto loop
