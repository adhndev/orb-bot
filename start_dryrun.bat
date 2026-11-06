@echo off
rem Dry run: same bot and watchdog, but NO orders are sent.
cd /d "%~dp0"
start "ORB watchdog" cmd /k python watchdog.py
python orb_bot.py
pause
