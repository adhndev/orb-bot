"""Offline tests for the real-time layer: stream hub, event handling, Claude's entry gate and news review.
No network, no keys. Run: python test_realtime.py"""
import json
import queue
import time
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import MagicMock
import pandas as pd
import advisor as adv
import orb_bot as b
import stream
from test_advisor import FakeLLM, FakeData, FakeNews, make as make_adv
from test_orb_bot import make_bars, fake_bot, write_inputs, CFG

NY = b.NY
NOW = datetime(2026, 10, 27, 9, 48, 5, tzinfo=NY)
SIG = {"entry": 101.25, "stop": 100.5, "target": 102.75, "risk": 0.75}


# ---------- stream.py ----------
def test_event_shapes():
    bar = NS(symbol="TSLA", timestamp=NOW, open="1", high="2", low="0.5", close="1.5", volume="100")
    k, sym, d = stream.bar_event(bar)
    assert (k, sym, d["close"], d["volume"]) == ("bar", "TSLA", 1.5, 100.0)
    n = NS(id=7, headline="TSLA recalls cars", symbols=["tsla", "x"], created_at=NOW, summary=None)
    k, d = stream.news_event(n)
    assert k == "news" and d["symbols"] == ["TSLA", "X"] and d["id"] == "7" and d["summary"] == ""
    o = NS(symbol="TSLA", id="o1", side=NS(value="buy"), order_type="market", status=NS(value="filled"),
           qty="10", filled_qty="10", filled_avg_price="101.3")
    u = NS(event=NS(value="fill"), order=o, qty="10", price="101.3", timestamp=NOW)
    k, d = stream.trade_event(u)
    assert k == "trade" and d["event"] == "fill" and d["side"] == "buy" and d["avg_price"] == 101.3 and d["qty"] == 10.0


def test_hub_reconnects_and_reports_status():
    q = queue.Queue()
    calls = {"n": 0}

    class FakeStream:
        def __init__(self, name, on_event, boom):
            self.name, self.on_event, self.boom = name, on_event, boom

        def run(self):
            if self.boom:
                raise ConnectionError("socket closed")
            self.on_event(("bar", "TSLA", {"close": 1.0}))
            time.sleep(5)  # stays "connected" until the test ends

        def stop(self):
            pass

    def factory(name, on_event):
        calls["n"] += 1
        return FakeStream(name, on_event, boom=calls["n"] == 1)   # first connection attempt fails

    hub = stream.StreamHub("k", "s", ["TSLA"], q, feeds=("bars", "news"), factory=factory)
    assert hub.status() == {"bars": False, "news": False}          # nothing delivered yet = not healthy
    hub.start()
    ev = q.get(timeout=8)                                          # the supervisor backed off 2 s and reconnected
    assert ev[0] == "bar" and calls["n"] >= 2
    st = hub.status()
    assert st["bars"] is True or st["news"] is True
    assert hub.status(stale_seconds=-1)["bars"] is False            # staleness window respected
    hub.stop()


# ---------- advisor gate / review ----------
def test_parsers():
    assert adv.parse_gate({"decision": "approve", "size_factor": 0.5, "reason": "ok"})["size_factor"] == 0.5
    assert adv.parse_gate({"decision": "approve", "size_factor": 7})["size_factor"] == 1.0         # never enlarge
    assert adv.parse_gate({"decision": "approve", "size_factor": 0.01})["size_factor"] == 0.25
    assert adv.parse_gate({"decision": "approve", "size_factor": "x"})["size_factor"] == 1.0
    for junk in (None, "yes", {}, {"decision": "maybe"}, {"decision": "veto", "reason": "bad news"}):
        assert adv.parse_gate(junk)["decision"] == "veto"
    assert adv.parse_gate({"decision": "veto", "reason": "bad news"})["reason"] == "bad news"
    assert adv.parse_position_review({"action": "exit", "reason": "halt"})["action"] == "exit"
    for junk in (None, "exit", {}, {"action": "sell everything"}, {"action": "hold"}):
        assert adv.parse_position_review(junk)["action"] == "hold"


def test_entry_gate():
    llm = FakeLLM('{"decision":"approve","size_factor":0.5,"reason":"market weak"}')
    a, _, _ = make_adv(llm, syms=("TSLA",))
    a.remember_headline("TSLA", "Tesla beats on deliveries", "09:40")
    g = a.entry_gate(NOW, "TSLA", SIG, 40, 30.0, -20.0, [], {"note": "gap up"})
    assert g == {"decision": "approve", "size_factor": 0.5, "reason": "market weak"}
    p = llm.prompts[0]
    assert "BUY TSLA x40" in p and "Tesla beats on deliveries" in p and "gap up" in p and "<headlines>" in p
    assert a.st["calls"] == 1 and a.st["spend_usd"] > 0                  # counted against the daily caps
    assert "gate" in open(adv.LLM_LOG).read()                            # and logged

    a, _, _ = make_adv(FakeLLM('{"decision":"veto","size_factor":1,"reason":"offering announced"}'), syms=("TSLA",))
    assert a.entry_gate(NOW, "TSLA", SIG, 40, 30.0, 0, [], {})["decision"] == "veto"

    a, _, _ = make_adv(FakeLLM("I think it looks fine!"), syms=("TSLA",))             # unreadable = veto
    assert a.entry_gate(NOW, "TSLA", SIG, 40, 30.0, 0, [], {})["decision"] == "veto"

    a, _, _ = make_adv(FakeLLM(fail=True), syms=("TSLA",))                            # API down = veto by default
    g = a.entry_gate(NOW, "TSLA", SIG, 40, 30.0, 0, [], {})
    assert g["decision"] == "veto" and "fail-safe" in g["reason"]
    a.cfg = dict(a.cfg, realtime=dict(a.cfg["realtime"], gate_on_error="approve"))
    g = a.entry_gate(NOW, "TSLA", SIG, 40, 30.0, 0, [], {})
    assert g["decision"] == "approve" and g["size_factor"] == 1.0

    a, _, _ = make_adv(FakeLLM('{"decision":"approve"}'), syms=("TSLA",))
    a.st["halted"] = True
    assert a.entry_gate(NOW, "TSLA", SIG, 40, 30.0, 0, [], {})["decision"] == "veto"  # halted = no entries


def test_position_review_and_headlines():
    a, _, _ = make_adv(FakeLLM('{"action":"exit","reason":"trading halted"}'), syms=("TSLA",))
    sent = []
    a.notifier = NS(send=lambda m, lvl="info": sent.append((m, lvl)))
    r = a.position_review(NOW, "TSLA", "Trading halted pending news", {"qty": "40"})
    assert r["action"] == "exit" and sent and sent[0][1] == "critical"
    a, _, _ = make_adv(FakeLLM(fail=True), syms=("TSLA",))
    assert a.position_review(NOW, "TSLA", "x", {})["action"] == "hold"               # failure never flattens
    a, _, _ = make_adv(FakeLLM('{"action":"hold","reason":"minor"}'), syms=("TSLA",))
    for i in range(8):
        a.remember_headline("TSLA", "h%d" % (i % 7), "09:%02d" % i)
    a.remember_headline("TSLA", "h0", "09:00")                                        # exact duplicate ignored
    assert len(a.heads["TSLA"]) == 5


def test_force_runs_intraday_early_but_rate_limited():
    a, path, _ = make_adv(FakeLLM('{}'), syms=("TSLA",))
    ran = []
    a.intraday = lambda now: ran.append(now)
    a.st.update(premarket_done=True, last_intraday=(NOW - timedelta(minutes=1)).isoformat())
    a.step(NOW, True, True)
    assert ran == []                                                 # only 1 min since the last look: not due
    a.force = True
    a.step(NOW, True, True)
    assert len(ran) == 1 and a.force is False                        # a headline asked for an early look
    a.force = True
    a.step(NOW + timedelta(seconds=20), True, True)
    assert len(ran) == 1                                             # but never more than once a minute
    a.force = True
    a.step(NOW + timedelta(seconds=70), True, True)
    assert len(ran) == 2


# ---------- the bot reacting to events ----------
def bar_ev(close, t):
    return ("bar", "TSLA", {"time": pd.Timestamp(t), "open": close, "high": close + .05, "low": close - .05,
                            "close": close, "volume": 1000})


def rt_bot(llm_text=None, gate=True):
    bars = make_bars(closes_after=[100.9, 101.2, 101.25])
    bot, tr = fake_bot(bars)
    bot.cfg = dict(bot.cfg, realtime=dict(CFG["realtime"], gate=gate))
    bot.state["stale_checked"] = True
    bot.market_open = True
    bot.events = queue.Queue()
    if llm_text is not None:
        a, _, _ = make_adv(FakeLLM(llm_text), syms=("TSLA",))
        a.cfg = bot.cfg
        a.bot, bot.advisor = bot, a
    return bot, tr


def test_bar_event_places_order_when_gate_approves():
    bot, tr = rt_bot('{"decision":"approve","size_factor":1.0,"reason":"clean"}')
    bot.handle_event(bar_ev(101.25, "2026-10-27 09:47:00-04:00"), NOW)
    assert tr.submit_order.call_count == 1
    t = bot.state["trades"][0]
    assert t["symbol"] == "TSLA" and t["claude_gate"]["decision"] == "approve" and t["status"] == "submitted"
    bot.handle_event(bar_ev(101.3, "2026-10-27 09:48:00-04:00"), NOW + timedelta(seconds=30))
    assert tr.submit_order.call_count == 1                           # same stock is never entered twice


def test_gate_veto_and_shrink():
    bot, tr = rt_bot('{"decision":"veto","size_factor":1,"reason":"offering announced"}')
    bot.handle_event(bar_ev(101.25, "2026-10-27 09:47:00-04:00"), NOW)
    assert tr.submit_order.call_count == 0 and "TSLA" in bot.state["tried"] and not bot.state["trades"]
    bot, tr = rt_bot('{"decision":"approve","size_factor":0.5,"reason":"weak tape"}')
    bot.handle_event(bar_ev(101.25, "2026-10-27 09:47:00-04:00"), NOW)
    assert tr.submit_order.call_count == 1
    full, _ = rt_bot('{"decision":"approve","size_factor":1.0,"reason":"x"}')
    full.handle_event(bar_ev(101.25, "2026-10-27 09:47:00-04:00"), NOW)
    assert 0 < bot.state["trades"][0]["qty"] <= full.state["trades"][0]["qty"] // 2 + 1


def test_no_gate_when_disabled_and_ignores_off_list_bars():
    bot, tr = rt_bot('{"decision":"veto"}', gate=False)
    bot.handle_event(bar_ev(101.25, "2026-10-27 09:47:00-04:00"), NOW)
    assert tr.submit_order.call_count == 1                           # gate off = rules only
    other = ("bar", "ZZZZ", {"time": pd.Timestamp("2026-10-27 09:47:00-04:00"), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1})
    bot.handle_event(other, NOW)                                     # not on the shortlist: no crash, no order


def test_news_exit_and_force():
    bot, tr = rt_bot('{"action":"exit","reason":"halt"}')
    bot.close_symbol = MagicMock(return_value=True)
    bot._last_positions = {"TSLA": NS(qty="40", avg_entry_price="101.2", unrealized_pl="-10")}
    bot.state["trades"].append({"symbol": "TSLA", "qty": 40, "entry": 101.2, "max_loss": 30, "status": "filled"})
    n = {"id": "n1", "headline": "SEC halts trading in Tesla", "symbols": ["TSLA"], "time": NOW, "summary": ""}
    bot.handle_event(("news", n), NOW)
    bot.close_symbol.assert_called_once_with("TSLA")
    assert bot.state["trades"][0]["closed_by"] == "claude-news"
    bot.handle_event(("news", n), NOW)                               # duplicate delivery is ignored
    bot.close_symbol.assert_called_once()
    bot2, _ = rt_bot('{"action":"hold"}')
    bot2.close_symbol = MagicMock()
    bot2.handle_event(("news", dict(n, id="n2", symbols=["TSLA"])), NOW)   # shortlisted but not held
    assert bot2.advisor.force is True and not bot2.close_symbol.called
    bot2.handle_event(("news", dict(n, id="n3", time=NOW - timedelta(hours=3))), NOW)   # stale headline ignored
    assert "n3" in bot2._seen_news


def test_trade_updates_confirm_and_failed_orders_do_not_count():
    bot, tr = rt_bot()
    bot.state["trades"].append({"symbol": "TSLA", "qty": 40, "entry": 101.2, "max_loss": 30, "status": "submitted", "order_id": "o1"})
    bot.alerts = []
    bot.alert = lambda m, lvl="info": bot.alerts.append(m)
    fill = {"event": "fill", "symbol": "TSLA", "order_id": "o1", "side": "buy", "order_type": "market", "status": "filled",
            "qty": 40.0, "filled_qty": 40.0, "price": 101.3, "avg_price": 101.3, "time": NOW}
    bot.handle_event(("trade", fill), NOW)
    assert bot.state["trades"][0]["status"] == "filled" and bot.state["trades"][0]["fill_price"] == 101.3
    sell = dict(fill, side="sell", order_type="limit", order_id="o2", avg_price=102.75, price=102.75)
    bot.handle_event(("trade", sell), NOW)
    assert bot.state["trades"][0]["exit"]["how"] == "target" and bot.state["trades"][0]["exit"]["pnl"] > 0
    assert any("CLOSED" in m for m in bot.alerts)
    bot.state["trades"].append({"symbol": "AMD", "qty": 5, "entry": 1, "max_loss": 1, "status": "submitted", "order_id": "o3"})
    assert bot.n_trades() == 2
    bot.handle_event(("trade", dict(fill, symbol="AMD", order_id="o3", event="rejected")), NOW)
    assert bot.state["trades"][1]["status"] == "failed" and bot.n_trades() == 1


def test_idle_consumes_events_and_survives_errors():
    bot, _ = rt_bot()
    seen = []
    bot.handle_event = lambda ev, now: (seen.append(ev[0]), 1 / 0)[0]    # a crashing handler must not kill the loop
    bot.events.put(("bar", "X", {}))
    bot.events.put(("news", {}))
    t0 = time.monotonic()
    bot._idle(1.5)
    assert seen == ["bar", "news"] and time.monotonic() - t0 >= 1.4


def test_risk_monitor_does_not_wait_for_a_busy_main_loop():
    import threading
    bars = make_bars(closes_after=[100.9])
    bot, tr = fake_bot(bars, pnl=-600.0)                          # beyond the $500 daily loss limit
    bot.market_open = True
    release = threading.Event()
    held = threading.Event()

    def hog():                                                    # the main loop, stuck in a slow Claude/skill call
        with bot.lock:
            held.set()
            release.wait(10)
    th = threading.Thread(target=hog, daemon=True)
    th.start()
    held.wait(2)
    t0 = time.monotonic()
    b.RiskMonitor(bot, 3).check()
    took = time.monotonic() - t0
    release.set()
    assert tr.close_all_positions.called and bot.state["halted"] and took < 5, took


def reprice_bot(latest, max_loss=30):
    bot, tr = rt_bot('{"decision":"approve","size_factor":1.0,"reason":"clean"}')
    write_inputs(symbols=[{"symbol": "TSLA", "max_loss_usd": max_loss}])
    bot.data.get_stock_latest_trade = MagicMock(return_value={"TSLA": NS(price=latest)})
    return bot, tr


def test_price_is_rechecked_after_the_slow_checks():
    bot, tr = reprice_bot(101.25)                                  # unchanged: order as planned
    bot.handle_event(bar_ev(101.25, "2026-10-27 09:47:00-04:00"), NOW)
    assert tr.submit_order.call_count == 1 and bot.state["trades"][0]["qty"] == 40   # $30 / $0.75 risk
    bot, tr = reprice_bot(101.40)                                  # ran up: stop is fixed, so risk per share grew
    bot.handle_event(bar_ev(101.25, "2026-10-27 09:47:00-04:00"), NOW)
    t = bot.state["trades"][0]
    assert tr.submit_order.call_count == 1 and t["qty"] == 33 and t["entry"] == 101.40   # $30 / $0.90
    assert t["max_loss"] <= 30.0
    bot, tr = reprice_bot(100.90)                                  # fell back inside the range: breakout failed
    bot.handle_event(bar_ev(101.25, "2026-10-27 09:47:00-04:00"), NOW)
    assert tr.submit_order.call_count == 0 and "TSLA" in bot.state["tried"]
    bot, tr = reprice_bot(None)                                    # no fresh price available: keep the plan
    bot.data.get_stock_latest_trade = MagicMock(side_effect=RuntimeError("down"))
    bot.handle_event(bar_ev(101.25, "2026-10-27 09:47:00-04:00"), NOW)
    assert tr.submit_order.call_count == 1


def test_partial_opening_range_is_not_locked_in():
    short = make_bars().iloc[:14]                                  # bars through 09:43; the 09:44 bar has not arrived yet
    bot, tr = fake_bot(short)
    write_inputs()
    now = datetime(2026, 10, 27, 9, 45, 5, tzinfo=NY)
    inputs = bot.load_inputs(now)
    bot.scan(now, 0.0, {}, inputs)
    assert "TSLA" not in bot.ranges and "TSLA" not in bot.state["tried"]               # waits instead of caching
    bot.data.get_stock_bars.return_value = NS(df=pd.concat({"TSLA": make_bars()}))     # the 09:44 bar arrives
    bot.scan(now + timedelta(seconds=20), 0.0, {}, inputs)
    assert bot.ranges["TSLA"] == {"high": 101.0, "low": 100.0, "mid": 100.5}


def test_leftover_stop_file_is_cleared_at_startup():
    old = b.STOP_FILE
    b.STOP_FILE = b.HERE / "t_STOP2"
    try:
        assert b.clear_stale_stop() is False                          # nothing to clear
        b.STOP_FILE.write_text("old request")
        assert b.clear_stale_stop() is True and not b.STOP_FILE.exists()
    finally:
        b.STOP_FILE = old


def test_stop_file_cuts_a_long_wait_short():
    from pathlib import Path
    old = b.STOP_FILE
    for events in (queue.Queue(), None):
        bot, _ = rt_bot()
        bot.events = events
        b.STOP_FILE = b.HERE / "t_STOP"
        b.STOP_FILE.write_text("x")
        t0 = time.monotonic()
        bot._idle(300)                                               # would otherwise sleep 5 minutes
        assert time.monotonic() - t0 < 2
        b.STOP_FILE.unlink()
    b.STOP_FILE = old


def test_stream_silence_warns_once():
    bot, _ = rt_bot()
    bot.hub = NS(status=lambda stale: {"bars": False})
    bot._stream_warned = False
    bot.alerts = []
    bot.alert = lambda m, lvl="info": bot.alerts.append(m)
    bot._check_streams(NOW)
    bot._check_streams(NOW)
    assert len(bot.alerts) == 1 and "slower" in bot.alerts[0]
    bot.hub = NS(status=lambda stale: {"bars": True})
    bot._check_streams(NOW)
    assert bot._stream_warned is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    for f in b.HERE.glob("t_*"):
        f.unlink()
    for f in b.HERE.glob("test_*.json"):
        f.unlink()
    print("realtime tests passed")
