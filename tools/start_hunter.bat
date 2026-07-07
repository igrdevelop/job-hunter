@echo off
title Job Hunter Bot
rem Repo root = parent of this script's directory (works from any checkout path)
cd /d %~dp0..

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
