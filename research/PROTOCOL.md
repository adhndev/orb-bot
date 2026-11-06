# QQQ opening-range study — protocol, fixed before any variant was run (2026-10-29)

Data: QQQ 1-minute bars, full-market (SIP) feed from Alpaca, 9:30–16:00 New York.
Costs on every trade: slippage max(3 bps, 1 cent) on entry, stop and flatten fills; regulatory fees (SEC $20.60/M on
sales, FINRA TAF $0.000166/share sold). One trade a day at most. Sizing as the live bot ($250 max loss, $10k cap).

## Periods
- Train: 2016-01 – 2018-12. Validation: 2019-01 – 2020-12. (Only these two are loaded while choosing.)
- Test: 2021-01 – 2026-09, loaded only after the choice below is written down. Run once.
  Caveat: the CURRENT rules were already run on 2016–2026 (yearly results seen). The variants below were not.

## The 96 variants (nothing else is tried)
or_min {5, 15, 30} × side {long, both} × stop {mid, far} × target R {1, 2, 3, none = hold to 15:55} × trend filter {off, on}.
Everything else stays as the live bot: range 0.3–3 % of price, max extension 0.5 × range, min risk 0.1 %, entries
until 15:30, flat by 15:55.

## Choosing (mechanical)
1. Keep variants with >= 100 train trades and train avg R > 0 after costs.
2. Rank by train avg R; take the top 5.
3. Pick the one with the highest validation avg R, which must be > 0 with >= 60 validation trades.
4. If nothing qualifies: report that no variant showed an edge; do not loosen the rules.

## "Beats a coin flip" — the pass bar on the test period (all three needed)
- Average R per trade > 0 after all costs.
- Bootstrap chance that the true average R is above zero >= 90 %.
- Beats the random-entry control in >= 95 % of 500 runs. Control = the same days, the same stop distance and target
  multiple, the same costs, but a random entry minute in the same window (and a random direction for two-sided rules).
