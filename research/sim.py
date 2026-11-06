"""Fast opening-range simulator for the QQQ study. Mirrors engine.py exactly for the bot's current rules
(verified trade by trade in check_baseline.py), and adds the pre-declared variants:
  or_min  5 / 15 / 30      opening-range length
  side    long / both      long-only (today's bot) or also short breakdowns
  stop    mid / far        range midpoint (today) or the far side of the range
  target  1 / 2 / 3 / None reward:risk, None = hold to the 15:55 flat-by
  trend   False / True     only trade in the direction of yesterday's close vs its 20-day average
Costs: slippage max(3 bps, 1 cent) on entry, stop and flatten fills (not on target limit fills), plus regulatory fees
(engine.trade_fees). One trade a day at most (single symbol, first signal), like the backtester.
"""
import math
import sys
from datetime import time as dtime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent if (HERE.parent / "orb_bot.py").exists() else HERE.parent / "proj"   # Orb_bot/research on your PC
sys.path.insert(0, str(PROJ))
import engine  # noqa: E402
import orb_bot as b  # noqa: E402

CFG = __import__("json").loads((PROJ / "config.json").read_text())
BT = CFG["backtest"]
FLAT = dtime(15, 55)
LAST_ENTRY = dtime(15, 30)


def load(folder, last_year=None):
    """last_year: ignore files after this year (the study chose its rules with 2016-2020 only)."""
    days = {}
    for f in sorted(Path(folder).glob("QQQ_sip_*.csv")):
        if last_year and int(f.stem.split("_")[-1][:4]) > last_year:
            continue
        df = pd.read_csv(f, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(b.NY)
        for d, g in df.groupby(df.index.date):
            days[d] = g.sort_index()
    # yesterday's close vs its 20-day average, known before today's open
    closes = pd.Series({d: float(g["close"].iloc[-1]) for d, g in days.items()}).sort_index()
    sma = closes.rolling(20).mean()
    trend = {}
    prev = list(closes.index)
    for i, d in enumerate(prev):
        if i >= 20:
            trend[d] = 1 if closes.iloc[i - 1] > sma.iloc[i - 1] else -1
    return days, trend


def slip(px):
    return max(px * BT["slippage_bps"] / 10000.0, BT["min_slippage_usd"])


class Day:
    """One day's bars as numpy arrays (9:30-15:59 New York)."""
    __slots__ = ("o", "h", "l", "c", "end", "t", "date")

    def __init__(self, date, g):
        self.date = date
        self.o, self.h, self.l, self.c = (g[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        mins = (g.index.hour * 60 + g.index.minute).to_numpy()
        self.t = mins                      # bar start, minutes after midnight
        self.end = mins + 1                # bar end


def _m(t):
    return t.hour * 60 + t.minute


def opening_range(d, or_min):
    w = (d.t >= 570) & (d.t < 570 + or_min)
    if w.sum() < max(3, or_min // 2 + 1):          # 15 min -> 8 bars, as config min_or_bars
        return None
    hi, lo = float(d.h[w].max()), float(d.l[w].min())
    return hi, lo


def find_signal(d, p, trend_dir):
    rng_ = opening_range(d, p["or_min"])
    if rng_ is None:
        return None
    hi, lo = rng_
    rng = hi - lo
    if not (CFG["min_range_pct"] <= rng / hi * 100 <= CFG["max_range_pct"]):
        return None
    mid = (hi + lo) / 2
    sides = [1] if p["side"] == "long" else [1, -1]
    if p["trend"]:
        sides = [s for s in sides if trend_dir is not None and s == trend_dir]
        if not sides:
            return None
    lo_t, hi_t = 570 + p["or_min"], _m(LAST_ENTRY)
    ext = CFG["max_extension_of_range"] * rng
    n = len(d.c)
    for i in range(n - 1):
        if d.end[i] < lo_t or d.end[i] >= hi_t:
            continue
        price = float(d.c[i])
        for s in sides:
            if s == 1:
                if price <= hi or price - hi > ext:
                    continue
                stop = math.floor(mid * 100) / 100 if p["stop"] == "mid" else math.floor(lo * 100) / 100
                risk = price - stop
            else:
                if price >= lo or lo - price > ext:
                    continue
                stop = math.ceil(mid * 100) / 100 if p["stop"] == "mid" else math.ceil(hi * 100) / 100
                risk = stop - price
            if risk < 0.05 or risk / price * 100 < CFG["min_risk_pct"]:
                continue
            if d.t[i + 1] - d.t[i] > 3:
                continue
            return {"i": i, "side": s, "price": price, "stop": stop, "risk": risk}
    return None


def simulate_exit(d, fi, side, stop, target):
    """Bars from fi (the fill bar) on. Returns (exit_px, reason). Mirrors engine.simulate_exit (and its mirror image)."""
    flat = _m(FLAT)
    for j in range(fi, len(d.c)):
        o, h, l = float(d.o[j]), float(d.h[j]), float(d.l[j])
        if d.t[j] >= flat:
            return o - side * slip(o), "flatten"
        if side == 1:
            if j > fi:
                if o <= stop:
                    return o - slip(o), "stop"
                if target is not None and o >= target:
                    return o, "target"
            if l <= stop:
                return stop - slip(stop), "stop"
            if target is not None and h >= target:
                return target, "target"
        else:
            if j > fi:
                if o >= stop:
                    return o + slip(o), "stop"
                if target is not None and o <= target:
                    return o, "target"
            if h >= stop:
                return stop + slip(stop), "stop"
            if target is not None and l <= target:
                return target, "target"
    c = d.c[-1]
    return c - side * slip(c), "data_end"


def trade(d, side, fi, stop, risk, rr, price_for_target):
    """Fill at the open of bar fi, then exit. R uses the signal-price risk, like engine._trade."""
    fo = float(d.o[fi])
    entry = fo + side * slip(fo)
    price_for_target, risk = float(price_for_target), float(risk)   # Python rounding, exactly as the bot
    target = None if rr is None else round(price_for_target + side * rr * risk, 2)
    px, reason = simulate_exit(d, fi, side, stop, target)
    qty, _ = b.size_position(price_for_target, risk, CFG, 0)        # the bot sizes from the signal price
    if qty < 1:
        return None
    sale = px if side == 1 else entry
    fees = engine.trade_fees(entry, sale, qty, BT)
    pnl = side * (px - entry) * qty - fees
    return {"date": d.date, "side": side, "qty": qty, "entry": entry, "exit": px, "reason": reason,
            "pnl": pnl, "fees": fees, "r": pnl / (qty * risk)}


def run(days, trend, p):
    out = []
    for date, d in days.items():
        if len(d.c) < 30:
            continue                                   # engine.simulate_day skips thin days too
        sig = find_signal(d, p, trend.get(date))
        if sig is None:
            continue
        t = trade(d, sig["side"], sig["i"] + 1, sig["stop"], sig["risk"], p["target"], sig["price"])
        if t:
            t["risk_ps"], t["signal_i"] = sig["risk"], sig["i"]
            out.append(t)
    return out


def coin_flip(days, trades, p, n_runs=500, seed=7):
    """The same days, the same stop distance and target multiple, the same costs, but a RANDOM entry minute
    (inside the same entry window) and, for two-sided rules, a random direction. Returns the avg R of each run."""
    rnd = np.random.default_rng(seed)
    lo_t, hi_t = 570 + p["or_min"], _m(LAST_ENTRY)
    res = []
    for _ in range(n_runs):
        rs = []
        for t in trades:
            d = days[t["date"]]
            ok = np.where((d.end >= lo_t) & (d.end < hi_t))[0]
            ok = ok[ok < len(d.c) - 1]
            if not len(ok):
                continue
            i = int(rnd.choice(ok))
            side = 1 if p["side"] == "long" else int(rnd.choice([1, -1]))
            price, risk = d.c[i], t["risk_ps"]
            stop = round(price - side * risk, 2)
            tr = trade(d, side, i + 1, stop, risk, p["target"], price)
            if tr:
                rs.append(tr["r"])
        res.append(float(np.mean(rs)) if rs else 0.0)
    return np.array(res)


def summary(trades):
    if not trades:
        return {"n": 0}
    r = np.array([t["r"] for t in trades])
    pnl = np.array([t["pnl"] for t in trades])
    gp, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    rnd = np.random.default_rng(1)
    boots = r[rnd.integers(0, len(r), (4000, len(r)))].mean(1)
    return {"n": len(r), "avg_r": round(float(r.mean()), 3), "win": round(float((r > 0).mean()), 3),
            "pf": round(float(gp / gl), 2) if gl else 99.0, "pnl": round(float(pnl.sum()), 0),
            "p_pos": round(float((boots > 0).mean()), 3),
            "lo95": round(float(np.percentile(boots, 2.5)), 3), "hi95": round(float(np.percentile(boots, 97.5)), 3)}
