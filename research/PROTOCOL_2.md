# Study 2 — choose on QQQ 2020–2026, test on markets never tested (fixed 2026-10-29, before any run)

Why other markets: every year 2016–2026 of QQQ has now been looked at (study 1 and the full QQQ backtest), so no
unseen QQQ years remain. SPY, IWM and DIA have never been run with this strategy in any form.
Same data (Alpaca SIP 1-minute, 9:30–16:00), same costs, same sizing, same 96 variants as PROTOCOL.md.

## Choosing — QQQ 2020-01 to 2026-09 only (mechanical)
1. Keep variants with >= 150 trades over 2020–2026 and avg R > 0 after costs in BOTH halves:
   2020-01 – 2022-12 and 2023-01 – 2026-09.
2. Pick the one with the highest avg R over all of 2020–2026.
3. If none qualifies: report that, and do not download or look at the test markets.
Note: study 1 already showed every variant lost on QQQ 2016–2018; that is known and not used here.

## Test — SPY, IWM, DIA, 2016-01 to 2026-09, the picked rules unchanged, run once
Pass (all needed), on the three markets' trades pooled:
- Average R > 0 after all costs; bootstrap chance the true average R > 0 is >= 90 %.
- Beats the random-entry control (same days, stop distance, target, costs; random minute, and random direction for
  two-sided rules) in >= 95 % of 500 runs.
- Average R > 0 in at least 2 of the 3 markets on its own.
Caveat to report: these indexes move together, so this is a test of whether the edge generalises, not three
independent samples.
