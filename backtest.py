"""
backtest.py - run the bot's opening-range rules over past days using Alpaca's free 1-minute data.

    python backtest.py --start 2025-01-01 --end 2026-10-23 --holdout-from 2026-04-01
    python backtest.py --symbols QQQ --start 2016-01-01 --end 2026-10-28 --holdout-from 2024-11-01 --feed sip --tag qqq
    (needs ALPACA_API_KEY / ALPACA_SECRET_KEY; first run downloads and caches data in bt_cache/)

 * --feed iex (default) is the free real-time exchange: a partial view of the market. --feed sip is the full
   consolidated market, free for history on Alpaca's basic plan (everything but the latest 15 minutes, since 2016).
 * --tag NAME writes backtest_trades_NAME.csv, backtest_summary_NAME.json and backtest_report_NAME.txt, so a side
   study never overwrites backtest_summary.json, which evaluate.py uses for the success criteria.
 * Costs counted: slippage on every fill (config backtest.slippage_bps / min_slippage_usd) and regulatory fees
   (SEC fee and FINRA TAF on sales, CAT fee if set; see engine.FEE_DEFAULTS). Alpaca charges no commission.

READ THIS BEFORE TRUSTING A NUMBER
 * This tests the RULES on all your universe stocks. Claude's picks cannot be backtested
   honestly (there is no record of what Claude would have known on past mornings), so the
   live "shadow.csv" comparison is the only fair test of Claude's value.
 * Free IEX data is a partial view of the market. Real fills will differ.
 * Tuning cheats. Settle your rules using only days BEFORE --holdout-from. Look at the holdout
   result once. If you change the rules afterwards, the holdout is used up: pick a newer one.
   There is deliberately no parameter-sweep option here.
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import engine

HERE = Path(__file__).resolve().parent
CACHE = HERE / "bt_cache"
NY = engine.b.NY
COLS = ["open", "high", "low", "close", "volume"]


def _clean(df):
    if df is None or df.empty:
        return pd.DataFrame(columns=COLS)
    df = df[COLS].copy()
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(NY)
    df = df[(df.index.time >= datetime(2000, 1, 1, 9, 30).time()) & (df.index.time < datetime(2000, 1, 1, 16, 0).time())]
    return df.sort_index()


def cache_path(cache, sym, year, month, feed="iex"):
    """IEX files keep their original names; SIP files are kept apart so the two feeds never mix."""
    return cache / (f"{sym}_{year}-{month:02d}.csv" if feed == "iex" else f"{sym}_{feed}_{year}-{month:02d}.csv")


def load_month(data, sym, year, month, refresh=False, cache=CACHE, feed="iex"):
    """One month of 1-minute bars for one stock, from cache or Alpaca."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    cache.mkdir(exist_ok=True)
    path = cache_path(cache, sym, year, month, feed)
    today = datetime.now(NY)
    current = (year, month) == (today.year, today.month)
    if path.exists() and not refresh and not current:
        return _clean(pd.read_csv(path, index_col=0))
    start = datetime(year, month, 1, tzinfo=NY)
    # the free plan withholds the latest 15 minutes of SIP data, so stop short of "now"
    now_limit = today - timedelta(minutes=16) if feed == "sip" else today
    end = min(datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=NY), now_limit)
    if start >= end:
        return pd.DataFrame(columns=COLS)
    try:
        df = data.get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Minute, start=start,
                                                  end=end, feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX)).df
    except Exception as e:
        if feed == "sip" and ("subscription" in str(e).lower() or "403" in str(e)):
            raise SystemExit("Alpaca refused SIP data for %s %d-%02d (%s). Run again with --feed iex." % (sym, year, month, e))
        raise
    if df is not None and not df.empty and df.index.nlevels > 1:
        df = df.xs(sym, level=0)
    df = _clean(df)
    if not current:
        df.to_csv(path)
    return df


def months(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def run(data, cfg, symbols, start, end, refresh=False, progress=print, cache=CACHE, feed="iex", min_symbols=3):
    """min_symbols: days with fewer stocks that have data are skipped (a guard against data holes in a universe
    run). A single-stock study such as QQQ passes 1."""
    frames = {}
    for sym in symbols:
        parts = []
        for i, (y, m) in enumerate(months(start, end)):
            parts.append(load_month(data, sym, y, m, refresh, cache, feed))
            if i % 12 == 11:
                progress(f"  {sym}: loaded through {y}-{m:02d}")
        parts = [p for p in parts if len(p)]
        frames[sym] = pd.concat(parts) if parts else pd.DataFrame(columns=COLS)
        progress(f"  loaded {sym}: {len(frames[sym])} bars")
    days = sorted({d for f in frames.values() if len(f) for d in f.index.date if start.date() <= d <= end.date()})
    trades = []
    skip = set(cfg.get("blackout", {}).get("dates", []))
    for day in days:
        if day.isoformat() in skip:
            continue  # the live bot does not trade these days either
        day_bars = {}
        for sym, f in frames.items():
            if len(f):
                sub = f[f.index.date == day]
                if len(sub):
                    day_bars[sym] = sub
        if len(day_bars) >= min_symbols:
            trades.extend(engine.simulate_day(day_bars, cfg))
    return trades, days


def report(trades, days, holdout_from, out=print):
    cut = holdout_from.isoformat() if holdout_from else None
    ins = [t for t in trades if cut is None or t["date"] < cut]
    hold = [t for t in trades if cut and t["date"] >= cut]
    res = {"days_tested": len(days), "holdout_from": cut, "in_sample": engine.summarize(ins),
           "holdout": engine.summarize(hold) if cut else None}
    for name, grp in (("IN-SAMPLE (tune only on this)", ins), ("HOLDOUT (look once)", hold)):
        if not grp:
            continue
        s = engine.summarize(grp)
        out(f"\n{name}: {s['n']} trades over {s['trading_days']} days")
        out(f"  win rate {s['win_rate']:.0%} | avg R {s['avg_r']:+.3f} | median R {s['median_r']:+.3f} | "
            f"profit factor {s['profit_factor']} | total ${s['total_pnl']:,.0f} | max drawdown ${s['max_drawdown_usd']:,.0f}")
        out(f"  costs included: regulatory fees ${s.get('total_fees', 0):,.2f} in total, plus slippage on every fill")
        bs = engine.bootstrap([t["r"] for t in grp])
        if bs:
            out(f"  95% range for the true avg R: {bs[0]:+.3f} to {bs[1]:+.3f} | chance it is above zero: {bs[2]:.0%}")
            if name.startswith("HOLD"):
                res["holdout_bootstrap"] = {"low": bs[0], "high": bs[1], "p_positive": bs[2]}
    by = {}
    for t in trades:
        by.setdefault(t["symbol"], []).append(t["r"])
    out("\nBy stock (avg R, trades): " + ", ".join(f"{k} {sum(v)/len(v):+.2f} ({len(v)})" for k, v in sorted(by.items())))
    out("\nCaveat: historical data, the rules alone (no Claude filter), pessimistic fills. A positive result is a "
        "reason to keep paper trading, not a promise.")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--holdout-from", help="date from which results are held back, e.g. 2026-04-01")
    ap.add_argument("--symbols", help="comma list; default = universe in config.json")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--feed", choices=("iex", "sip"), default="iex", help="sip = the full market (free for history)")
    ap.add_argument("--tag", help="write backtest_*_TAG files instead of the main ones evaluate.py reads")
    ap.add_argument("--min-symbols", type=int, help="skip days with fewer stocks with data (default 3, or 1 with --symbols)")
    a = ap.parse_args()
    cfg = json.loads((HERE / "config.json").read_text())
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY.")
    import orb_bot
    from alpaca.data.historical import StockHistoricalDataClient
    data = orb_bot.add_timeout(StockHistoricalDataClient(key, secret), 60)
    syms = a.symbols.split(",") if a.symbols else cfg["universe"]
    start = datetime.fromisoformat(a.start).replace(tzinfo=NY)
    end = datetime.fromisoformat(a.end).replace(tzinfo=NY)
    hold = datetime.fromisoformat(a.holdout_from).date() if a.holdout_from else None
    min_syms = a.min_symbols or (1 if a.symbols else 3)
    print(f"Backtest {','.join(syms)} {a.start} to {a.end}, feed {a.feed.upper()}, holdout from {a.holdout_from or '-'}")
    trades, days = run(data, cfg, syms, start, end, a.refresh, feed=a.feed, min_symbols=min_syms)
    lines = []

    def out(x):
        print(x)
        lines.append(x)
    out(f"Backtest {','.join(syms)} {a.start} to {a.end} | feed {a.feed.upper()} | {len(days)} trading days with data")
    res = report(trades, days, hold, out=out)
    sfx = f"_{a.tag}" if a.tag else ""
    res.update(symbols=syms, feed=a.feed, start=a.start, end=a.end,
               fees=dict(engine.FEE_DEFAULTS, **(cfg.get("backtest", {}).get("fees") or {})))
    (HERE / f"backtest_report{sfx}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with open(HERE / f"backtest_trades{sfx}.csv", "w", newline="", encoding="utf-8") as f:
        if trades:
            w = csv.DictWriter(f, fieldnames=list(trades[0]))
            w.writeheader()
            w.writerows(trades)
    res["created"] = datetime.now(NY).isoformat()
    (HERE / f"backtest_summary{sfx}.json").write_text(json.dumps(res, indent=2, default=str))
    print(f"\nSaved backtest_trades{sfx}.csv, backtest_summary{sfx}.json and backtest_report{sfx}.txt")


if __name__ == "__main__":
    main()
