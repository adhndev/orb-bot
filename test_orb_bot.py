"""Offline tests: no network, no Alpaca. Run: python test_orb_bot.py"""
import json
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import MagicMock
import pandas as pd
import orb_bot as b

CFG = json.load(open(b.HERE / "config.json"))
CFG["strategy"] = "orb"            # these tests are about the opening-range rules; gap mode has its own tests
CFG["strategy"] = "orb"            # these tests are about the opening-range rules; gap mode has its own tests
import tempfile as _tf
from pathlib import Path as _P
TEST_HISTORY = _P(_tf.mkdtemp()) / "state_history"
NY = b.NY


def make_bars(day="2026-10-27", closes_after=None):
    """15 opening minutes between 100.00 and 101.00, then given closes."""
    idx = pd.date_range(f"{day} 09:30", periods=15, freq="1min", tz=NY)
    rows = [dict(open=100.4, high=101.0 if i == 7 else 100.8, low=100.0 if i == 3 else 100.2,
                 close=100.5, volume=1000) for i in range(15)]
    if closes_after:
        idx2 = pd.date_range(f"{day} 09:45", periods=len(closes_after), freq="1min", tz=NY)
        rows += [dict(open=c, high=c + .05, low=c - .05, close=c, volume=1000) for c in closes_after]
        idx = idx.append(idx2)
    return pd.DataFrame(rows, index=idx)


def test_range_signal_size():
    bars = make_bars()
    r = b.opening_range(bars, CFG)
    assert r == {"high": 101.0, "low": 100.0, "mid": 100.5}
    assert b.breakout_signal(100.9, r, CFG) is None            # not above the high
    assert b.breakout_signal(101.6, r, CFG) is None            # ran too far (> 0.5 x range)
    s = b.breakout_signal(101.2, r, CFG)
    assert s["stop"] == 100.5 and abs(s["risk"] - 0.7) < 1e-9 and s["target"] == 102.6
    qty, why = b.size_position(s["entry"], s["risk"], CFG, 0)
    assert qty == 98 and why == "position cap"                 # 10000 cap / 101.2 = 98 < 250/0.7=357
    assert b.size_position(300.0, 1.0, CFG, 0)[0] == 33        # 10000/300 = 33 (max order)
    assert b.size_position(100.0, 10.0, CFG, 0) == (25, "max loss")
    assert b.size_position(100.0, 1.0, CFG, 20000)[0] == 25    # cash left: 22500-20000
    assert b.daily_risk_ok(-300, 100, 90, 500) and not b.daily_risk_ok(-300, 100, 150, 500)
    wide = make_bars(); wide.loc[wide.index[2], "high"] = 110.0
    assert b.breakout_signal(111, b.opening_range(wide, CFG), CFG) is None  # range too wide
    assert b.opening_range(bars.iloc[:5], CFG) is None          # too few bars


def fake_bot(bars, pnl=0.0, submit=True):
    trading = MagicMock()
    trading.get_account.return_value = NS(equity=str(100000 + pnl), last_equity="100000")
    trading.get_all_positions.return_value = []
    trading.submit_order.return_value = NS(id="abc123")
    data = MagicMock()
    data.get_stock_bars.return_value = NS(df=pd.concat({"TSLA": bars}))
    b.STATE_FILE = b.HERE / "test_state.json"          # set BEFORE the bot exists: it reads its state on creation
    b.STATE_FILE.unlink(missing_ok=True)               # and start clean, never from another test's leftovers
    b.STATE_HISTORY = TEST_HISTORY
    bot = b.Bot(dict(CFG, flatten_confirm_seconds=0), trading, data, submit)
    bot._sleep = lambda s: None
    bot.state = {"date": "2026-10-27", "tried": [], "trades": [], "halted": False, "flattened": False}
    b.STATE_FILE = b.HERE / "test_state.json"
    b.STOP_FILE = b.HERE / "test_STOP_unused"          # never the real STOP file: tests must not depend on it
    b.PAUSE_FILE = b.HERE / "test_PAUSE"
    b.PAUSE_FILE.unlink(missing_ok=True)
    b.STATE_HISTORY = TEST_HISTORY                      # never the real archive: a fake day must not reach the journal
    write_inputs()
    return bot, trading


def write_inputs(**over):
    d = {"date": "2026-10-27", "trade_today": True, "symbols": [{"symbol": "TSLA"}]}
    d.update(over)
    b.INPUTS_FILE = b.HERE / "test_inputs.json"
    b.INPUTS_FILE.write_text(json.dumps(d))


def test_tick_flow():
    bars = make_bars(closes_after=[100.9, 101.2, 101.25])
    now = datetime(2026, 10, 27, 9, 49, 5, tzinfo=NY)
    bot, tr = fake_bot(bars)
    bot.tick(now)
    assert tr.submit_order.call_count == 1
    req = tr.submit_order.call_args[0][0]
    assert req.symbol == "TSLA" and req.qty == 98 and req.stop_loss.stop_price == 100.5
    bot.tick(now)                                              # same symbol is never retried
    assert tr.submit_order.call_count == 1
    assert bot.state["trades"][0]["max_loss"] > 0
    bot.tick(now.replace(hour=15, minute=56))                  # flat-by time
    tr.close_all_positions.assert_called_once()
    bot, tr = fake_bot(bars, submit=False)                     # dry run sends nothing
    bot.tick(now); assert tr.submit_order.call_count == 0 and len(bot.state["trades"]) == 1
    bot, tr = fake_bot(bars, pnl=-500)                         # daily loss limit
    bot.tick(now); assert tr.submit_order.call_count == 0 and bot.state["halted"]
    tr.close_all_positions.assert_called_once()
    bot, tr = fake_bot(bars)
    bot.tick(now.replace(hour=9, minute=40)); assert tr.submit_order.call_count == 0   # too early
    bot.tick(now.replace(hour=15, minute=31)); assert tr.submit_order.call_count == 0  # too late


def test_inputs():
    bars = make_bars(closes_after=[100.9, 101.2, 101.25])
    now = datetime(2026, 10, 27, 9, 49, 5, tzinfo=NY)
    r = b.opening_range(bars, CFG)
    assert b.breakout_signal(101.2, r, CFG, trigger=102.0) is None       # extra trigger not reached
    assert b.breakout_signal(102.1, r, CFG, trigger=102.0) is not None   # reached it
    # no file / stale date / trade_today false / wrong symbol -> no order
    for over in ({"date": "2026-10-26"}, {"trade_today": False}, {"symbols": [{"symbol": "AMD"}]}):
        bot, tr = fake_bot(bars); write_inputs(**over); bot.tick(now)
        assert tr.submit_order.call_count == 0, over
    bot, tr = fake_bot(bars); b.INPUTS_FILE = b.HERE / "missing.json"; bot.tick(now)
    assert tr.submit_order.call_count == 0
    # Claude can tighten but not loosen: max loss 100 -> fewer shares; 9999 is clamped to 250
    bot, tr = fake_bot(bars); write_inputs(symbols=[{"symbol": "TSLA", "max_loss_usd": 9999}])
    assert bot.load_inputs(now)["symbols"]["TSLA"]["max_loss"] == 250
    bot, tr = fake_bot(bars); write_inputs(symbols=[{"symbol": "TSLA", "max_loss_usd": 30}]); bot.tick(now)
    assert tr.submit_order.call_args[0][0].qty == 40        # 30 / 0.75 risk = 40
    bot, tr = fake_bot(bars); write_inputs(max_trades=0); bot.tick(now)
    assert tr.submit_order.call_count == 0
    bot, tr = fake_bot(bars); write_inputs(max_trades=99); assert bot.load_inputs(now)["max_trades"] == 3
    bot, tr = fake_bot(bars); write_inputs(symbols=[{"symbol": s} for s in "ABCDEFG"])
    assert len(bot.load_inputs(now)["symbols"]) == 5


def test_timeout_and_heartbeat():
    from requests.adapters import HTTPAdapter
    import requests
    seen = {}
    orig = HTTPAdapter.send
    HTTPAdapter.send = lambda self, request, **kw: seen.update(kw) or (_ for _ in ()).throw(RuntimeError("stop"))
    try:
        c = NS(_session=requests.Session())
        b.add_timeout(c, 7)
        try:
            c._session.get("https://example.invalid/x")
        except RuntimeError:
            pass
        assert seen.get("timeout") == 7                         # default timeout injected
    finally:
        HTTPAdapter.send = orig
    bars = make_bars(closes_after=[100.9, 101.2, 101.25])
    now = datetime(2026, 10, 27, 9, 49, 5, tzinfo=NY)
    bot, tr = fake_bot(bars)
    logs = []
    b.log.info = lambda m, *a, **k: logs.append(m % a if a else m)
    bot.tick(now)
    assert any(x.startswith("STATUS") and "mode=SUBMIT" in x for x in logs)
    bot.state["trades"] = [{"symbol": "X"}] * 3; bot._logged_max = False
    bot.tick(now.replace(minute=50)); assert any("Trade limit reached" in x for x in logs)


def test_flatten_retry_and_stale():
    bars = make_bars(closes_after=[100.9, 101.2, 101.25])
    now = datetime(2026, 10, 27, 9, 49, 5, tzinfo=NY)
    late = now.replace(hour=15, minute=56)
    bot, tr = fake_bot(bars)                                     # failed close is retried, not forgotten
    tr.close_all_positions.side_effect = [RuntimeError("net down"), None]
    bot.tick(late); assert not bot.state["flattened"]
    bot.tick(late); assert bot.state["flattened"] and tr.close_all_positions.call_count == 2
    bot, tr = fake_bot(bars, pnl=-500)                           # same for the daily loss limit
    tr.close_all_positions.side_effect = [RuntimeError("net down"), None]
    bot.tick(now); assert not bot.state["halted"]
    bot.tick(now); assert bot.state["halted"]
    old = NS(symbol="OLD", market_value="1000", qty="10")        # position left over from an earlier day
    bot, tr = fake_bot(bars); tr.get_all_positions.return_value = [old]
    bot.tick(now); tr.close_all_positions.assert_called_once(); assert tr.submit_order.call_count == 0
    bot, tr = fake_bot(bars); tr.get_all_positions.return_value = [old]
    bot.state["trades"] = [{"symbol": "OLD", "max_loss": 10}]    # today's own position is NOT stale
    bot.tick(now); tr.close_all_positions.assert_not_called()
    bot, tr = fake_bot(bars, submit=False); tr.get_all_positions.return_value = [old]
    bot.tick(now); tr.close_all_positions.assert_not_called()    # dry run never closes anything


def test_monitor_blackout_sector_halfday():
    bars = make_bars(closes_after=[100.9, 101.2, 101.25])
    now = datetime(2026, 10, 27, 9, 49, 5, tzinfo=NY)
    # fast monitor: daily loss limit, with the market flag on
    bot, tr = fake_bot(bars, pnl=-600); bot.market_open = True
    b.RiskMonitor(bot, 3).check()
    tr.close_all_positions.assert_called_once(); assert bot.state["halted"]
    bot, tr = fake_bot(bars, pnl=-600)                                    # market closed: it stays quiet
    b.RiskMonitor(bot, 3).check(); tr.close_all_positions.assert_not_called()
    # fast monitor: one position far past the per-trade limit (limit 250 x 1.5 = 375)
    bot, tr = fake_bot(bars); bot.market_open = True
    tr.get_all_positions.return_value = [NS(symbol="TSLA", unrealized_pl="-400", market_value="9000")]
    tr.get_orders.return_value = [NS(id="o1"), NS(id="o2")]
    b.RiskMonitor(bot, 3).check()
    tr.close_position.assert_called_once_with("TSLA"); assert tr.cancel_order_by_id.call_count == 2
    tr.get_all_positions.return_value = [NS(symbol="TSLA", unrealized_pl="-300", market_value="9000")]
    tr.close_position.reset_mock(); b.RiskMonitor(bot, 3).check(); tr.close_position.assert_not_called()
    # blackout day and blacked-out stock
    bot, tr = fake_bot(bars)
    bot.cfg = dict(bot.cfg, blackout={"dates": ["2026-10-27"], "symbols": {}}); write_inputs()
    assert bot.load_inputs(now) is None
    bot.cfg = dict(bot.cfg, blackout={"dates": [], "symbols": {"TSLA": ["2026-10-27"]}}); write_inputs()
    assert bot.load_inputs(now) is None                                   # its only symbol is blacked out
    # one position per sector
    for rule, expect in ((True, 0), (False, 1)):                          # control case: rule off -> the trade happens
        bot, tr = fake_bot(bars)
        bot.cfg = dict(bot.cfg, sectors={"TSLA": "autos", "HELD": "autos"}, one_per_sector=rule)
        bot.state["stale_checked"] = True
        tr.get_all_positions.return_value = [NS(symbol="HELD", market_value="1000")]
        bot.tick(now)
        assert tr.submit_order.call_count == expect, rule
        if rule:
            assert "TSLA" not in bot.state["tried"]                       # skipped, will be retried later
    # half-day: Alpaca's real close pulls the cutoffs earlier
    bot, tr = fake_bot(bars)
    assert bot.times()[2].strftime("%H:%M") == "15:55"
    bot.close_time = datetime(2026, 11, 27, 13, 0, tzinfo=NY)
    nb, na, fb = bot.times()
    assert fb.strftime("%H:%M") == "12:55" and na.strftime("%H:%M") == "12:30"
    # end-of-day close-out runs once, two minutes after the flatten
    import journal
    calls = []
    real_close_out = journal.close_out
    journal.close_out = lambda bot_, now_: calls.append(now_)
    try:
        bot, tr = fake_bot(bars)
        bot.state["flattened"] = True
        bot.tick(now.replace(hour=15, minute=56)); assert calls == []
        bot.tick(now.replace(hour=15, minute=58)); bot.tick(now.replace(hour=15, minute=59)); assert len(calls) == 1
    finally:
        journal.close_out = real_close_out                # never leak the stand-in into other tests


def test_flatten_is_confirmed_against_the_account():
    """2026-10-28: at 15:55 Alpaca accepted the close-all call but refused MU inside the reply (its bracket legs
    were still being cancelled). The bot counted that as flat, MU stayed open with no stop, and only a manual
    /stop closed it at 15:57. A close must now be confirmed by the account itself."""
    bars = make_bars(closes_after=[100.9, 101.2, 101.25])
    late = datetime(2026, 10, 27, 15, 55, 17, tzinfo=NY)
    mu = NS(symbol="MU", qty="10", market_value="9840", unrealized_pl="33", avg_entry_price="980.68")
    refused = NS(symbol="MU", status=403, body={"message": "insufficient qty available for order"})

    # what happened: refused inside the reply, and closing the symbol alone also fails -> NOT flat, retried
    bot, tr = fake_bot(bars); bot.state["trades"] = [{"symbol": "MU", "max_loss": 108.1}]
    tr.close_all_positions.return_value = [refused]
    tr.get_all_positions.return_value = [mu]
    tr.get_orders.return_value = []
    tr.close_position.side_effect = RuntimeError("insufficient qty")
    bot.tick(late)
    assert not bot.state["flattened"] and not bot.state.get("closed_out")
    bot.tick(late.replace(minute=57))                       # retried on the next loop, journal still waits
    assert tr.close_all_positions.call_count == 2 and not bot.state.get("closed_out")

    # the second try (symbol by symbol, after its orders are cancelled) works -> flat, confirmed
    bot, tr = fake_bot(bars); bot.state["trades"] = [{"symbol": "MU", "max_loss": 108.1}]
    tr.close_all_positions.return_value = [refused]
    held = [[mu], [mu], []]                                 # flat-by tick, first check, after close_position
    tr.get_all_positions.side_effect = lambda: held.pop(0) if held else []
    leg = NS(id="leg1")
    tr.get_orders.return_value = [leg]
    bot.tick(late)
    tr.cancel_order_by_id.assert_called_with("leg1"); tr.close_position.assert_called_with("MU")
    assert bot.state["flattened"]

    # a clean close-all is confirmed at once
    bot, tr = fake_bot(bars); bot.state["trades"] = [{"symbol": "MU", "max_loss": 108.1}]
    tr.close_all_positions.return_value = [NS(symbol="MU", status=200, body={})]
    bot.tick(late); assert bot.state["flattened"]


def test_status_runs_all_session_and_after_the_close():
    """The last STATUS line used to be 15:18 (P&L -16.61) while the trade closed at +33.55: status lines
    stopped at 15:30, so the log and the dashboard kept the stale number."""
    bars = make_bars(closes_after=[100.9, 101.2, 101.25])
    logs = []
    orig = b.log.info
    b.log.info = lambda m, *a, **k: logs.append(m % a if a else m)
    try:
        bot, tr = fake_bot(bars, pnl=-16.61)
        write_inputs(trade_today=False)
        bot.tick(datetime(2026, 10, 27, 15, 40, tzinfo=NY))                     # after the entry window
        assert any(x.startswith("STATUS 15:40") and "day P&L -16.61" in x for x in logs)
        tr.get_account.return_value = NS(equity="100033.55", last_equity="100000")
        bot.tick(datetime(2026, 10, 27, 15, 55, 17, tzinfo=NY))                 # flat-by: a fresh line right away
        assert logs[-1].startswith("STATUS 15:55") and "day P&L 33.55" in logs[-1], logs[-1]
    finally:
        b.log.info = orig


if __name__ == "__main__":
    test_range_signal_size(); test_tick_flow(); test_inputs(); test_timeout_and_heartbeat(); test_flatten_retry_and_stale()
    test_monitor_blackout_sector_halfday()
    for f in ("test_state.json", "test_inputs.json"):
        (b.HERE / f).unlink(missing_ok=True)
    print("all tests passed")


def test_pause_blocks_new_entries_only():
    bars = make_bars(closes_after=[100.9, 101.2, 101.25])
    now = datetime(2026, 10, 27, 9, 49, 5, tzinfo=NY)
    bot, tr = fake_bot(bars)
    b.PAUSE_FILE.write_text("paused")
    try:
        bot.tick(now); assert tr.submit_order.call_count == 0               # a valid breakout, but paused
        bot.tick(now.replace(hour=15, minute=56)); assert bot.state["flattened"]   # the flat-by still runs
        bot, tr = fake_bot(bars, pnl=-500); b.PAUSE_FILE.write_text("paused")
        bot.tick(now); assert bot.state["halted"]                           # and so does the daily loss limit
    finally:
        b.PAUSE_FILE.unlink(missing_ok=True)
    bot, tr = fake_bot(bars)
    bot.tick(now); assert tr.submit_order.call_count == 1                   # resumed: trades again


def _ord(id_, sym, side, qty, px, legs=None, otype="market"):
    return NS(id=id_, symbol=sym, side=NS(value=side), status=NS(value="filled"), filled_qty=str(qty),
              filled_avg_price=str(px), legs=legs or [], order_type=NS(value=otype))


def test_journal_catch_up_after_an_early_stop():
    """You /stop at 23:00 IST: the bot exits before its 15:57 New York end-of-day step, so that day had no journal.
    The next start archives the day and fills the journal in from Alpaca's fills for that day only."""
    import journal, shutil
    shutil.rmtree(TEST_HISTORY, ignore_errors=True)
    jpath = TEST_HISTORY.parent / "journal.csv"
    old_journal = journal.JOURNAL
    journal.JOURNAL = jpath
    import inspect
    defaults = journal.catch_up.__defaults__
    journal.catch_up.__defaults__ = (jpath,)
    day = "2026-10-20"
    stopped = {"date": day, "tried": ["MU"], "halted": False, "flattened": False,
               "trades": [{"symbol": "MU", "qty": 10, "entry": 980.58, "stop": 969.77, "target": 1002.2, "max_loss": 108.1,
                           "time": "t", "order_id": "p1"}]}
    try:
        bars = make_bars()
        bot, tr = fake_bot(bars)
        b.STATE_FILE.write_text(json.dumps(stopped))
        sent = []
        bot.alert = lambda t, l="info": sent.append(t)
        bot.state = bot._load_state(datetime.now(NY).date().isoformat())          # a new day: the old one is archived
        arc = json.loads((TEST_HISTORY / f"{day}.json").read_text())
        assert arc["journaled"] is False and arc["trades"][0]["order_id"] == "p1"
        calls = []
        def orders(req):
            calls.append(req)
            return [_ord("p1", "MU", "buy", 10, 980.68),
                    _ord("s1", "MU", "sell", 10, 984.04)]                       # the /stop's market sell
        tr.get_orders.side_effect = orders
        bot.catch_up_journal()
        assert calls and calls[0].until is not None                              # only that day's orders
        rows = journal.read_csv(jpath)
        assert len(rows) == 1 and rows[0]["status"] == "closed" and rows[0]["pnl"] == "33.6" and rows[0]["exit_reason"] == "flatten"
        assert sent and "Journal catch-up for 2026-10-20" in sent[0] and "$33.60" in sent[0]
        assert json.loads((TEST_HISTORY / f"{day}.json").read_text())["journaled"] is True
        bot.catch_up_journal(); assert len(calls) == 1                           # done once, never again
        # a normal day (end-of-day step ran) is archived as already journaled
        b.STATE_FILE.write_text(json.dumps(dict(stopped, date="2026-10-21", closed_out=True)))
        bot._load_state(datetime.now(NY).date().isoformat())
        assert json.loads((TEST_HISTORY / "2026-10-21.json").read_text())["journaled"] is True
        bot.catch_up_journal(); assert len(calls) == 1
        # a dry run never archives (its trades are not real)
        dry, _ = fake_bot(bars, submit=False)
        b.STATE_FILE.write_text(json.dumps(dict(stopped, date="2026-10-22")))
        dry._load_state(datetime.now(NY).date().isoformat())
        assert not (TEST_HISTORY / "2026-10-22.json").exists()
        # an unresolved trade is retried, at most 5 times
        b.STATE_FILE.write_text(json.dumps(dict(stopped, date="2026-10-23")))
        bot._load_state(datetime.now(NY).date().isoformat())
        tr.get_orders.side_effect = lambda req: calls.append(req) or [_ord("p1", "MU", "buy", 10, 980.68)]   # no exit found
        for _ in range(7):
            bot.catch_up_journal()
        assert len(calls) == 1 + 5
    finally:
        journal.JOURNAL = old_journal
        journal.catch_up.__defaults__ = defaults
        shutil.rmtree(TEST_HISTORY, ignore_errors=True)


def gap_bot(gaps, day="2026-10-27", trades=None):
    """A bot in gap-down mode. gaps: {symbol: (9:30 open, 9:30 close)}; every stock closed at 100 yesterday, ATR 3."""
    rows = {}
    for sym, (o, c) in gaps.items():
        idx = pd.date_range(f"{day} 09:30", periods=2, freq="1min", tz=NY)
        rows[sym] = pd.DataFrame([dict(open=o, high=max(o, c) + .05, low=min(o, c) - .05, close=c, volume=1000),
                                  dict(open=c, high=c + .05, low=c - .05, close=c, volume=1000)], index=idx)
    bot, tr = fake_bot(make_bars())
    bot.cfg = dict(bot.cfg, strategy="gap_fade", gap_fade={"min_gap_pct": 2.0, "atr_k": 0.2, "rr": 1.0, "last_entry": "09:34"},
                   one_per_sector=False)
    bot.strategy = "gap_fade"
    bot.facts = {"date": day, "by": {s: {"prev_close": 100.0, "atr": 3.0, "last_day": "x"} for s in gaps}}
    bot.fetch_bars = lambda syms, now: {s: rows[s] for s in syms if s in rows}
    write_inputs(symbols=[])
    bot.cfg["universe"] = list(gaps)
    return bot, tr


def test_gap_strategy_entries():
    t = lambda h, m, s=5: datetime(2026, 10, 27, h, m, s, tzinfo=NY)
    bot, tr = gap_bot({"AAA": (97.5, 97.6), "BBB": (98.5, 98.4), "CCC": (95.0, 95.2), "DDD": (103.0, 103.1)})
    bot.tick(t(9, 30, 40)); assert tr.submit_order.call_count == 0             # 9:30 bar not finished yet
    bot.tick(t(9, 31))
    reqs = [c[0][0] for c in tr.submit_order.call_args_list]
    assert [r.symbol for r in reqs] == ["CCC", "AAA"]                          # -5 % first, then -2.5 %; BBB -1.5 % and DDD up: no
    ccc = reqs[0]
    assert ccc.stop_loss.stop_price == 94.6 and ccc.take_profit.limit_price == 95.8   # 95.2 - 0.6 ; 95.2 + 1R
    tr0 = bot.state["trades"][0]
    assert tr0["strategy"] == "gap_fade" and tr0["gap"] == -5.0
    assert set(bot.state["tried"]) >= {"AAA", "BBB", "CCC", "DDD"}
    bot.tick(t(9, 32)); assert tr.submit_order.call_count == 2                 # one look per stock per day
    bot, tr = gap_bot({"AAA": (97.5, 97.6)})
    bot.tick(t(9, 34)); assert tr.submit_order.call_count == 0                 # window closed at 9:34
    bot, tr = gap_bot({"AAA": (97.5, 97.6)})
    write_inputs(symbols=[], trade_today=False)
    bot.tick(t(9, 31)); assert tr.submit_order.call_count == 0                 # Claude/blackout said no trading today
    bot, tr = gap_bot({"AAA": (97.5, 97.6)})
    bot.facts = {}
    bot._facts_tried = t(9, 30)                                                # facts failed to load a moment ago
    bot.tick(t(9, 31)); assert tr.submit_order.call_count == 0                 # no facts, no trade (never a guess)
    bot, tr = gap_bot({"AAA": (97.5, 97.6)})
    b.PAUSE_FILE.write_text("p")
    try:
        bot.tick(t(9, 31)); assert tr.submit_order.call_count == 0             # pause works in gap mode too
    finally:
        b.PAUSE_FILE.unlink(missing_ok=True)


def test_gap_mode_turns_off_breakout_entries_and_respects_limits():
    bot, tr = gap_bot({"AAA": (97.5, 97.6)})
    bot.scan(datetime(2026, 10, 27, 10, 0, tzinfo=NY), 0.0, {}, {"symbols": {"AAA": {"trigger": None, "max_loss": 250}},
                                                                  "max_trades": 3})
    assert tr.submit_order.call_count == 0                                     # real-time bar events cannot enter by ORB rules
    bot, tr = gap_bot({"AAA": (97.5, 97.6), "BBB": (96.0, 96.1), "CCC": (95.0, 95.2)})
    bot.cfg["max_open_positions"] = 2
    bot.tick(datetime(2026, 10, 27, 9, 31, 5, tzinfo=NY))
    assert tr.submit_order.call_count == 2                                     # the open-position limit holds
    bot, tr = gap_bot({"AAA": (97.5, 97.6)}, )
    bot.cfg["max_loss_per_trade_usd"] = 30
    bot.tick(datetime(2026, 10, 27, 9, 31, 5, tzinfo=NY))
    assert tr.submit_order.call_args[0][0].qty == 50                           # $30 / $0.60 risk a share


def test_gap_facts_from_history():
    import strategies
    days = pd.bdate_range("2026-09-15", "2026-10-26")
    frames = {}
    for k, sym in enumerate(("AAA", "BBB")):
        rows, idx = [], []
        for i, d in enumerate(days):
            base = 100 + k * 10 + i * 0.1
            for m in range(390):
                ts = pd.Timestamp(f"{d.date()} 09:30", tz=NY) + pd.Timedelta(minutes=m)
                rows.append(dict(open=base, high=base + 1.0, low=base - 1.0, close=base + 0.2, volume=100)); idx.append(ts)
        frames[sym] = pd.DataFrame(rows, index=pd.DatetimeIndex(idx).tz_convert("UTC"))
    bot, tr = gap_bot({"AAA": (97.5, 97.6), "BBB": (97.5, 97.6)})
    bot.facts = {}
    seen = {}
    def bars(req):
        seen["req"] = req
        return NS(df=pd.concat(frames))
    bot.data.get_stock_bars.side_effect = bars
    assert bot.prepare_gap_facts(datetime(2026, 10, 27, 9, 0, tzinfo=NY), force=True)
    f = bot.facts["by"]["AAA"]
    assert abs(f["prev_close"] - (100 + (len(days) - 1) * 0.1 + 0.2)) < 1e-9 and abs(f["atr"] - 2.0) < 1e-6
    assert str(seen["req"].feed.value) == "sip" and seen["req"].end.date() <= datetime(2026, 10, 27).date()   # nothing from today
    assert bot.prepare_gap_facts(datetime(2026, 10, 27, 9, 5, tzinfo=NY))      # cached for the day
