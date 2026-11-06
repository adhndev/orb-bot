@echo off
rem Opens the control panel at http://127.0.0.1:8765 . Closing this window closes the panel, NOT the bot.
cd /d "%~dp0"
python dashboard.py
pause
