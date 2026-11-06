# QQQ opening-range study (2026-10-29)

Question: does the bot's opening-range breakout, or one of 96 pre-declared variants of it, beat a coin flip on QQQ
after costs? Rules were chosen with 2016-2020 only; 2021-2026 was kept back for one final test.

Result: no. None of the 96 variants had a positive average result on 2016-2018 after costs, so, by the protocol
written before the search (PROTOCOL.md), nothing was taken to the test years. They remain unused.
The current rules also did worse than entering at a random minute with the same stop and target (2016-2020).

Files: PROTOCOL.md (fixed before the search), sim.py (fast simulator; check_baseline.py proves it matches
engine.py trade for trade), search.py (the 96 variants), search_results.json, diagnose.py (coin-flip comparison).
Run from the Orb_bot folder:  python research\check_baseline.py   /   python research\search.py

## Study 2 (PROTOCOL_2.md): choose on QQQ 2020-01 – 2026-09, test on SPY / IWM / DIA
Result: no. None of the 96 variants made money over 2020–2026 after costs (best -0.018R a trade), so nothing
qualified and the test markets were never downloaded or looked at. They remain unused.
On 2020–2026, both the current rules (-0.170R) and the best variant (-0.018R) did worse than random entry minutes
with the same stop, target and costs, in 100 % of 300 random runs.
Files: PROTOCOL_2.md, search2.py, search2_results.json (search2.py reads QQQ 2019-12..2026-09 from a "qqq2020"
folder next to it if present, else from bt_cache).

## Study 3 (PROTOCOL_3.md): 1,032 variants of 10 strategy families, all day, 26 stocks — IN PROGRESS
lab.py = engine (its trade simulator matches engine.py on all 616 QQQ trades), study3.py = the run.
1. run_fetch_data.bat downloads the data into research\data.
2. python research\study3.py choose  -> discovery 2016-2021, luck test, validation 2022-2023, pick3.json (locked)
3. python research\study3.py test    -> the one run on 2024-01..2026-09

### Study 3 result (2026-10-29)
Luck test failed: best variant t = 3.31, p = 0.149 (needed < 0.05), so by the protocol nothing went to validation and
2022-2026 stays unused. Only 55 of 1,032 variants were positive after costs; every family's median was negative.
The best idea, in all 10 top variants: BUY a stock that gaps DOWN >= 2% at the first minute's close, stop 0.2 x ATR,
target 1R (F5_gap(2.0, -1, 0, 0.2, 1, 0, 1)): +0.168R over 1,219 discovery trades, positive in all 6 years and on 22
of 26 stocks, not driven by outliers. Not proven: a pattern this strong appears by luck ~15% of the time among 1,032
tries, and real spreads in the opening minute may be wider than the 3 bps assumed.
These 10 variants are on forward_watch.json: daily.py tests them on every new day from now on.
