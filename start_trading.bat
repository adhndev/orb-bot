@echo off
rem Starts the watchdog (own window) and then the bot (paper orders). Close both windows to stop.
cd /d "%~dp0"
start "ORB watchdog" cmd /k python watchdog.py --flatten
python orb_bot.py --submit
pause
