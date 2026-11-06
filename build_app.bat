@echo off
rem Builds the ORB bot app: OrbBot.exe, which runs on any Windows PC without Python.
rem Uses the Python already on this PC (3.8 to 3.15) for the build only. Takes 5-10 minutes the first time.
rem The result goes to %LOCALAPPDATA%\OrbBotBuild\dist (outside OneDrive, so the big build files are not synced).
setlocal
cd /d "%~dp0"
set "OUT=%LOCALAPPDATA%\OrbBotBuild"
set "VENV=%OUT%\build_env"

echo.
echo === Building the ORB bot app ===
echo.
python --version
if not errorlevel 1 goto have_python
echo Python was not found. Install it from python.org - tick "Add python.exe to PATH" - then run this again.
goto fail

:have_python
tasklist /FI "IMAGENAME eq OrbBotWorker.exe" 2>nul | find /I "OrbBotWorker.exe" >nul
if not errorlevel 1 goto app_running
tasklist /FI "IMAGENAME eq OrbBot.exe" 2>nul | find /I "OrbBot.exe" >nul
if not errorlevel 1 goto app_running
if not exist "%OUT%" mkdir "%OUT%"
if exist "%VENV%\Scripts\python.exe" goto have_venv
echo Creating the build environment...
python -m venv "%VENV%"
if errorlevel 1 goto fail

:have_venv
echo Installing what the app needs (first time: a few minutes)...
"%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip
"%VENV%\Scripts\python.exe" -m pip install --quiet -r requirements.txt pyinstaller keyring truststore tzdata
if errorlevel 1 goto fail

echo Packing the app...
"%VENV%\Scripts\python.exe" -m PyInstaller OrbBot.spec --noconfirm --clean --distpath "%OUT%\dist" --workpath "%OUT%\work"
if errorlevel 1 goto fail

echo Checking the app...
"%OUT%\dist\OrbBot\OrbBotWorker.exe" --selftest
if errorlevel 1 goto fail

echo Making a zip to copy to another PC...
powershell -NoProfile -Command "Compress-Archive -Path '%OUT%\dist\OrbBot' -DestinationPath '%OUT%\dist\OrbBot-windows.zip' -Force"
if errorlevel 1 goto fail

echo.
echo === Done ===
echo App:  %OUT%\dist\OrbBot\OrbBot.exe   - double-click it. Keep the whole OrbBot folder together.
echo Zip:  %OUT%\dist\OrbBot-windows.zip  (unzip on another PC and double-click OrbBot.exe)
explorer "%OUT%\dist"
pause
exit /b 0

:app_running
echo.
echo The app is still running - OrbBot.exe or OrbBotWorker.exe - so its files cannot be replaced.
echo 1. In the app: if a bot is running, press "Stop and close positions" and wait until it says STOPPED.
echo 2. Press "Close app". If OrbBotWorker.exe is still in Task Manager - Ctrl+Shift+Esc - end it.
echo 3. Run build_app.bat again.
pause
exit /b 1

:fail
echo.
echo BUILD FAILED. Send Claude the last lines above.
pause
exit /b 1
