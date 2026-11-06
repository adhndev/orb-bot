@echo off
rem Stops the dashboard from opening at Windows login. The bot itself is not touched.
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ORB dashboard.bat" 2>nul
echo Removed: the dashboard no longer opens at login.
pause
