"""
fetch_data.py - download 1-minute bars (full-market SIP feed, free for history) for the strategy research.

    python research\\fetch_data.py                 # every stock in config.json's universe, plus IWM and DIA
    python research\\fetch_data.py AAPL MSFT       # only these
    python research\\fetch_data.py --workers 3     # stocks downloaded side by side (default 3)

Several stocks download at once, sharing ONE speed limit that stays under the free plan's 200 requests a minute.

Writes research\\data\\SYMBOL.csv.gz: regular hours only (9:30-16:00 New York), 2016-01 to the end of last month.
One compressed file per stock (about 10-20 MB each). A stock whose file exists is skipped, so if the download stops
you can simply run it again and it carries on. Needs ALPACA_API_KEY / ALPACA_SECRET_KEY (your paper keys).
"""
import gzip
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "data"
NY = "America/New_York"
EXTRA = ["IWM", "DIA"]
START = (2016, 1)


def months(until):
    y, m = START
    while (y, m) <= until:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


class RateLimit:
    """One shared limit for every download thread: at most `per_minute` requests a minute in total."""

    def __init__(self, per_minute=170, clock=time.monotonic, sleep=time.sleep):
        self.gap, self.next, self.lock, self.clock, self.sleep = 60.0 / per_minute, 0.0, threading.Lock(), clock, sleep
        self.count = 0

    def wait(self):
        with self.lock:
            now = self.clock()
            t = max(now, self.next)
            self.next = t + self.gap
            self.count += 1
        if t > now:
            self.sleep(t - now)


LIMIT = RateLimit()
_print_lock = threading.Lock()


def say(msg):
    with _print_lock:
        print(msg, flush=True)


def fetch_symbol(client, sym, until, pause=None):
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    parts = []
    for y, m in months(until):
        start = pd.Timestamp(datetime(y, m, 1), tz=NY)
        end = pd.Timestamp(datetime(y + (m == 12), m % 12 + 1, 1), tz=NY)
        for attempt in range(5):
            LIMIT.wait()                       # a month is usually 2 pages; alpaca-py fetches the 2nd one itself
            try:
                df = client.get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Minute,
                                                             start=start, end=end, feed=DataFeed.SIP)).df
                LIMIT.wait()                   # count the probable second page too
                break
            except Exception as e:
                if attempt == 4:
                    raise
                slow = "429" in str(e) or "rate" in str(e).lower()
                wait = (20 if slow else 5) * (attempt + 1)
                say(f"    {sym} {y}-{m:02d}: {type(e).__name__}{' (rate limit)' if slow else ''}, retrying in {wait} s")
                time.sleep(wait)
        if df is None or df.empty:
            continue
        if df.index.nlevels > 1:
            df = df.xs(sym, level=0)
        df = df[["open", "high", "low", "close", "volume"]]
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(NY)
        t = df.index.hour * 60 + df.index.minute
        parts.append(df[(t >= 570) & (t < 960)])
        if m == 12:
            say(f"    {sym}: through {y}")
    return pd.concat(parts) if parts else None


def download(client_factory, sym, until):
    path = OUT / f"{sym}.csv.gz"
    t0 = time.time()
    df = fetch_symbol(client_factory(), sym, until)
    if df is None or df.empty:
        return f"{sym}: no data"
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
        df.to_csv(f, index_label="timestamp")
    tmp.replace(path)                         # only a complete file ever gets the real name
    return (f"{sym}: done, {len(df):,} bars from {df.index[0].date()} in {time.time() - t0:.0f} s "
            f"({path.stat().st_size / 1e6:.1f} MB)")


def main():
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY first.")
    from alpaca.data.historical import StockHistoricalDataClient
    args = sys.argv[1:]
    workers = 3
    if "--workers" in args:
        k = args.index("--workers")
        workers = max(1, min(6, int(args[k + 1])))
        del args[k:k + 2]
    cfg = json.loads((ROOT / "config.json").read_text())
    syms = args or list(dict.fromkeys(cfg["universe"] + EXTRA))
    now = datetime.now()
    until = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    OUT.mkdir(exist_ok=True)
    todo = [s for s in syms if not (OUT / f"{s}.csv.gz").exists()]
    say(f"Downloading {len(todo)} stocks ({len(syms) - len(todo)} already done), 2016-01 to {until[0]}-{until[1]:02d}, "
        f"{workers} at a time, into {OUT}")
    t0, done = time.time(), 0
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(download, lambda: StockHistoricalDataClient(key, secret), s, until): s for s in todo}
        for f in as_completed(futs):
            done += 1
            try:
                say(f"[{done}/{len(todo)}] " + f.result())
            except Exception as e:
                say(f"[{done}/{len(todo)}] {futs[f]}: FAILED ({type(e).__name__}: {str(e)[:120]}). Run again to retry it.")
    say(f"\nDone in {(time.time() - t0) / 60:.0f} min. Tell Claude the download finished.")


if __name__ == "__main__":
    main()
