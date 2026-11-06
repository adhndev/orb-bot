"""
engine.py - replays the bot's opening-range rules on historical 1-minute bars.

Used by backtest.py (many past days) and journal.py (every night, on today's bars,
for the "no Claude" control). It calls the SAME functions the live bot uses
(opening_range, breakout_signal, size_position), so the rules cannot drift apart.

Assumptions, deliberately pessimistic:
  * You are filled at the OPEN of the bar after the signal bar, plus slippage.
  * If a bar touches both stop and target, the stop is assumed to hit first.
  * Stop and flatten exits pay slippage; a stop is filled at the open if the bar gaps through it.
  * Free IEX data is a partial view of the market; real fills will differ (backtest.py --feed sip uses the full market).
  * Regulatory fees are charged on every trade (cfg["backtest"]["fees"], see FEE_DEFAULTS): the SEC fee and FINRA TAF
    on the sale, the CAT fee on both sides, rounded UP to the cent as Alpaca does. Alpaca charges no commission.
Approximations: only each stock's first signal of the day counts, and the daily
loss limit looks at realized P&L only.
"""
import math
import random
from datetime import timedelta

import orb_bot as b


# Rates checked 2026-10-29. SEC Section 31: $20.60 per $1M of sales from 2026-04-04 (SEC Fee Rate Advisory 2026-2).
# FINRA TAF: $0.000166 per share sold, max $8.30 a trade (FINRA adjusts it; check finra.org). CAT: not published as a
# simple per-share rate here, so 0 by default; set cat_per_share in config.json if you know your broker's figure.
FEE_DEFAULTS = {"sec_per_million": 20.60, "taf_per_share": 0.000166, "taf_max": 8.30, "cat_per_share": 0.0}


def trade_fees(entry, exit_px, qty, bt):
    """Regulatory fees for one round trip (buy then sell `qty` shares), in dollars, rounded up to the cent."""
    f = dict(FEE_DEFAULTS, **(bt.get("fees") or {}))
    total = (exit_px * qty * f["sec_per_million"] / 1e6
             + min(qty * f["taf_per_share"], f["taf_max"])
             + 2 * qty * f["cat_per_share"])
    return math.ceil(round(total * 100, 6)) / 100.0 if total > 0 else 0.0


def _slip(price, bt):
    return max(price * bt["slippage_bps"] / 10000.0, bt["min_slippage_usd"])


def find_signal(df, cfg, trigger=None):
    """df: ONE day of 1-minute bars (New York index; open/high/low/close/volume), oldest first."""
    orng = b.opening_range(df, cfg)
    if orng is None:
        return None
    lo_t, hi_t = b.hhmm(cfg["no_entries_before"]), b.hhmm(cfg["no_entries_after"])
    idx = list(df.index)
    for i in range(len(idx) - 1):
        end = (idx[i] + timedelta(minutes=1)).time()
        if end < lo_t or end >= hi_t:
            continue
        sig = b.breakout_signal(float(df["close"].iloc[i]), orng, cfg, trigger)
        if sig is None:
            continue
        if idx[i + 1] - idx[i] > timedelta(minutes=3):
            continue  # next bar too far away to be a realistic fill
        return dict(sig, signal_bar=idx[i], fill_idx=i + 1, fill_time=idx[i + 1], orng=orng)
    return None


def simulate_exit(df, sig, cfg):
    bt = cfg["backtest"]
    stop, target = sig["stop"], sig["target"]
    flat_t = b.hhmm(cfg["flat_by"])
    fi = sig["fill_idx"]
    fill_open = float(df["open"].iloc[fi])
    entry = fill_open + _slip(fill_open, bt)
    for j in range(fi, len(df)):
        ts = df.index[j]
        o, h, l = float(df["open"].iloc[j]), float(df["high"].iloc[j]), float(df["low"].iloc[j])
        if ts.time() >= flat_t:
            return ts, o - _slip(o, bt), "flatten", entry
        if j > fi:
            if o <= stop:
                return ts, o - _slip(o, bt), "stop", entry      # gapped through the stop
            if o >= target:
                return ts, o, "target", entry                    # limit sell fills at the better open
        if l <= stop:
            return ts, stop - _slip(stop, bt), "stop", entry     # stop first if both touched
        if h >= target:
            return ts, target, "target", entry
    last = len(df) - 1
    c = float(df["close"].iloc[last])
    return df.index[last], c - _slip(c, bt), "data_end", entry


def _trade(sym, day, sig, qty, exit_info, bt=None):
    ts, px, reason, entry = exit_info
    risk_usd = qty * sig["risk"]
    fees = trade_fees(entry, px, qty, bt or {})
    pnl = (px - entry) * qty - fees
    return {"date": str(day), "symbol": sym, "qty": qty, "signal_bar": sig["signal_bar"].isoformat(),
            "fill_time": sig["fill_time"], "entry_fill": round(entry, 4), "stop": sig["stop"],
            "target": sig["target"], "exit_time": ts, "exit_fill": round(px, 4), "exit_reason": reason,
            "pnl": round(pnl, 2), "fees": fees, "risk_usd": round(risk_usd, 2),
            "r": round(pnl / risk_usd, 3) if risk_usd else 0.0}


def simulate_day(day_bars, cfg, triggers=None, portfolio=True):
    """day_bars: {symbol: one day of bars}.
    portfolio=True  -> obey max trades, max open positions, one-per-sector, cash and daily-loss limits (the backtest).
    portfolio=False -> every symbol's first signal, independently (the nightly control sample)."""
    triggers = triggers or {}
    found = []
    for sym, df in day_bars.items():
        if df is None or len(df) < 30:
            continue
        sig = find_signal(df, cfg, triggers.get(sym))
        if sig:
            found.append((sig["fill_time"], sym, sig, df))
    found.sort(key=lambda x: x[0])
    trades, sectors = [], cfg.get("sectors", {})
    for fill_time, sym, sig, df in found:
        day = fill_time.date()
        if not portfolio:
            qty, _ = b.size_position(sig["entry"], sig["risk"], cfg, 0)
            if qty >= 1:
                trades.append(_trade(sym, day, sig, qty, simulate_exit(df, sig, cfg), cfg.get("backtest")))
            continue
        if len(trades) >= cfg["max_trades_per_day"]:
            break
        open_now = [t for t in trades if t["exit_time"] > fill_time]
        realized = sum(t["pnl"] for t in trades if t["exit_time"] <= fill_time)
        if realized <= -cfg["max_daily_loss_usd"] or len(open_now) >= cfg["max_open_positions"]:
            continue
        if cfg.get("one_per_sector") and sectors.get(sym) and any(sectors.get(t["symbol"]) == sectors[sym] for t in open_now):
            continue
        open_value = sum(t["qty"] * t["entry_fill"] for t in open_now)
        qty, _ = b.size_position(sig["entry"], sig["risk"], cfg, open_value)
        if qty < 1:
            continue
        if not b.daily_risk_ok(realized, sum(t["risk_usd"] for t in open_now), qty * sig["risk"], cfg["max_daily_loss_usd"]):
            continue
        trades.append(_trade(sym, day, sig, qty, simulate_exit(df, sig, cfg), cfg.get("backtest")))
    return trades


# ---------- statistics ----------
def summarize(trades):
    n = len(trades)
    if n == 0:
        return {"n": 0, "trading_days": 0}
    rs = [t["r"] for t in trades]
    pnls = [t["pnl"] for t in trades]
    wins, losses = [x for x in pnls if x > 0], [x for x in pnls if x < 0]
    gp, gl = sum(wins), -sum(losses)
    daily = {}
    for t in trades:
        daily[t["date"]] = daily.get(t["date"], 0.0) + t["pnl"]
    cum = peak = dd = 0.0
    for d in sorted(daily):
        cum += daily[d]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    srt = sorted(rs)
    return {"n": n, "trading_days": len(daily), "win_rate": round(len(wins) / n, 3),
            "avg_r": round(sum(rs) / n, 3), "median_r": round(srt[n // 2], 3),
            "total_pnl": round(sum(pnls), 2), "total_fees": round(sum(t.get("fees", 0.0) for t in trades), 2),
            "profit_factor": round(gp / gl, 2) if gl > 0 else (999.0 if gp > 0 else 0.0),
            "avg_win_r": round(sum(r for r in rs if r > 0) / max(1, sum(1 for r in rs if r > 0)), 3),
            "avg_loss_r": round(sum(r for r in rs if r < 0) / max(1, sum(1 for r in rs if r < 0)), 3),
            "max_drawdown_usd": round(dd, 2)}


def bootstrap(rs, n_boot=5000, seed=1):
    """(low95, high95, probability the true average R is above zero) by resampling the trades."""
    if len(rs) < 5:
        return None
    rnd = random.Random(seed)
    k = len(rs)
    means = sorted(sum(rnd.choice(rs) for _ in range(k)) / k for _ in range(n_boot))
    return (round(means[int(0.025 * n_boot)], 3), round(means[int(0.975 * n_boot)], 3),
            round(sum(1 for m in means if m > 0) / n_boot, 3))
