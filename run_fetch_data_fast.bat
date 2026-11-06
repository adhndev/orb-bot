@echo off
rem Downloads ~10 years of 1-minute bars (2016 to last month) for every stock in the universe, plus IWM and DIA,
rem from Alpaca (free, full-market SIP feed) into research\data. 3 stocks at a time under one shared speed limit
rem (the free plan allows 200 requests a minute). Safe to close and run again: finished stocks are skipped.
cd /d "%~dp0"
python research\fetch_data.py --workers 3
pause
