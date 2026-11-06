from pathlib import Path
import tempfile
"""Offline tests: simulation engine, backtest loader, journal, nightly control, success criteria. Run: python test_engine.py"""
import json
import shutil
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
import pandas as pd
import backtest
import engine
import evaluate
import journal
import orb_bot as b

CFG = json.load(open(b.HERE / "config.json"))
CFG["strategy"] = "orb"            # these tests are about the opening-range rules; gap mode has its own tests
CFG["strategy"] = "orb"            # these tests are about the opening-range rules; gap mode has its own tests
NY = b.NY
DAY = "2026-10-27"
T = b.HERE


def bars(after, day=DAY, start="09:45"):
    """15 opening minutes (high 101, low 100), then the given prices as 1-minute bars (open=prev close)."""
    idx = pd.date_range(f"{day} 09:30", periods=15, freq="1min", tz=NY)
    rows = [dict(open=100.5, high=101.0 if i == 7 else 100.8, low=100.0 if i == 3 else 100.2, close=100.5, volume=1000) for i in range(15)]
    prev = 100.5
    for c in after:
        rows.append(dict(open=prev, high=max(prev, c) + 0.02, low=min(prev, c) - 0.02, close=c, volume=1000))
        prev = c
    idx = pd.date_range(f"{day} 09:30", periods=len(rows), freq="1min", tz=NY)
    return pd.DataFrame(rows, index=idx)


def add_bars(df, rows):
    """Append (open, high, low, close) minute bars after the last bar of df."""
    last = df.index[-1]
    extra = pd.DataFrame([dict(open=o, high=h, low=l, close=c, volume=1000) for o, h, l, c in rows],
                         index=[last + pd.Timedelta(minutes=i + 1) for i in range(len(rows))])
    return pd.concat([df, extra])


def test_engine_outcomes():
    up = [100.9, 101.2] + [101.3 + 0.1 * i for i in range(20)]            # breaks out at 9:46, then climbs to the target
    t = engine.simulate_day({"A": bars(up)}, CFG)
    assert len(t) == 1 and t[0]["exit_reason"] == "target" and t[0]["qty"] == 98
    assert t[0]["exit_fill"] == 102.6 and 1.5 < t[0]["r"] < 2.0 and t[0]["pnl"] > 100   # target is 2R before slippage
    down = [100.9, 101.2] + [101.0 - 0.1 * i for i in range(20)]          # breaks out, then falls through the stop
    t = engine.simulate_day({"A": bars(down)}, CFG)
    assert t[0]["exit_reason"] == "stop" and t[0]["r"] < -1.0              # slippage makes the loss worse than 1R
    flat = [100.9, 101.2] + [101.3] * 380                                  # goes nowhere until the flat-by time
    t = engine.simulate_day({"A": bars(flat)}, CFG)
    assert t[0]["exit_reason"] == "flatten" and t[0]["exit_time"].strftime("%H:%M") == "15:55"
    assert engine.simulate_day({"A": bars([100.5] * 40)}, CFG) == []       # never breaks out
    assert engine.simulate_day({"A": bars([100.9, 101.2] + [101.3] * 20)}, dict(CFG, no_entries_after="09:46")) == []
    gap = add_bars(bars([100.9, 101.2, 101.3]), [(99.0, 99.2, 98.8, 99.0)] + [(99.0, 99.1, 98.9, 99.0)] * 25)
    t = engine.simulate_day({"A": gap}, CFG)                               # next bar opens far below the 100.50 stop
    assert t[0]["exit_reason"] == "stop" and t[0]["exit_fill"] < 99.0
    both = add_bars(bars([100.9, 101.2]), [(101.2, 103.0, 100.0, 101.3)] + [(101.3, 101.4, 101.2, 101.3)] * 25)
    t = engine.simulate_day({"A": both}, CFG)
    assert t[0]["exit_reason"] == "stop"                                    # touching both in one bar counts as a stop


def test_portfolio_rules():
    up = [100.9, 101.2] + [101.3 + 0.1 * i for i in range(20)]
    day = {s: bars(up) for s in "ABCDE"}
    cfg = dict(CFG, max_trades_per_day=3, max_open_positions=2, sectors={s: s for s in "ABCDE"}, one_per_sector=True)
    assert len(engine.simulate_day(day, cfg)) == 2                          # all five signal at once: only 2 positions may be open
    stag = {"A": bars(up), "B": bars([100.5] * 20 + up), "C": bars([100.5] * 40 + up), "D": bars([100.5] * 60 + up)}
    got = engine.simulate_day(stag, cfg)                                    # signals spread out: the limit of 3 trades a day binds
    assert [x["symbol"] for x in got] == ["A", "B", "C"] and all(x["exit_reason"] == "target" for x in got)
    long_hold = {s: bars([100.9, 101.2] + [101.3] * 380) for s in "ABC"}
    assert len(engine.simulate_day(long_hold, cfg)) == 2                    # only 2 can be open at once
    same = dict(cfg, sectors={"A": "x", "B": "x", "C": "y"})
    assert {t["symbol"] for t in engine.simulate_day(long_hold, same)} == {"A", "C"}   # one per sector
    assert len(engine.simulate_day(long_hold, cfg, portfolio=False)) == 3   # the control ignores those caps


def test_stats():
    trades = [{"date": f"2026-10-{d:02d}", "pnl": p, "r": r} for d, p, r in
              [(1, 100, 1.0), (2, -50, -0.5), (3, 200, 2.0), (4, -50, -0.5), (5, 100, 1.0)]]
    s = engine.summarize(trades)
    assert s["n"] == 5 and s["win_rate"] == 0.6 and s["avg_r"] == 0.6 and s["total_pnl"] == 300
    assert s["profit_factor"] == 4.0 and s["max_drawdown_usd"] == 50
    assert engine.summarize([])["n"] == 0
    assert engine.bootstrap([1.0] * 10)[2] == 1.0 and engine.bootstrap([-1.0] * 10)[2] == 0.0
    assert engine.bootstrap([1, 2]) is None
    lo, hi, p = engine.bootstrap([r["r"] for r in trades]); assert lo < 0.6 < hi and 0 <= p <= 1


class FakeData:
    def __init__(self):
        self.calls = 0

    def get_stock_bars(self, req):
        self.calls += 1
        d = bars([100.9, 101.2] + [101.3 + 0.1 * i for i in range(20)], day="2026-09-15")
        return NS(df=pd.concat({req.symbol_or_symbols: d}))


def test_backtest_loader_and_report():
    shutil.rmtree(T / "t_cache", ignore_errors=True)
    data = FakeData()
    a = backtest.load_month(data, "A", 2026, 9, cache=T / "t_cache")
    b_ = backtest.load_month(data, "A", 2026, 9, cache=T / "t_cache")       # second read comes from the cache
    assert data.calls == 1 and len(a) == len(b_) and str(b_.index[0].tz) == "America/New_York"
    assert b_.index[0].strftime("%H:%M") == "09:30"
    assert list(backtest.months(datetime(2025, 11, 5), datetime(2026, 2, 1))) == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]
    trades = [{"date": "2026-09-01", "symbol": "A", "pnl": 50.0, "r": 0.7}] * 8 + [{"date": "2026-10-05", "symbol": "A", "pnl": -40.0, "r": -0.6}] * 6
    lines = []
    res = backtest.report(trades, ["2026-09-01"], datetime(2026, 10, 1).date(), out=lines.append)
    assert res["in_sample"]["n"] == 8 and res["holdout"]["n"] == 6 and "HOLDOUT" in "\n".join(lines)
    shutil.rmtree(T / "t_cache", ignore_errors=True)


def test_blackout_days_are_skipped():
    shutil.rmtree(T / "t_cache", ignore_errors=True)
    start, end = datetime(2026, 9, 1, tzinfo=NY), datetime(2026, 9, 30, tzinfo=NY)
    for blackout, expect_trades in (([], True), (["2026-09-15"], False)):
        cfg = dict(CFG, blackout={"dates": blackout, "symbols": {}})
        trades, days = backtest.run(FakeData(), cfg, ["A", "B", "C"], start, end, progress=lambda *_: None, cache=T / "t_cache")
        assert bool(trades) == expect_trades and len(days) == 1
        shutil.rmtree(T / "t_cache", ignore_errors=True)
    bot = NS(cfg=dict(CFG, blackout={"dates": [DAY], "symbols": {}}), fetch_bars=lambda *a: 1 / 0)
    assert journal.shadow(bot, datetime(2026, 10, 27, 15, 58, tzinfo=NY), T / "t_s.csv") == []   # never even fetches data


def order(id_, sym, side, qty, px, legs=None, otype="market"):
    return NS(id=id_, symbol=sym, side=NS(value=side), status=NS(value="filled"), filled_qty=str(qty),
              filled_avg_price=str(px), legs=legs or [], order_type=NS(value=otype))


def test_journal_and_control():
    j = T / "t_journal.csv"; j.unlink(missing_ok=True)
    state = {"date": DAY, "trades": [
        {"symbol": "TSLA", "order_id": "p1", "entry": 394.27, "stop": 388.78, "target": 405.25, "max_loss": 137.25, "time": "t", "trigger": None, "note": "n", "source": "claude-premarket"},
        {"symbol": "XOM", "order_id": "p2", "entry": 183.74, "stop": 182.47, "target": 186.28, "max_loss": 68.58, "time": "t"},
        {"symbol": "AMD", "order_id": "p3", "entry": 500.0, "stop": 490.0, "target": 520.0, "max_loss": 100.0, "time": "t"},
        {"symbol": "OLD", "order_id": "dry-run", "entry": 1, "stop": 1, "target": 1, "max_loss": 1, "time": "t"}]}
    orders = [order("p1", "TSLA", "buy", 25, 394.30, legs=[order("l1", "TSLA", "sell", 25, 405.25, otype="limit"), order("l2", "TSLA", "sell", 0, 0, otype="stop")]),
              order("p2", "XOM", "buy", 54, 183.80, legs=[order("l3", "XOM", "sell", 0, 0, otype="limit"), order("l4", "XOM", "sell", 0, 0, otype="stop")]),
              order("f1", "XOM", "sell", 54, 183.10),
              order("p3", "AMD", "buy", 20, 500.5, legs=[order("l5", "AMD", "sell", 20, 490.0, otype="stop")])]
    tr = NS(get_orders=lambda req: orders)
    now = datetime(2026, 10, 27, 15, 58, tzinfo=NY)
    rows = journal.reconcile(tr, state, now, j)
    by = {r["symbol"]: r for r in rows}
    assert len(rows) == 3                                                    # the dry-run row is skipped
    assert by["TSLA"]["exit_reason"] == "target" and by["TSLA"]["pnl"] == round((405.25 - 394.30) * 25, 2)
    assert by["TSLA"]["entry_slippage"] == 0.03 and by["TSLA"]["r"] == round(273.75 / 137.25, 3)
    assert by["XOM"]["exit_reason"] == "flatten" and by["XOM"]["pnl"] == round((183.10 - 183.80) * 54, 2)
    assert by["AMD"]["exit_reason"] == "stop" and by["AMD"]["r"] == -2.1      # lost $210 against $100 of planned risk
    assert by["AMD"]["pnl"] == round((490.0 - 500.5) * 20, 2)
    journal.reconcile(tr, state, now, j)                                     # running it again must not duplicate
    assert len(journal.read_csv(j)) == 3
    # nightly control with a fake bot
    s = T / "t_shadow.csv"; s.unlink(missing_ok=True)
    up = [100.9, 101.2] + [101.3 + 0.1 * i for i in range(20)]
    bot = NS(cfg=CFG, fetch_bars=lambda syms, now_: {"NVDA": bars(up), "AMD": bars(up), "TSLA": bars([100.5] * 30)})
    (T / "t_inputs.json").write_text(json.dumps({"date": DAY, "trade_today": True, "symbols": [{"symbol": "NVDA"}]}))
    keep = Path(tempfile.mkdtemp()) / "daily_inputs"                       # never the real archive next to the bot
    rows = journal.shadow(bot, now, s, T / "t_inputs.json", keep_dir=keep)
    assert {r["symbol"]: r["in_shortlist"] for r in rows} == {"NVDA": "yes", "AMD": "no"}   # TSLA never broke out
    assert (keep / f"inputs_{DAY}.json").exists()
    shutil.rmtree(keep.parent, ignore_errors=True); (T / "t_inputs.json").unlink(); j.unlink(); s.unlink()


def test_criteria():
    c = json.load(open(T / "success_criteria.json"))
    rows, verdict = evaluate.evaluate([], [], None, c)
    assert verdict == "KEEP RUNNING" and all(s == "WAIT" for _, s, _ in rows)
    good = [{"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "status": "closed", "pnl": "100" if i % 3 else "-50",
             "r": "1.0" if i % 3 else "-0.5"} for i in range(70)]
    sh = [{"in_shortlist": "yes", "pnl": "1", "r": "0.4"}] * 35 + [{"in_shortlist": "no", "pnl": "1", "r": "0.1"}] * 35
    bt = {"holdout": {"n": 120, "avg_r": 0.2}}
    rows, verdict = evaluate.evaluate(good, sh, bt, c)
    assert verdict == "MET", rows
    bad_sh = [{"in_shortlist": "yes", "pnl": "1", "r": "0.0"}] * 35 + [{"in_shortlist": "no", "pnl": "1", "r": "0.3"}] * 35
    rows, verdict = evaluate.evaluate(good, bad_sh, bt, c)
    assert verdict == "FAILED" and any(n.startswith("Claude") and s == "FAIL" for n, s, _ in rows)
    losing = [dict(r, pnl="-50", r="-0.5") for r in good]
    assert evaluate.evaluate(losing, sh, bt, c)[1] == "FAILED"
    few = good[:10]
    assert evaluate.evaluate(few, sh, bt, c)[1] == "KEEP RUNNING"           # too little data is never a verdict
    crit, lock = T / "t_crit.json", T / "t_lock"
    crit.write_text("{}"); lock.unlink(missing_ok=True)
    assert evaluate.check_lock(crit, lock) is None and evaluate.check_lock(crit, lock) is None
    crit.write_text('{"changed": true}'); assert "CHANGED" in evaluate.check_lock(crit, lock)
    crit.unlink(); lock.unlink()


def test_regulatory_fees():
    bt = {}                                                                  # the defaults: SEC $20.60/M, TAF $0.000166/share
    assert engine.trade_fees(500.0, 510.0, 20, bt) == 0.22                   # 0.2101 SEC + 0.0033 TAF = 0.2134, rounded UP
    assert engine.trade_fees(10.0, 10.0, 1, bt) == 0.01                      # anything above zero costs at least a cent
    big = engine.trade_fees(10.0, 10.0, 1_000_000, bt)
    assert big == round(10.0 * 1_000_000 * 20.60 / 1e6 + 8.30, 2)            # TAF is capped at $8.30 a trade
    assert engine.trade_fees(500, 510, 20, {"fees": {"sec_per_million": 0, "taf_per_share": 0}}) == 0.0
    assert engine.trade_fees(100, 100, 10, {"fees": {"sec_per_million": 0, "taf_per_share": 0,
                                                     "cat_per_share": 0.001}}) == 0.02   # CAT on both sides
    up = [100.9, 101.2] + [101.3 + 0.1 * i for i in range(20)]
    free = dict(CFG, backtest=dict(CFG["backtest"], fees={"sec_per_million": 0, "taf_per_share": 0}))
    t_free, t_fee = engine.simulate_day({"A": bars(up)}, free)[0], engine.simulate_day({"A": bars(up)}, CFG)[0]
    assert t_free["fees"] == 0.0 and t_fee["fees"] > 0
    assert round(t_free["pnl"] - t_fee["pnl"], 2) == t_fee["fees"] and t_fee["r"] < t_free["r"]   # fees cut P&L and R
    assert engine.summarize([t_fee])["total_fees"] == t_fee["fees"]


class RecData(FakeData):
    def __init__(self):
        super().__init__()
        self.reqs = []

    def get_stock_bars(self, req):
        self.reqs.append(req)
        return super().get_stock_bars(req)


def test_sip_feed_single_symbol_and_cache():
    shutil.rmtree(T / "t_cache", ignore_errors=True)
    data = RecData()
    backtest.load_month(data, "QQQ", 2026, 9, cache=T / "t_cache", feed="sip")
    backtest.load_month(data, "QQQ", 2026, 9, cache=T / "t_cache", feed="iex")   # other feed: its own file, fetched again
    assert [r.feed.value for r in data.reqs] == ["sip", "iex"]
    assert (T / "t_cache" / "QQQ_sip_2026-09.csv").exists() and (T / "t_cache" / "QQQ_2026-09.csv").exists()
    start, end = datetime(2026, 9, 1, tzinfo=NY), datetime(2026, 9, 30, tzinfo=NY)
    one, _ = backtest.run(RecData(), CFG, ["QQQ"], start, end, progress=lambda *_: None, cache=T / "t_cache", feed="sip")
    assert one == []                                                        # the universe guard skips one-stock days...
    one, days = backtest.run(RecData(), CFG, ["QQQ"], start, end, progress=lambda *_: None, cache=T / "t_cache",
                             feed="sip", min_symbols=1)
    assert len(one) == 1 and one[0]["symbol"] == "QQQ" and one[0]["fees"] > 0   # ...a single-stock study passes 1
    now = datetime.now(NY)                                                  # current month: SIP stops 16 min short of now
    data = RecData()
    backtest.load_month(data, "QQQ", now.year, now.month, cache=T / "t_cache", feed="sip")
    if data.reqs:
        end = data.reqs[0].end                                              # alpaca-py stores it as naive UTC
        end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
        assert end <= now - timedelta(minutes=15)
    lines = []
    backtest.report(one, days, None, out=lines.append)
    assert any("regulatory fees $" in x for x in lines)
    shutil.rmtree(T / "t_cache", ignore_errors=True)


if __name__ == "__main__":
    test_engine_outcomes(); test_portfolio_rules(); test_stats(); test_backtest_loader_and_report()
    test_blackout_days_are_skipped(); test_journal_and_control(); test_criteria()
    print("engine, journal and criteria tests passed")
