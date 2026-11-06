# Study 3 — many strategy families, all day, 26 stocks (fixed 2026-10-29, before any family was run)

## Data
1-minute bars, Alpaca SIP (full market), 9:30–16:00 New York, 2016-01 to 2026-09, for the 24 universe stocks
plus IWM and DIA (research\data). One trade per stock per day per variant (the first signal).
Costs on every trade: slippage max(3 bps, 1 cent) on entry, stop and time exits (none on target limit fills);
SEC fee $20.60/M on sales, FINRA TAF $0.000166/share sold; sized like the bot ($250 max loss, $10k cap).
Entries until 15:30, everything flat at 15:55, like the bot. A stop must be at least 0.1 % of price away.

## Periods
- Discovery: 2016-01 – 2021-12 (all choosing happens here)
- Validation: 2022-01 – 2023-12 (only the shortlisted few are run here)
- Test: 2024-01 – 2026-09 — run ONCE, for ONE strategy, after it is written down.
While building the engine, only discovery years may be used.

## Families (every variant runs as "both sides" and as "long only")
F1 opening-range breakout · F2 failed-breakout fade · F3 VWAP trend pullback · F4 VWAP stretch mean reversion ·
F5 opening gap (go / fade) · F6 intraday momentum (first 30 min predicts the last half hour) ·
F7 prior-day high/low breakout · F8 late-day trend continuation · F9 5-minute RSI mean reversion ·
F10 relative strength vs SPY. The parameter grid is in lab.py and is not changed after the first run.

## Choosing (mechanical)
1. Luck test on discovery (White's Reality Check, stationary bootstrap of daily results, 1,000 resamples):
   is the best variant better than the best of the same number of no-edge variants would be by luck?
   If p >= 0.05: stop and report "no edge beyond luck". Nothing goes further.
2. Shortlist: the top 10 variants by discovery t-statistic (day-clustered), each with >= 300 discovery trades and
   avg R > 0 after costs.
3. Validation: keep shortlisted variants with avg R > 0 and t > 1 on 2022–2023 (>= 100 trades);
   pick the one with the highest validation avg R. If none: stop and report.

## Test (2024-01 – 2026-09, once) — "beats a coin flip" means all of:
- avg R > 0 after all costs, and bootstrap chance the true avg R > 0 >= 90 %;
- beats the random-entry control (same stocks and days, same stop distance, target and costs, random entry minute
  in the strategy's window, random side for two-sided rules) in >= 95 % of 500 runs.
Even a pass is a reason to paper trade it, not proof: the test years are recent and few.
