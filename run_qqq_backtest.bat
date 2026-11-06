@echo off
rem QQQ opening-range backtest, 2016 to 2026-10-28, full-market (SIP) 1-minute data from Alpaca (free).
rem Counts slippage on every fill and regulatory fees. Holdout = Nov 2024 onward (look at it once).
rem Results: backtest_report_qqq.txt, backtest_summary_qqq.json, backtest_trades_qqq.csv
rem The first run downloads ~10 years of data into bt_cache (a few minutes); later runs read the cache.
cd /d "%~dp0"
python backtest.py --symbols QQQ --start 2016-01-01 --end 2026-10-28 --holdout-from 2024-11-01 --feed sip --tag qqq
pause
