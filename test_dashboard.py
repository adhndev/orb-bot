"""Offline tests for the dashboard. Run: python test_dashboard.py"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import dashboard as d

TMP = Path(tempfile.mkdtemp())
d.HERE = TMP
(TMP / "dummy_bot.py").write_text("import time\nwhile True:\n    time.sleep(0.2)\n")
d.command = lambda name, mode: [sys.executable, str(TMP / "dummy_bot.py")]
os.environ.pop("ANTHROPIC_API_KEY", None)
ALPACA = {"/v2/account": {"equity": "100033.55", "last_equity": "100000", "cash": "90000"},
          "/v2/positions": [{"symbol": "MU", "qty": "10", "side": "long", "avg_entry_price": "980.68",
                             "current_price": "984.00", "unrealized_pl": "33.2"}],
          "/v2/orders?status=open&limit=200&nested=true": [{"symbol": "MU", "type": "market", "side": "buy", "legs": [
              {"symbol": "MU", "type": "stop", "side": "sell", "stop_price": "969.77"},
              {"symbol": "MU", "type": "limit", "side": "sell", "limit_price": "1002.20"}]}]}
F = lambda sym, side, qty, px, t: {"id": "%s-%s-%s" % (sym, side, t), "symbol": sym, "side": side, "qty": str(qty),
                                   "price": str(px), "transaction_time": "2026-10-%sT14:00:00Z" % t}
ALPACA["/v2/account/activities/FILL?direction=asc&page_size=100"] = [
    F("AMD", "buy", 10, 100, 27), F("AMD", "sell", 10, 103, 27),                       # +30
    F("TSLA", "buy", 5, 200, 27), F("TSLA", "buy", 5, 202, 27), F("TSLA", "sell", 4, 199, 27),
    F("TSLA", "sell", 6, 198, 27),                                                     # avg 201: -8 and -18 = -26
    F("NVDA", "sell", 3, 50, 28), F("NVDA", "buy", 3, 48, 28),                         # short +6
    F("MU", "buy", 10, 980.68, 28)]                                                    # still open: no result yet
ALPACA["/v2/account/portfolio/history?period=all&timeframe=1D"] = {"base_value": 100000}
ALPACA["/v2/account"]["buying_power"] = "180000"
d.alpaca_get = lambda path, timeout=10: ALPACA[path]          # tests never touch the network
os.environ["ALPACA_API_KEY"] = os.environ["ALPACA_SECRET_KEY"] = "x"

srv = d.make_server(0)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % PORT


def get(path, host=None):
    req = urllib.request.Request(BASE + path, headers={"Host": host} if host else {})
    return urllib.request.urlopen(req, timeout=5)


def post(path, body=None, token=d.TOKEN, origin=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["X-Token"] = token
    if origin:
        h["Origin"] = origin
    req = urllib.request.Request(BASE + path, data=json.dumps(body or {}).encode(), headers=h, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_helpers():
    p = TMP / "x.log"
    p.write_text("\n".join("line%d" % i for i in range(500)))
    assert d.tail(p, nlines=3) == ["line497", "line498", "line499"] and d.tail(TMP / "nope") == []
    assert d.pid_alive(os.getpid()) and not d.pid_alive(None) and not d.pid_alive(4000000)
    assert d.read_json(TMP / "nope.json", 5) == 5


def test_status_reads_files():
    now = datetime.now(timezone.utc)
    (TMP / "heartbeat.json").write_text(json.dumps({"time": (now - timedelta(seconds=30)).isoformat(), "pid": 4000000, "submit": True,
                                                    "market_open": True, "errors_in_a_row": 0, "stopped": None,
                                                    "streams": {"bars": True, "news": False}}))
    (TMP / "bot_state.json").write_text(json.dumps({"date": "2026-10-27", "tried": ["TSLA"], "halted": False, "flattened": False,
                                                    "trades": [{"symbol": "TSLA", "qty": 40, "entry": 101.2, "stop": 100.5,
                                                                "target": 102.6, "max_loss": 28, "status": "submitted"}]}))
    (TMP / "bot.log").write_text("x\n2026-10-27 STATUS 10:01 NY | day P&L -12.50 | mode=paper\nlast line\n", encoding="utf-8")
    (TMP / "inputs.json").write_text(json.dumps({"date": "2026-10-27", "trade_today": True, "max_trades": 2, "note": "gap up",
                                                 "symbols": [{"symbol": "TSLA"}, {"symbol": "AMD"}]}))
    (TMP / "llm_log.jsonl").write_text(json.dumps({"time": "2026-10-27T09:00:00-04:00", "tag": "premarket", "model": "m",
                                                   "cost_usd": 0.02, "prompt": "p", "response": "R" * 500}) + "\nnot json\n")
    s = d.build_status()
    assert s["bot"]["heartbeat_age"] in range(29, 40) and s["pnl"] == -12.5 and s["streams"] == {"bars": True, "news": False}
    assert s["state"]["trades"][0]["symbol"] == "TSLA" and s["claude"]["symbols"] == ["TSLA", "AMD"]
    assert len(s["claude"]["decisions"]) == 1 and len(s["claude"]["decisions"][0]["text"]) == 260
    assert s["bot"]["running"] is False and "ANTHROPIC_API_KEY" in s["missing_env"]
    json.dumps(s)
    (TMP / "heartbeat.json").unlink()
    assert d.build_status()["bot"]["heartbeat_age"] is None                       # no heartbeat file: no crash


def test_page_and_security():
    html = get("/").read().decode()
    assert d.TOKEN in html and "__TOKEN__" not in html and "ORB Bot Control" in html
    assert json.loads(get("/api/status").read())["bot"]["running"] is False
    try:
        get("/api/status", host="evil.example.com")
        assert False, "foreign Host header must be refused"
    except urllib.error.HTTPError as e:
        assert e.code == 403
    assert post("/api/start", {"mode": "dry"}, token=None)[0] == 403
    assert post("/api/start", {"mode": "dry"}, token="wrong")[0] == 403
    assert post("/api/start", {"mode": "dry"}, origin="http://evil.example.com")[0] == 403


def test_start_stop_kill_cycle():
    (TMP / "STOP").write_text("old")                                              # leftover from a previous stop
    code, r = post("/api/start", {"mode": "dry"})
    assert code == 200 and r["ok"] and not (TMP / "STOP").exists()                # leftover STOP removed
    time.sleep(0.5)
    s = d.build_status()
    assert s["bot"]["running"] and s["bot"]["pid"]
    assert post("/api/start", {"mode": "dry"})[1]["ok"] is False                  # no double start
    code, r = post("/api/stop")
    assert r["ok"] and (TMP / "STOP").exists() and d.build_status()["bot"]["stopping"]
    code, r = post("/api/kill")                                                   # the dummy never obeys STOP: force it
    assert r["ok"]
    deadline = time.time() + 5
    while time.time() < deadline and d.build_status()["bot"]["running"]:
        time.sleep(0.2)
    assert d.build_status()["bot"]["running"] is False
    assert post("/api/stop")[1]["ok"] is False and not (TMP / "STOP").exists()    # stopping a stopped bot is harmless
    assert d.running("watchdog")                                                  # started together with the bot
    assert post("/api/watchdog/stop")[1]["ok"]
    time.sleep(0.5)
    assert not d.running("watchdog")
    code, r = post("/api/watchdog/start", {"mode": "paper"})
    assert r["ok"] and d.running("watchdog")
    first = d.running("watchdog")
    code, r = post("/api/start", {"mode": "paper"})                               # paper start replaces the old watchdog
    assert r["ok"] and "auto-close ON" in r["msg"] and d.running("watchdog") != first
    post("/api/kill")
    post("/api/watchdog/stop")


def test_a_bot_that_crashes_at_start_is_reported():
    (TMP / "crash_bot.py").write_text("import sys\nprint('starting')\nimport not_a_real_module_xyz\n")
    old = d.command
    d.command = lambda name, mode: [sys.executable, str(TMP / ("crash_bot.py" if name == "bot" else "dummy_bot.py"))]
    try:
        ok, msg = d.start("bot", "paper")
        assert not ok and "stopped right after starting" in msg and "not_a_real_module_xyz" in msg
        assert "ModuleNotFoundError" in (TMP / "bot_console.log").read_text()
        assert not d.running("bot") and "bot" not in d.procs
    finally:
        d.command = old


def test_refuses_to_start_without_alpaca_keys():
    k = os.environ.pop("ALPACA_API_KEY")
    try:
        code, r = post("/api/start", {"mode": "paper"})
        assert r["ok"] is False and "Alpaca keys are not set" in r["msg"]
    finally:
        os.environ["ALPACA_API_KEY"] = k


def test_systemd_mode_drives_units_not_processes():
    """On the server the buttons must go through systemd, so a click can never start a second bot."""
    active, calls = {}, []

    def fake(*args):
        calls.append(args)
        verb, units = args[0], [a for a in args[1:] if a.endswith(".service")]
        if verb == "show":
            return 0, str(active.get(units[0], 0))
        if verb in ("start", "enable"):
            if "--now" in args or verb == "start":
                for u in units:
                    active[u] = 1000 + len(active)
            return 0, ""
        if verb == "disable":
            if "--now" in args:
                for u in units:
                    active.pop(u, None)
            return 0, ""
        return 1, "unexpected"

    old = d.SYSTEMD, d.systemctl
    d.SYSTEMD, d.systemctl = True, fake
    try:
        (TMP / "STOP").unlink(missing_ok=True)
        assert d.running("bot") is None and d.running("watchdog") is None
        ok, msg = d.start_bot_with_watchdog("paper")
        assert ok and "auto-close ON" in msg, msg
        assert ("enable", "--now", "orb-bot.service") in calls and ("enable", "--now", "orb-watchdog.service") in calls
        assert d.running("bot") and d.running("watchdog")
        assert d.start("bot", "dry") == (False, "bot is already running")      # no second bot, paper or dry
        assert not any(c[0] == "start" for c in calls)
        ok, msg = d.stop("bot")                                                 # graceful: STOP file + no reboot comeback
        assert ok and (TMP / "STOP").exists() and ("disable", "orb-bot.service") in calls
        (TMP / "STOP").unlink()
        ok, msg = d.stop("bot", force=True)
        assert ok and "force-stopped" in msg and d.running("bot") is None
        ok, msg = d.start("bot", "dry")                                         # a dry run is started, never enabled
        assert ok and calls[-1] == ("start", "orb-bot-dry.service")
        d.stop("bot", force=True); d.stop("watchdog", force=True)
        assert d.running("bot") is None and d.running("watchdog") is None
    finally:
        d.SYSTEMD, d.systemctl = old
        (TMP / "STOP").unlink(missing_ok=True)


def test_bot_and_watchdog_started_outside_are_recognised():
    """A bot from start_trading.bat is not in the dashboard's PID list. It must still count as running,
    or a click (or a Telegram /start) would launch a second bot on the same account."""
    now = datetime.now(timezone.utc)
    hb = {"time": (now - timedelta(seconds=20)).isoformat(), "pid": os.getpid(), "submit": True, "stopped": None}
    (TMP / "heartbeat.json").write_text(json.dumps(hb))
    try:
        assert d.running("bot") == os.getpid()
        assert d.start("bot", "paper") == (False, "bot is already running")
        (TMP / "heartbeat.json").write_text(json.dumps(dict(hb, time=(now - timedelta(minutes=30)).isoformat())))
        assert d.running("bot") is None                                           # stale heartbeat: PID may be reused
        (TMP / "heartbeat.json").write_text(json.dumps(dict(hb, stopped="user (STOP file)")))
        assert d.running("bot") is None                                           # it said it stopped
        (TMP / "heartbeat.json").write_text(json.dumps(dict(hb, pid=4000000)))
        assert d.running("bot") is None                                           # dead process
        (TMP / "watchdog.json").write_text(json.dumps({"pid": os.getpid(), "time": now.isoformat(), "flatten": True}))
        assert d.running("watchdog") == os.getpid()
    finally:
        (TMP / "heartbeat.json").unlink(missing_ok=True)
        (TMP / "watchdog.json").unlink(missing_ok=True)


class FakeTelegram:
    def __init__(self, updates):
        self.updates, self.sent, self.calls = list(updates), [], []

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return {"ok": True, "result": out}
        if method == "sendMessage":
            self.sent.append((params["chat_id"], params["text"]))
        return {"ok": True}


def msg(uid, text, chat=555, age=0, now=1_800_000_000):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "date": now - age, "text": text}}


def test_telegram_menu_is_retried_and_status_shown():
    calls = []
    def api(method, params, timeout=40):
        calls.append(method)
        if method == "setMyCommands" and calls.count("setMyCommands") == 1:
            raise urllib.error.URLError("offline")
        return {"ok": True, "result": []}
    t = d.TelegramControl("tok", "42", api=api)
    assert t.set_menu() is False and "command menu not set" in t.last_error
    assert t.set_menu() is True
    names = [c for c, _ in d.TG_COMMANDS]
    assert all(x in names for x in ("start", "stop", "status", "money", "help"))
    old = d.TG
    try:
        d.TG = None
        tok = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        st = d.telegram_status()
        assert st["on"] is False and "Settings" in st["why"]
        if tok is not None:
            os.environ["TELEGRAM_BOT_TOKEN"] = tok
        d.TG = t
        assert d.telegram_status() == {"on": True, "menu": True, "error": "command menu not set: URLError: offline"}
        assert "telegram" in d.build_status()
    finally:
        d.TG = old


def test_telegram_commands_owner_only():
    now = 1_800_000_000
    calls = []
    old = d.start_bot_with_watchdog, d.stop
    d.start_bot_with_watchdog = lambda mode: calls.append(("start", mode)) or (True, "bot started (paper) + watchdog (auto-close ON)")
    d.stop = lambda name, force=False: calls.append(("stop", name, force)) or (True, "Stop requested")
    try:
        api = FakeTelegram([msg(10, "/start", chat=999),                         # a stranger: ignored, never answered
                            msg(11, "/start", age=3600),                          # old command: not run late
                            msg(12, "/status"),
                            msg(13, "/start@Orb007_bot"),
                            msg(14, "/stop"),
                            msg(15, "hello")])
        t = d.TelegramControl("tok", "555", api=api, clock=lambda: now)
        t.poll_once(wait=0)
        assert calls == [("start", "paper"), ("stop", "bot", False)], calls
        assert t.offset == 16 and all(chat == "555" for chat, _ in api.sent)
        texts = [x for _, x in api.sent]
        assert len(texts) == 5 and "Ignored an old /start" in texts[0]
        assert texts[1].startswith("Bot: ") and "Watchdog:" in texts[1]
        assert texts[2].startswith("OK: bot started (paper)") and texts[3].startswith("OK: Stop requested")
        assert texts[4].startswith("Commands:") and "/stop" in texts[4]
        api.updates = [msg(16, "/status")]
        t.poll_once(wait=0)
        assert api.calls[-2][1]["offset"] == 16                                   # never re-reads handled messages
    finally:
        d.start_bot_with_watchdog, d.stop = old


def test_telegram_error_in_command_is_reported_not_fatal():
    old = d.status_text
    d.status_text = lambda s=None: 1 / 0
    try:
        api = FakeTelegram([msg(1, "/status")])
        t = d.TelegramControl("tok", "555", api=api, clock=lambda: 1_800_000_000)
        t.poll_once(wait=0)
        assert api.sent and api.sent[0][1].startswith("Error: ZeroDivisionError")
    finally:
        d.status_text = old


def test_status_text_reads_like_a_summary():
    now = datetime.now(timezone.utc)
    s = {"bot": {"running": True, "mode": "paper orders", "heartbeat_age": 12, "stopping": False, "stopped": None,
                 "market_open": True, "errors": 0},
         "watchdog": {"running": True}, "pnl": -12.5,
         "state": {"date": "2026-10-29", "trades": [{}], "tried": ["XOM"], "halted": False},
         "claude": {"date": "2026-10-29", "trade_today": True, "max_trades": 3, "symbols": ["XOM", "CRM"]},
         "log": ["2026-10-29 20:01:02,000 INFO STATUS 10:31 NY | trades=1 | open=XOM | day P&L -12.50 | mode=SUBMIT"]}
    t = d.status_text(s)
    assert "RUNNING (paper orders), last heartbeat 12s ago" in t and "Watchdog: running" in t
    assert "Bot day 2026-10-29: 1 trades, tried XOM" in t and "XOM, CRM" in t and "Latest: 20:01:02 STATUS 10:31 NY" in t
    s["account"] = {"ok": True, "equity": 100033.55, "day_pnl": 33.55, "positions": []}
    s["paused"] = True
    t = d.status_text(s)
    assert "PAUSED" in t and "day P&L +$33.55, equity $100,033.55, 0 open" in t


def test_live_account_and_positions_text():
    d._cache.clear()
    a = d.live_account()
    assert a["ok"] and a["day_pnl"] == 33.55 and a["positions"][0]["stop"] == 969.77 and a["positions"][0]["target"] == 1002.2
    assert "Day P&L: +$33.55" in d.pnl_text() and "Equity: $100,033.55" in d.pnl_text()
    assert d.positions_text() == "MU long 10 @ 980.68, now 984.00, +$33.20 | stop 969.77, target 1002.20"
    d._cache.clear()
    old = d.alpaca_get
    d.alpaca_get = lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("offline"))
    try:
        a = d.live_account()
        assert a["ok"] is False and "URLError" in a["error"] and "Could not reach Alpaca" in d.pnl_text()
    finally:
        d.alpaca_get = old
        d._cache.clear()


def test_money_to_date():
    d._cache.clear()
    m = d.money_to_date()
    assert m["ok"] and m["total_profit"] == 36.0 and m["total_loss"] == -26.0 and m["realized"] == 10.0
    assert (m["wins"], m["losses"], m["trades"]) == (2, 1, 3) and m["overall"] == 33.55 and m["cash"] == 90000
    assert m["first_trade"] == "2026-10-27"
    t = d.money_text()
    assert "Total profit: +$36.00 (winning trades: 2)" in t and "Total loss: -$26.00 (losing trades: 1)" in t
    assert "Overall P/L: +$33.55" in t and "Cash: $90,000.00" in t and "Equity (cash + positions): $100,033.55" in t
    assert d.handle_command("/money") == t
    flip = d.round_trips([F("X", "buy", 10, 10, 27), F("X", "sell", 15, 11, 27), F("X", "buy", 5, 12, 27)])
    assert [r["pnl"] for r in flip] == [10.0, -5.0]                                    # long +10, then short 5 -5
    d._cache.clear()
    old = d.alpaca_get
    d.alpaca_get = lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("offline"))
    try:
        assert d.money_to_date()["ok"] is False and "Could not reach Alpaca" in d.money_text()
    finally:
        d.alpaca_get = old
        d._cache.clear()


def test_scorecard_uses_evaluate():
    import shutil as sh
    sh.copy(d.Path(__file__).resolve().parent / "success_criteria.json", TMP / "success_criteria.json")
    (TMP / "config.json").write_text(json.dumps(dict(json.loads((d.Path(__file__).resolve().parent / "config.json").read_text()), strategy="orb")))
    (TMP / "config.json").write_text(json.dumps(dict(json.loads((d.Path(__file__).resolve().parent / "config.json").read_text()), strategy="orb")))
    (TMP / "journal.csv").write_text("date,symbol,status,pnl,r\n2026-10-28,MU,closed,33.55,0.31\n", encoding="utf-8")
    (TMP / "shadow.csv").write_text("date,symbol,in_shortlist,pnl,r\n" + "2026-10-28,A,yes,1,0.1\n" * 3 + "2026-10-28,B,no,-1,-0.1\n" * 5,
                                    encoding="utf-8")
    (TMP / "backtest_summary.json").write_text(json.dumps({"holdout": {"n": 410, "avg_r": -0.109}}))
    (TMP / "llm_log.jsonl").write_text("\n".join(json.dumps({"cost_usd": 0.25}) for _ in range(4)) + "\n")
    d._cache.clear()
    sc = d.scorecard()
    assert sc["ok"] and sc["verdict"] == "FAILED" and sc["paper"]["trades"] == 1 and sc["paper"]["pnl"] == 33.55
    assert sc["control"] == {"label": "Claude vs control signals", "a": 3, "b": 5, "need": 30} and sc["claude_spend_total"] == 1.0
    t = d.score_text()
    assert t.startswith("Strategy: orb | Verdict: FAILED") and "1/60 trades" in t and "FAIL Backtest on held-back days" in t
    d._cache.clear()


def test_new_telegram_commands():
    d._cache.clear()
    (TMP / "PAUSE").unlink(missing_ok=True)
    assert d.handle_command("/pause").startswith("Paused") and (TMP / "PAUSE").exists()
    assert "PAUSED" in d.status_text()
    assert d.handle_command("/resume").startswith("Resumed") and not (TMP / "PAUSE").exists()
    assert d.handle_command("/resume") == "Was not paused."
    assert d.handle_command("/pnl").startswith("Day P&L: +$33.55")
    assert "stop 969.77" in d.handle_command("/positions")
    (TMP / "bot.log").write_text("\n".join("2026-10-29 19:%02d:00,000 INFO line %d" % (i, i) for i in range(30)), encoding="utf-8")
    lg = d.handle_command("/log")
    assert lg.count("\n") == 14 and lg.endswith("19:29:00 INFO line 29")
    assert d.handle_command("/autostart off").startswith("Auto-start is OFF") and d.automation()["autostart"] is False
    assert d.handle_command("/autostart on").startswith("Auto-start is ON")
    assert "No plan" in d.handle_command("/plan") or "Shortlist" in d.handle_command("/plan")
    assert d.handle_command("/trades")
    h = d.handle_command("/nonsense")
    assert all("/" + c in h for c, _ in d.TG_COMMANDS) and len(d.TG_COMMANDS) <= 100
    d._cache.clear()


def test_autostart_rules():
    NYT = d.NY
    (TMP / d.AUTO_FILE).unlink(missing_ok=True)
    (TMP / "STOP").unlink(missing_ok=True)
    (TMP / "heartbeat.json").unlink(missing_ok=True)
    assert d.automation()["autostart"] is False                             # off unless you turn it on
    d.save_automation(autostart=True)
    starts = []
    fake_start = lambda mode: starts.append(mode) or (True, "bot started (%s)" % mode)
    old_running = d.running
    alive = {"bot": False}
    d.running = lambda name: 123 if alive.get(name) else None
    try:
        thu = lambda h, m: datetime(2026, 10, 29, h, m, tzinfo=NYT)
        assert d.auto_tick(thu(8, 30), fake_start) is None                    # before 08:45 New York
        assert d.auto_tick(datetime(2026, 10, 31, 10, 0, tzinfo=NYT), fake_start) is None   # Saturday
        (TMP / "STOP").write_text("left over from the last stop")
        assert d.auto_tick(thu(8, 46), fake_start) == "start" and starts == ["paper"]   # a leftover STOP never blocks it
        (TMP / "STOP").unlink(missing_ok=True)
        alive["bot"] = True
        assert d.auto_tick(thu(9, 0), fake_start) is None                     # running: nothing to do
        alive["bot"] = False                                                  # it died on its own...
        (TMP / "heartbeat.json").write_text(json.dumps({"time": "x", "stopped": None}))
        assert [d.auto_tick(thu(10, i), fake_start) for i in range(5)] == ["restart"] * 3 + [None, None]   # ...3 tries max
        d.save_automation(crash_restarts=0)
        (TMP / "heartbeat.json").write_text(json.dumps({"time": "x", "stopped": "user (STOP file)"}))
        assert d.auto_tick(thu(11, 0), fake_start) is None                    # it stopped on purpose
        d.save_automation(started_on=None)
        d.note_user_stop(thu(11, 5))
        assert d.auto_tick(thu(11, 6), fake_start) is None                    # you stopped it today: stays off
        assert d.automation_status(thu(11, 6))["user_stopped_today"]
        assert d.auto_tick(datetime(2026, 10, 30, 8, 50, tzinfo=NYT), fake_start) == "start"   # next day: back on
        d.save_automation(started_on=None, autostart=False)
        assert d.auto_tick(datetime(2026, 11, 2, 9, 0, tzinfo=NYT), fake_start) is None        # switched off
        d.save_automation(autostart=True, started_on=None)
        assert d.auto_tick(thu(15, 31), fake_start) is None                   # too late to bother
        bad = lambda mode: (False, "ALPACA keys missing")
        msgs = []
        assert d.auto_tick(datetime(2026, 11, 2, 9, 0, tzinfo=NYT), bad, alert=msgs.append) == "failed"
        assert msgs and "FAILED" in msgs[0] and d.automation().get("started_on") != "2026-11-02"   # retried next pass
        d.keep_awake(True)                                                    # a no-op off Windows, never raises
    finally:
        d.running = old_running
        (TMP / d.AUTO_FILE).unlink(missing_ok=True)
        (TMP / "heartbeat.json").unlink(missing_ok=True)


def test_user_stop_and_start_and_pause_routes():
    (TMP / d.AUTO_FILE).unlink(missing_ok=True)
    d.stop("bot")                                                             # a stop by you is remembered...
    assert d.automation().get("user_stopped_on") == datetime.now(d.NY).date().isoformat()
    old = d.start_bot_with_watchdog
    d.start_bot_with_watchdog = lambda mode: (True, "bot started (%s)" % mode)
    try:
        assert d.user_start("paper")[0] and d.automation().get("user_stopped_on") is None   # ...until you start it
    finally:
        d.start_bot_with_watchdog = old
    assert post("/api/pause")[1]["ok"] and (TMP / "PAUSE").exists()
    assert post("/api/resume")[1]["ok"] and not (TMP / "PAUSE").exists()
    assert post("/api/autostart", {"on": False})[1]["msg"] == "Auto-start OFF" and post("/api/autostart", {"on": True})[1]["ok"]
    st = json.loads(get("/api/status").read())
    assert {"account", "score", "paused", "automation"} <= set(st)
    (TMP / d.AUTO_FILE).unlink(missing_ok=True)


def test_nightly_learning_trigger_and_commands():
    (TMP / d.AUTO_FILE).unlink(missing_ok=True)
    runs = []
    go = lambda: runs.append(1) or (True, "started")
    thu = lambda h, m: datetime(2026, 10, 29, h, m, tzinfo=d.NY)
    assert d.learning_tick(thu(15, 0), go) is None                         # before 16:20 New York
    assert d.learning_tick(thu(16, 21), go) == "learn" and runs == [1]
    assert d.learning_tick(thu(17, 0), go) is None and runs == [1]          # once a day
    assert d.learning_tick(datetime(2026, 10, 31, 17, 0, tzinfo=d.NY), go) is None    # weekend
    d.save_automation(learn=False)
    assert d.learning_tick(datetime(2026, 10, 30, 17, 0, tzinfo=d.NY), go) is None    # switched off
    (TMP / "research").mkdir(exist_ok=True)
    assert d.handle_command("/lessons").startswith("No lessons yet")
    (TMP / "research" / "lessons.json").write_text(json.dumps([
        {"id": "L1", "text": "prefer MU", "confirmed": 3, "contradicted": 1, "status": "active"},
        {"id": "L2", "text": "old", "confirmed": 0, "contradicted": 3, "status": "retired"}]))
    t = d.handle_command("/lessons")
    assert "1 retired" in t and "L1 (+3/-1) prefer MU" in t
    real_code = d.apphome.CODE
    d.apphome.CODE = TMP                                                   # never start the REAL learning from a test
    try:
        assert "not found" in d.handle_command("/learn")                    # no research/daily.py in the test folder
    finally:
        d.apphome.CODE = real_code
    assert d.automation_status()["learn"] is False
    (TMP / d.AUTO_FILE).unlink(missing_ok=True)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    for n in list(d.procs):
        d.stop(n, force=True)
    srv.shutdown()
    print("dashboard tests passed")


def test_settings_save_test_and_import():
    import apphome
    class V:
        d = {}
        def available(self): return True
        def get(self, n): return self.d.get(n)
        def set(self, n, v): self.d[n] = v
    old_vault, saved_env = apphome.VAULT, dict(os.environ)
    apphome.VAULT = V()
    started = []
    old_restart = d.restart_telegram
    d.restart_telegram = lambda: started.append(1)
    try:
        assert d.save_keys({})[0] is False
        ok, msg = d.save_keys({"ANTHROPIC_API_KEY": "sk-new", "TELEGRAM_CHAT_ID": " 42 "})
        assert ok and "Saved 2 keys" in msg and apphome.VAULT.d == {"ANTHROPIC_API_KEY": "sk-new", "TELEGRAM_CHAT_ID": "42"}
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-new" and started == [1]          # Telegram restarted with new chat
        st = {k["name"]: k for k in d.build_status()["settings"]["keys"]}
        assert st["ANTHROPIC_API_KEY"]["set"] and "sk-new" not in json.dumps(d.build_status(), default=str)
        # tests against the services, with a fake network
        seen = []
        def http(req, timeout=15, context=None):
            seen.append((req.full_url.split("/bot")[0], dict(req.header_items())))
            if "alpaca" in req.full_url:
                return {"account_number": "PA0TESTACCT", "equity": "100033.55"}
            if "getMe" in req.full_url:
                return {"result": {"username": "Orb007_bot"}}
            return {"ok": True}
        ok, msg = d.test_keys("alpaca", {"ALPACA_API_KEY": "PKX", "ALPACA_SECRET_KEY": "S"}, http)
        assert ok and "PA0TESTACCT" in msg and seen[-1][1]["Apca-api-key-id"] == "PKX"
        assert d.test_keys("claude", {}, http) == (True, "Claude OK: the key is accepted.")    # uses the saved key
        ok, msg = d.test_keys("telegram", {"TELEGRAM_BOT_TOKEN": "123456:" + "A" * 30}, http)
        assert ok and "@Orb007_bot" in msg
        live = lambda req, timeout=15, context=None: {"account_number": "123", "equity": "1"}
        assert "not for a PAPER account" in d.test_keys("alpaca", {"ALPACA_API_KEY": "AK", "ALPACA_SECRET_KEY": "S"}, live)[1]
        def refuse(req, timeout=15, context=None):
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)
        ok, msg = d.test_keys("alpaca", {"ALPACA_API_KEY": "PK", "ALPACA_SECRET_KEY": "S"}, refuse)
        assert not ok and "HTTP 401" in msg and "PK)" in msg
        def boom(req, timeout=15, context=None):
            raise urllib.error.URLError("https://api.telegram.org/bot123456:%s/getMe down" % ("B" * 30))
        ok, msg = d.test_keys("telegram", {"TELEGRAM_BOT_TOKEN": "123456:" + "B" * 30, "TELEGRAM_CHAT_ID": "1"}, boom)
        assert not ok and "B" * 30 not in msg                                                    # token never echoed
        # import (refused while the bot runs)
        src = Path(tempfile.mkdtemp())
        (src / "journal.csv").write_text("date\n")
        (src / "orb_bot.py").write_text("code")
        ok, msg = d.import_history(str(src))
        assert ok and "Imported 1 files" in msg and (TMP / "journal.csv").exists() and not (TMP / "orb_bot.py").exists()
        (TMP / "journal.csv").unlink()
        old_running = d.running
        d.running = lambda name: 123
        try:
            assert d.import_history(str(src)) == (False, "Stop the bot first, then import.")
        finally:
            d.running = old_running
        code, r = post("/api/keys/test", {"which": "nope"})
        assert code == 200 and r == {"ok": False, "msg": "Unknown test."}
        assert post("/api/keys/save", {"values": {}}, token="wrong")[0] == 403
    finally:
        apphome.VAULT = old_vault
        d.restart_telegram = old_restart
        os.environ.clear()
        os.environ.update(saved_env)
