"""
strategies.py - live versions of research strategies. Each must match its research twin in research/lab.py
(test_strategies.py checks that on real data).

GAP-DOWN REVERSION ("gap_fade"), research/lab.py F5_gap(2.0, -1, 0, 0.2, 1, 0, 1):
  * A stock opens at least 2 % below yesterday's close.
  * Signal on the close of the first minute (9:30-9:31 New York); buy at the next price.
  * Stop = signal price - 0.2 x ATR(14) (average true range of the last 14 sessions, regular hours).
  * Target = 1R above the signal price. Long only. Flat by 15:55 like everything else.
Status: the best idea of study 3, NOT proven (luck test p = 0.149). Paper only, judged by success_criteria_gap.json.
"""
import math

import numpy as np

GAP_DEFAULTS = {"min_gap_pct": 2.0, "atr_k": 0.2, "rr": 1.0, "last_entry": "09:34", "atr_days": 14}


def daily_facts(sym, atr_days=14):
    """From a research/lab.Sym holding the PREVIOUS sessions (oldest first, split-adjusted):
    {'prev_close', 'atr', 'last_day'} for the next session, exactly as lab computes them for that day."""
    if len(sym.days) < atr_days + 1:
        return None
    last = lambda r: r[~np.isnan(r)][-1] if (~np.isnan(r)).any() else np.nan
    close = np.array([last(r) for r in sym.C])
    high, low = np.nanmax(sym.H, 1), np.nanmin(sym.L, 1)
    prev = np.r_[np.nan, close[:-1]]
    tr = np.nanmax(np.vstack([high - low, np.abs(high - prev), np.abs(low - prev)]), 0)
    atr = float(np.mean(tr[-atr_days:]))
    if not (atr > 0) or np.isnan(close[-1]):
        return None
    return {"prev_close": float(close[-1]), "atr": atr, "last_day": str(sym.days[-1])}


def gap_signal(facts, first_open, first_close, p=None):
    """The gap-down entry, or None. first_open / first_close: the 9:30 minute bar."""
    p = dict(GAP_DEFAULTS, **(p or {}))
    if not facts or not (first_open > 0 and first_close > 0):
        return None
    gap = (first_open - facts["prev_close"]) / facts["prev_close"] * 100
    if gap > -p["min_gap_pct"]:
        return None
    price = float(first_close)
    raw_stop = price - p["atr_k"] * facts["atr"]
    if price - raw_stop < 0.05 or price - raw_stop < price * 0.001:
        return None                                                          # too tight to survive the spread
    stop = math.floor(raw_stop * 100) / 100                                  # whole cents, never closer than planned
    risk = price - stop
    return {"entry": price, "stop": stop, "target": round(price + p["rr"] * risk, 2), "risk": risk,
            "gap": round(gap, 2)}


def gap_reprice(sig, fresh_price):
    """The stop and target stay where the signal put them (as in the backtest). A newer price only changes the risk
    per share; it is refused if it already sits at or beyond the stop or the target."""
    if fresh_price is None or fresh_price == sig["entry"]:
        return sig
    if fresh_price <= sig["stop"] or fresh_price >= sig["target"]:
        return None
    risk = fresh_price - sig["stop"]
    if risk < 0.05 or risk < fresh_price * 0.001:
        return None
    return dict(sig, entry=float(fresh_price), risk=risk)
