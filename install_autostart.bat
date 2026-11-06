@echo off
rem One-time setup: the ORB dashboard opens by itself (minimized, no browser tab) each time you log into Windows.
rem The dashboard then auto-starts the bot on trading days, keeps the PC awake while it runs, and answers Telegram.
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
(
echo @echo off
echo rem Opens the ORB dashboard at login. Delete this file to stop that.
echo cd /d "%~dp0"
echo start "ORB dashboard" /min cmd /k python dashboard.py --no-browser
)> "%STARTUP%\ORB dashboard.bat"
if exist "%STARTUP%\ORB dashboard.bat" (
  echo Installed. The dashboard will open by itself when you log into Windows.
  echo To undo it, run uninstall_autostart.bat
) else (
  echo Could not write to your Startup folder: %STARTUP%
)
pause
