"""Offline tests: phone alerts, heartbeat, and the watchdog's decisions. Run: python test_notify.py"""
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
import journal
import notify
import orb_bot as b
import test_advisor as ta
import test_orb_bot as to
import watchdog

NY = b.NY


class Rec:
    configured = True

    def __init__(self):
        self.sent = []

    def send(self, text, level="info"):
        self.sent.append((level, text))
        return True

    def has(self, *words, level=None):
        return any(all(w in t for w in words) and (level is None or level == l) for l, t in self.sent)


def test_notifier():
    out, t = [], [1_000_000.0]
    n = notify.Notifier("tok", "123", max_per_hour=3, sync=True, transport=out.append, clock=lambda: t[0])
    assert n.send("hello") and "[ORB paper" in out[0] and "IST" in out[0] and "ET]" in out[0] and out[0].endswith("hello")
    assert not n.send("hello") and len(out) == 1                              # same message twice in a row is held back
    t[0] += 121
    assert n.send("hello")                                                    # fine again after two minutes
    assert n.send("two") and not n.send("three")                              # 3 per hour for ordinary alerts
    assert n.send("four", "critical") and "CRITICAL" in out[-1]               # critical ones always get through
    t[0] += 3700
    assert n.send("five") and "held back" in out[-1]                          # and you are told something was held back
    assert n.send("warned", "warn") and "WARNING" in out[-1]
    assert notify.NOOP.send("x") is False and notify.Notifier(None, None, sync=True).send("x") is False
    # a failing transport never raises and never leaks the token
    cap = logging.getLogger("notify"); records = []
    h = logging.Handler(); h.emit = lambda r: records.append(r.getMessage()); cap.addHandler(h)
    def boom(text): raise RuntimeError("https://api.telegram.org/botSECRETTOKEN/sendMessage failed")
    bad = notify.Notifier("SECRETTOKEN", "1", sync=True, transport=boom)
    assert bad.send("x") is True and records and not any("SECRETTOKEN" in r for r in records)
    cap.removeHandler(h)
    old = dict(os.environ)
    try:
        os.environ.pop("TELEGRAM_BOT_TOKEN", None); os.environ.pop("TELEGRAM_CHAT_ID", None)
        assert notify.Notifier.from_env({}) is notify.NOOP
        os.environ.update(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="9")
        assert notify.Notifier.from_env({}, sync=True).configured
        assert notify.Notifier.from_env({"notify": {"enabled": False}}) is notify.NOOP
    finally:
        os.environ.clear(); os.environ.update(old)


def test_telegram_setup_errors():
    import io
    import contextlib
    import urllib.error
    assert notify.clean_token('  "123456789:AAAA-BBBB_cccc"  ') == "123456789:AAAA-BBBB_cccc"
    good = "123456789:" + "A" * 35
    assert notify.diagnose(good) == ["The shape looks right."]
    assert any("no colon" in x for x in notify.diagnose("my_cool_bot")) and any("empty" in x for x in notify.diagnose(""))
    assert any("space" in x for x in notify.diagnose(" " + good)) and any("does not look" in x for x in notify.diagnose("123456789:short"))
    old_env, old_argv, old_api = dict(os.environ), list(notify.sys.argv), notify._api
    try:
        os.environ["TELEGRAM_BOT_TOKEN"] = good
        def deny(token, method):
            raise urllib.error.HTTPError("https://api.telegram.org/bot%s/x" % token, 401, "Unauthorized", {}, None)
        notify._api = deny
        for cmd in ("setup", "check"):
            notify.sys.argv = ["notify.py", cmd]
            try:
                notify.main(); raise AssertionError("should have exited")
            except SystemExit as e:
                msg = str(e)
            assert "rejected the token (HTTP 401)" in msg and "/token" in msg and good not in msg   # clear message, token never shown
        notify._api = lambda token, method: {"result": []}
        notify.sys.argv = ["notify.py", "setup"]
        try:
            notify.main()
        except SystemExit as e:
            assert "send it any message" in str(e)
        notify._api = lambda token, method: {"result": [{"message": {"chat": {"id": 555, "first_name": "Ada"}}}]}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            notify.main()
        assert "chat id: 555 (Ada)" in buf.getvalue()
        os.environ.pop("TELEGRAM_BOT_TOKEN"); notify.sys.argv = ["notify.py", "setup"]
        try:
            notify.main()
        except SystemExit as e:
            assert "Set the TELEGRAM_BOT_TOKEN" in str(e)
    finally:
        notify._api = old_api; notify.sys.argv[:] = old_argv; os.environ.clear(); os.environ.update(old_env)


def test_bot_events():
    bars = to.make_bars(closes_after=[100.9, 101.2, 101.25])
    now = datetime(2026, 10, 27, 9, 49, 5, tzinfo=NY)
    for submit, word in ((True, "ORDER SENT"), (False, "DRY RUN")):
        bot, tr = to.fake_bot(bars, submit=submit); bot.notifier = Rec()
        bot.tick(now)
        assert bot.notifier.has(word, "TSLA", "stop 100.50", "target"), bot.notifier.sent
    bot, tr = to.fake_bot(bars); bot.notifier = Rec()                         # a skill veto is reported
    bot.skills = NS(second_opinion=lambda o, n: {"qty": 0, "skill_qty": 0, "verdict": "BLOCK", "reasons": ["window"]})
    bot.tick(now); assert bot.notifier.has("blocked by the risk skills", "window")
    bot, tr = to.fake_bot(bars); bot.notifier = Rec()                         # positions opening and closing
    early = now.replace(minute=40)
    pos = NS(symbol="TSLA", qty="25", avg_entry_price="394.30", market_value="9800")
    tr.get_all_positions.return_value = []; bot.tick(early)
    tr.get_all_positions.return_value = [pos]; bot.tick(early.replace(second=30))
    tr.get_all_positions.return_value = []; bot.tick(early.replace(second=59))
    assert bot.notifier.has("FILLED: TSLA x25 at about 394.30") and bot.notifier.has("CLOSED: TSLA", "Day P&L")
    assert len([1 for _, t in bot.notifier.sent if t.startswith("FILLED")]) == 1
    bot, tr = to.fake_bot(bars); bot.notifier = Rec()                         # end of day, loss limit, failed close
    bot.tick(now.replace(hour=15, minute=56)); assert bot.notifier.has("Closed ALL", "confirmed flat", "flat-by", level="info")
    bot, tr = to.fake_bot(bars, pnl=-600); bot.notifier = Rec()
    bot.tick(now); assert bot.notifier.has("loss limit", level="critical")
    bot, tr = to.fake_bot(bars); bot.notifier = Rec(); tr.close_all_positions.side_effect = RuntimeError("net")
    bot.tick(now.replace(hour=15, minute=56)); assert bot.notifier.has("FAILED", "will retry", level="critical")
    bot, tr = to.fake_bot(bars, pnl=-600); bot.notifier = Rec(); bot.market_open = True
    tr.get_all_positions.return_value = [NS(symbol="X", unrealized_pl="-400", market_value="1")]
    b.RiskMonitor(bot, 3).check()
    assert bot.notifier.has("loss limit", level="critical")
    bot, tr = to.fake_bot(bars); bot.notifier = Rec(); bot.market_open = True
    tr.get_all_positions.return_value = [NS(symbol="X", unrealized_pl="-400", market_value="1")]
    tr.get_orders.return_value = []
    b.RiskMonitor(bot, 3).check(); assert bot.notifier.has("FAST MONITOR", "X is down $400", level="critical")
    bot.notifier = type("Bad", (), {"send": lambda self, *a: 1 / 0})()        # a broken notifier can never break trading
    bot.alert("x")


def test_heartbeat_and_run_loop():
    tmp = Path(tempfile.mkdtemp()); b.HEARTBEAT = tmp / "heartbeat.json"
    bars = to.make_bars()
    bot, tr = to.fake_bot(bars); bot.notifier = Rec()
    bot.write_heartbeat(); hb = json.loads(b.HEARTBEAT.read_text())
    assert hb["pid"] == os.getpid() and hb["stopped"] is None and abs((datetime.now(timezone.utc) - datetime.fromisoformat(hb["time"])).total_seconds()) < 5
    bot.write_heartbeat(stopped="user (Ctrl-C)"); assert json.loads(b.HEARTBEAT.read_text())["stopped"] == "user (Ctrl-C)"
    bot, tr = to.fake_bot(bars); bot.notifier = Rec()
    tr.get_clock.side_effect = RuntimeError("api down")
    calls, real = [], b.time.sleep
    def fake_sleep(s):
        calls.append(s)
        if sum(calls) >= 6 * bot.cfg["poll_seconds"]:     # the wait is now taken in 1-second steps, so count seconds
            raise KeyboardInterrupt
    b.time.sleep = fake_sleep
    try:
        bot.run()
    finally:
        b.time.sleep = real
    assert bot.notifier.has("Bot started") and bot.notifier.has("failed 5 times", level="critical")
    assert bot.notifier.has("stopped by you", level="warn") and json.loads(b.HEARTBEAT.read_text())["stopped"] == "user (Ctrl-C)"
    assert sum(1 for _, t in bot.notifier.sent if "failed" in t and "times in a row" in t) == 1


def test_advisor_alerts():
    rec = Rec()
    a, path, _ = ta.make(ta.FakeLLM('{"trade_today": true, "note": "chips strong", "symbols": [{"symbol": "NVDA"}]}'))
    a.notifier = rec; a.premarket(ta.NOW)
    assert rec.has("Today's plan: NVDA", "chips strong")
    rec = Rec(); a, path, _ = ta.make(ta.FakeLLM("{}")); a.notifier = rec
    a.cfg = dict(a.cfg, blackout={"dates": ["2026-10-27"], "symbols": {}}); a.premarket(ta.NOW)
    assert rec.has("No trading today", "blackout")
    rec = Rec(); a, path, _ = ta.make(ta.FakeLLM("x", fail=True)); a.notifier = rec
    for _ in range(3):
        a._call(ta.NOW, "intraday_model", "s", "u", 10, "t")
    assert rec.has("advisor halted", level="critical")


def test_end_of_day_summary():
    rows = [{"symbol": "TSLA", "qty": 25, "status": "closed", "exit_reason": "target", "pnl": "273.75", "r": "1.99"},
            {"symbol": "XOM", "qty": 54, "status": "closed", "exit_reason": "flatten", "pnl": "-37.8", "r": "-0.55"},
            {"symbol": "AMD", "qty": 0, "status": "not_filled"}]
    rec = Rec(); sent = []
    saved = journal.reconcile, journal.shadow
    journal.reconcile, journal.shadow = (lambda *a, **k: rows), (lambda *a, **k: [])
    try:
        journal.close_out(NS(submit=True, trading=None, state={}, alert=lambda t, l="info": rec.send(t, l)), datetime(2026, 10, 27, 15, 58, tzinfo=NY))
    finally:
        journal.reconcile, journal.shadow = saved                      # never leak the stand-ins into other tests
    assert rec.has("End of day: 2 trades closed, total $235.95", "TSLA x25: target", "1 open or unfilled")


def test_watchdog():
    w = {"stale_seconds": 240, "flatten_after_seconds": 600, "poll_seconds": 30}
    t0 = datetime(2026, 10, 29, 14, 0, tzinfo=timezone.utc)
    at = lambda s: t0 + timedelta(seconds=s)                                                  # the clock at a moment of the test
    hb = lambda now, age, **kw: dict({"time": (now - timedelta(seconds=age)).isoformat()}, **kw)   # a heartbeat that is `age` seconds old
    kinds = lambda acts: [x[0] for x in acts]
    st = {"a": 1}
    assert watchdog.assess(hb(t0, 10), t0, False, 2, w, True, st) == [] and st == {}          # market closed: always quiet
    assert watchdog.assess(hb(t0, 30), t0, True, 2, w, True, {}) == []                         # healthy
    st = {}                                                                                    # silent bot, alert only
    acts = watchdog.assess(hb(t0, 300), t0, True, 2, w, False, st)
    assert kinds(acts) == ["alert"] and "2 positions" in acts[0][1] and acts[0][2] == "critical"
    assert watchdog.assess(hb(at(60), 360), at(60), True, 2, w, False, st) == []               # no repeated alarms
    acts = watchdog.assess(hb(at(400), 700), at(400), True, 2, w, False, st)
    assert kinds(acts) == ["alert"] and "Still no bot" in acts[0][1]                           # a reminder once it drags on
    assert watchdog.assess(hb(at(500), 800), at(500), True, 2, w, False, st) == []             # but not again within 10 minutes
    st = {}                                                                                    # silent bot, auto-close ON
    acts = watchdog.assess(hb(t0, 300), t0, True, 2, w, True, st)
    assert kinds(acts) == ["alert"] and "will close" in acts[0][1]
    acts = watchdog.assess(hb(at(400), 700), at(400), True, 2, w, True, st)
    assert kinds(acts) == ["flatten", "alert"]
    assert watchdog.assess(hb(at(460), 760), at(460), True, 2, w, True, st) == []              # closes only once
    acts = watchdog.assess(hb(at(900), 5), at(900), True, 0, w, True, st)                      # the bot is back
    assert kinds(acts) == ["alert"] and "All clear" in acts[0][1] and st == {}
    st = {}                                                                                    # nothing to close -> never flattens
    watchdog.assess(hb(t0, 300), t0, True, 0, w, True, st)
    assert "flatten" not in kinds(watchdog.assess(hb(at(600), 900), at(600), True, 0, w, True, st))
    st = {}                                                                                    # you stopped it on purpose
    acts = watchdog.assess(hb(t0, 20, stopped="user (Ctrl-C)"), t0, True, 3, w, True, st)
    assert kinds(acts) == ["alert"] and acts[0][2] == "warn" and "3 positions" in acts[0][1]
    assert watchdog.assess(hb(t0, 900, stopped="user (Ctrl-C)"), at(900), True, 3, w, True, st) == []   # told once, never auto-closed
    st = {}                                                                                    # no heartbeat file at all
    acts = watchdog.assess(None, t0, True, 0, w, False, st)
    assert "no heartbeat file" in acts[0][1] and watchdog.assess(None, at(30), True, 0, w, False, st) == []
    assert "not running" in watchdog.assess({"time": "garbage"}, t0, True, 1, w, False, {})[0][1]


def test_last_alert_survives_exit():
    """The bot sends "stopped, positions closed" and exits at once. The sender is a background thread, so
    without flush() the process died first and the message never reached the phone."""
    import subprocess, sys, tempfile, textwrap
    from pathlib import Path
    out = Path(tempfile.mkdtemp()) / "sent.txt"
    prog = textwrap.dedent("""
        import sys, time, notify
        def slow(text):                      # a real Telegram call takes a moment
            time.sleep(0.5)
            open(sys.argv[1], "a").write(text)
        n = notify.Notifier("t", "1", transport=slow)
        n.send("Bot stopped by the STOP file.", "warn")
        if sys.argv[2] == "flush":
            assert n.flush(5)
    """)
    here = str(Path(__file__).resolve().parent)
    subprocess.run([sys.executable, "-c", prog, str(out), "noflush"], cwd=here, check=True, timeout=30)
    assert not out.exists()                                    # the bug: exit killed the message
    subprocess.run([sys.executable, "-c", prog, str(out), "flush"], cwd=here, check=True, timeout=30)
    assert "Bot stopped by the STOP file." in out.read_text()  # the fix
    assert notify.NOOP.flush() is True


if __name__ == "__main__":
    test_notifier(); test_telegram_setup_errors(); test_bot_events(); test_heartbeat_and_run_loop(); test_advisor_alerts()
    test_end_of_day_summary(); test_watchdog()
    for f in ("t_adv_state.json", "t_llm_log.jsonl", "t_inputs.json", "t_inputs.tmp", "test_state.json", "test_inputs.json"):
        (b.HERE / f).unlink(missing_ok=True)
    print("alert and watchdog tests passed")


def test_nightly_verdict_and_weekly_report():
    import shutil, tempfile
    tmp = Path(tempfile.mkdtemp())
    old = journal.HERE, journal.JOURNAL, journal.SHADOW, journal.reconcile, journal.shadow
    journal.HERE, journal.JOURNAL, journal.SHADOW = tmp, tmp / "journal.csv", tmp / "shadow.csv"
    try:
        shutil.copy(Path(__file__).resolve().parent / "success_criteria.json", tmp / "success_criteria.json")
        (tmp / "config.json").write_text(json.dumps(dict(json.loads((Path(__file__).resolve().parent / "config.json").read_text()), strategy="orb")))
        (tmp / "config.json").write_text(json.dumps(dict(json.loads((Path(__file__).resolve().parent / "config.json").read_text()), strategy="orb")))
        (tmp / "journal.csv").write_text("date,symbol,status,pnl,r\n2026-10-26,MU,closed,33.55,0.31\n"
                                         "2026-10-28,TSLA,closed,-20,-0.2\n2026-10-30,XOM,closed,10,0.1\n2026-10-23,OLD,closed,99,1\n",
                                         encoding="utf-8")
        (tmp / "llm_log.jsonl").write_text(json.dumps({"time": "2026-10-28T09:00:00-04:00", "cost_usd": 0.5}) + "\n" +
                                           json.dumps({"time": "2026-10-20T09:00:00-04:00", "cost_usd": 9}) + "\n")
        v = journal.verdict_line()
        assert "Scorecard (orb): KEEP RUNNING (4/60 paper trades)" in v, v
        w = journal.weekly_report(datetime(2026, 10, 30, 15, 58, tzinfo=NY))
        assert "Weekly report 2026-10-26 to 2026-10-30" in w and "Trades: 3 | P&L +$23.55" in w   # last week's row left out
        assert "2026-10-28: -$20.00" in w and "Claude spend this week: $0.50" in w and "Scorecard (orb):" in w
        rec = Rec()
        journal.reconcile, journal.shadow = (lambda *a, **k: []), (lambda *a, **k: [])
        bot = NS(submit=True, trading=None, state={}, alert=lambda t, l="info": rec.send(t, l))
        journal.close_out(bot, datetime(2026, 10, 29, 15, 58, tzinfo=NY))          # Thursday: no weekly report
        assert rec.has("End of day", "Scorecard (") and not rec.has("Weekly report")
        journal.close_out(bot, datetime(2026, 10, 30, 15, 58, tzinfo=NY))          # Friday: the week too
        assert rec.has("Weekly report 2026-10-26 to 2026-10-30")
    finally:
        journal.HERE, journal.JOURNAL, journal.SHADOW, journal.reconcile, journal.shadow = old


def test_telegram_uses_the_system_certificate_list_when_available():
    import ssl
    notify._TLS.clear()
    ctx = notify.tls()
    assert ctx is not None and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname   # still verifies
    notify._TLS.clear()
    real = __import__
    def no_truststore(name, *a, **k):
        if name == "truststore":
            raise ImportError
        return real(name, *a, **k)
    import builtins
    builtins.__import__ = no_truststore
    try:
        assert notify.tls() is None                                    # falls back to Python's default checking
    finally:
        builtins.__import__ = real
        notify._TLS.clear()
