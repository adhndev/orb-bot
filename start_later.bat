@echo off
rem Double-click and leave this window open. At 18:00 (this PC's clock) it opens the dashboard; the dashboard then
rem starts the bot at 08:45 New York (18:15 India time until Nov 1, 19:15 after). Keeps the PC awake while it waits.
rem Another time: edit 18:00 below.
cd /d "%~dp0"
python start_later.py --at 18:00
pause
