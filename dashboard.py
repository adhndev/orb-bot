"""
dashboard.py - a local control panel for the bot. Standard library only.

    python dashboard.py            then open http://127.0.0.1:8765  (it opens by itself)

It shows what the bot is doing (heartbeat, live feeds, today's trades, Claude's shortlist and latest
decisions, spend, the log) and lets you start the bot (dry run or paper orders), stop it (it closes all
positions first, using the same STOP file as always) or force-kill it. It only listens on this computer.
Start/stop buttons need the random token that this page embeds, so another web page cannot press them.
The bot keeps running if you close the dashboard: it is a remote control, not part of the bot.
"""
import csv
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import notify

import apphome
HERE = apphome.HOME                         # data folder (the project folder, or Documents\OrbBot in the app)
PORT = 8765
TOKEN = secrets.token_urlsafe(24)
PIDS_FILE = "dashboard_pids.json"
procs = {}                      # name -> Popen started by this dashboard
lock = threading.Lock()
# On the Linux server, systemd runs the bot and watchdog (deploy/), so they survive reboots and crashes.
# There the buttons ask systemd instead of starting processes themselves, so there is never a second bot.
SYSTEMD = os.environ.get("ORB_SYSTEMD") == "1"
UNITS = {"bot": ("orb-bot.service", "orb-bot-dry.service"), "watchdog": ("orb-watchdog.service",)}


def systemctl(*args):
    try:
        r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return 1, str(e)


def unit_pid(unit):
    code, out = systemctl("show", "-p", "MainPID", "--value", unit)
    try:
        return int(out) or None
    except ValueError:
        return None


# ---------- helpers ----------
def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def tail(path, nbytes=60000, nlines=120):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - nbytes))
            return f.read().decode("utf-8", "replace").splitlines()[-nlines:]
    except Exception:
        return []


def pid_alive(pid):
    if not pid:
        return False
    try:
        pid = int(pid)
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)           # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return bool(ok) and code.value == 259           # STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def kill_pid(pid):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, timeout=15)
        else:
            os.kill(int(pid), 15)
    except Exception:
        pass


def saved_pids():
    return read_json(HERE / PIDS_FILE, {}) or {}


def save_pid(name, pid):
    d = saved_pids()
    if pid is None:
        d.pop(name, None)
    else:
        d[name] = pid
    (HERE / PIDS_FILE).write_text(json.dumps(d), encoding="utf-8")


def running(name):
    if SYSTEMD:
        for unit in UNITS[name]:
            pid = unit_pid(unit)
            if pid:
                return pid
        return None
    p = procs.get(name)
    if p is not None and p.poll() is None:
        return p.pid
    if pid_alive(saved_pids().get(name)):
        return saved_pids().get(name)
    return outside_pid(name)


def outside_pid(name, now=None):
    """A bot or watchdog started outside this dashboard (start_trading.bat), found through the file it keeps
    rewriting. Both the PID and a fresh time are required, so a reused PID is never mistaken for it."""
    doc = read_json(HERE / ("heartbeat.json" if name == "bot" else "watchdog.json"))
    if not doc or (name == "bot" and doc.get("stopped")):
        return None
    try:
        age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(doc["time"])).total_seconds()
    except Exception:
        return None
    return doc.get("pid") if age < 600 and pid_alive(doc.get("pid")) else None


START_CHECK_S = 4     # a bot that dies within this many seconds of Start is reported as a failed start


def last_error(path, n=3):
    """The last few lines a crashed process printed (the error is usually the very last one)."""
    try:
        lines = [l.strip() for l in Path(path).read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    except Exception:
        return ""
    return " | ".join(lines[-n:])[-500:]


def command(name, mode):
    if name == "bot":
        return apphome.script_cmd("orb_bot.py", *(["--submit"] if mode == "paper" else []))
    return apphome.script_cmd("watchdog.py", *(["--flatten"] if mode == "paper" else []))


def missing_env():
    need = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY"]
    return [k for k in need if not os.environ.get(k)]


def start(name, mode):
    with lock:
        if running(name):
            return False, "%s is already running" % name
        if name == "bot":
            if {"ALPACA_API_KEY", "ALPACA_SECRET_KEY"} & set(missing_env()):
                return False, "The Alpaca keys are not set: add them in Settings at the bottom of the dashboard"
            (HERE / "STOP").unlink(missing_ok=True)           # a leftover STOP file would stop the bot at once
        if SYSTEMD:
            if name == "bot":   # paper is enabled so it comes back after a reboot; a dry run is one-off
                unit = "orb-bot.service" if mode == "paper" else "orb-bot-dry.service"
                args = ("enable", "--now", unit) if mode == "paper" else ("start", unit)
            else:
                unit, args = "orb-watchdog.service", ("enable", "--now", "orb-watchdog.service")
            code, out = systemctl(*args)
            if code:
                return False, "could not start %s: %s" % (unit, out[:200])
            return True, "%s started (%s)" % (name, mode)
        flags = 0x08000000 if os.name == "nt" else 0           # CREATE_NO_WINDOW
        console = HERE / ("%s_console.log" % name)             # everything it prints, so a crash at start is visible
        with open(console, "w", encoding="utf-8") as out:
            p = subprocess.Popen(command(name, mode), cwd=str(HERE), stdin=subprocess.DEVNULL, stdout=out,
                                 stderr=subprocess.STDOUT, creationflags=flags,
                                 env=dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1", ORB_HOME=str(HERE)))
        procs[name] = p
        deadline = time.time() + START_CHECK_S
        while name == "bot" and time.time() < deadline and p.poll() is None:
            time.sleep(0.25)
        if p.poll() is not None:
            procs.pop(name, None)
            return False, "The %s stopped right after starting (exit code %s). %s" % (
                name, p.returncode, last_error(console) or "It printed nothing; see %s." % console.name)
        save_pid(name, p.pid)
        return True, "%s started (%s)" % (name, mode)


def start_bot_with_watchdog(mode):
    """Start the bot, and the watchdog beside it. Paper mode always gets a fresh watchdog WITH auto-close,
    so a watchdog left over in alerts-only mode (from a dry run) can never be the one guarding real paper orders."""
    ok, msg = start("bot", mode)
    if not ok:
        return ok, msg
    if SYSTEMD:   # the server's watchdog always runs with auto-close, so just make sure it is up
        if not running("watchdog"):
            start("watchdog", "paper")
        return True, msg + " + watchdog (auto-close ON)"
    wd, wdoc = running("watchdog"), read_json(HERE / "watchdog.json", {}) or {}
    if mode == "paper" and wd and wdoc.get("pid") == wd and wdoc.get("flatten"):
        return True, msg + " (watchdog already running with auto-close ON)"
    if mode == "paper" and running("watchdog"):
        stop("watchdog", force=True)
        time.sleep(0.5)
    wd_ok, _ = start("watchdog", mode)
    return True, msg + (" + watchdog (%s)" % ("auto-close ON" if mode == "paper" else "alerts only") if wd_ok
                        else " (watchdog was already running)")


def stop(name, force=False, by_user=True):
    if name == "bot" and by_user:
        note_user_stop()
    with lock:
        pid = running(name)
        if not pid:
            if name == "bot":
                (HERE / "STOP").unlink(missing_ok=True)
            return False, "%s is not running" % name
        if name == "bot" and not force:
            (HERE / "STOP").write_text("stopped from the dashboard\n", encoding="utf-8")
            if SYSTEMD:
                systemctl("disable", "orb-bot.service")   # stay stopped after a reboot too
            return True, "Stop requested: the bot will close all positions and exit (can take up to a minute)"
        if SYSTEMD:
            systemctl("disable", "--now", *UNITS[name])
            return True, "%s was force-stopped. Open positions keep their stop-loss and target at Alpaca until the close." % name
        kill_pid(pid)
        save_pid(name, None)
        return True, "%s was force-stopped. Open positions keep their stop-loss and target at Alpaca until the close." % name


# ---------- live account numbers, straight from Alpaca (paper) ----------
ALPACA_URL = "https://paper-api.alpaca.markets"     # paper only, like the bot
_cache = {}


def alpaca_get(path, timeout=10):
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
    req = urllib.request.Request(ALPACA_URL + path, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def cached(name, max_age, fn):
    """Re-use a result for max_age seconds: the page refreshes every 3 s, Alpaca does not need to."""
    hit = _cache.get(name)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    v = fn()
    _cache[name] = (time.time(), v)
    return v


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def live_account():
    """Equity, today's P&L and open positions (with their stop and target) as Alpaca sees them now."""
    def fetch():
        try:
            a = alpaca_get("/v2/account")
            ps = alpaca_get("/v2/positions")
            orders = alpaca_get("/v2/orders?status=open&limit=200&nested=true")
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": "Alpaca answered HTTP %s" % e.code}
        except Exception as e:                       # never echo request details: they would include the keys
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:120])}
        legs = {}
        for o in orders:
            for x in [o] + list(o.get("legs") or []):
                d = legs.setdefault(x.get("symbol"), {})
                if x.get("type") in ("stop", "stop_limit") and x.get("stop_price"):
                    d["stop"] = _num(x["stop_price"])
                elif x.get("type") == "limit" and x.get("side") == "sell" and x.get("limit_price"):
                    d["target"] = _num(x["limit_price"])
        pos = [{"symbol": p["symbol"], "qty": _num(p.get("qty")), "side": p.get("side", "long"),
                "entry": _num(p.get("avg_entry_price")), "price": _num(p.get("current_price")),
                "upl": _num(p.get("unrealized_pl")), **legs.get(p["symbol"], {})} for p in ps]
        return {"ok": True, "equity": _num(a.get("equity")), "day_pnl": round(_num(a.get("equity")) - _num(a.get("last_equity")), 2),
                "cash": _num(a.get("cash")), "positions": pos, "as_of": datetime.now(timezone.utc).isoformat()}
    return cached("account", 15, fetch)


def round_trips(fills):
    """Alpaca FILL activities (oldest first) -> one result per trade, a trade being flat -> position -> flat again
    in one stock (long or short). Average-cost accounting, as Alpaca uses for a position's cost basis."""
    pos, done = {}, []
    for f in fills:
        sym, qty, px = f.get("symbol"), _num(f.get("qty")), _num(f.get("price"))
        if not sym or qty <= 0:
            continue
        signed = qty if f.get("side") == "buy" else -qty
        p = pos.setdefault(sym, {"qty": 0.0, "cost": 0.0, "pnl": 0.0, "start": f.get("transaction_time")})
        if p["qty"] == 0:
            p.update(cost=0.0, pnl=0.0, start=f.get("transaction_time"))
        if p["qty"] == 0 or (p["qty"] > 0) == (signed > 0):          # opening or adding
            p["cost"] = (p["cost"] * abs(p["qty"]) + px * qty) / (abs(p["qty"]) + qty)
            p["qty"] += signed
            continue
        close = min(qty, abs(p["qty"]))                               # reducing (maybe flipping)
        p["pnl"] += (px - p["cost"]) * close * (1 if p["qty"] > 0 else -1)
        p["qty"] += signed
        if abs(p["qty"]) < 1e-9:
            p["qty"] = 0.0
            done.append({"symbol": sym, "pnl": round(p["pnl"], 2), "opened": p["start"],
                         "closed": f.get("transaction_time")})
        elif (p["qty"] > 0) == (signed > 0):                          # flipped: the rest opens a new trade
            done.append({"symbol": sym, "pnl": round(p["pnl"], 2), "opened": p["start"],
                         "closed": f.get("transaction_time")})
            p.update(cost=px, pnl=0.0, start=f.get("transaction_time"))
    return done


def all_fills(max_pages=50):
    fills, token = [], None
    for _ in range(max_pages):
        q = "/v2/account/activities/FILL?direction=asc&page_size=100" + ("&page_token=" + token if token else "")
        page = alpaca_get(q)
        if not page:
            break
        fills += page
        if len(page) < 100:
            break
        token = page[-1]["id"]
    return fills


def money_to_date():
    """Totals since the paper account began, live from Alpaca: profit from winning trades, loss from losing trades,
    overall P/L (equity now minus the starting balance, so it includes open positions) and the money in the account."""
    def fetch():
        try:
            a = alpaca_get("/v2/account")
            fills = all_fills()
            try:
                h = alpaca_get("/v2/account/portfolio/history?period=all&timeframe=1D")
                start = _num(h.get("base_value")) or None
            except Exception:
                start = None
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": "Alpaca answered HTTP %s" % e.code}
        except Exception as e:
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:120])}
        trips = round_trips(fills)
        wins = [t["pnl"] for t in trips if t["pnl"] > 0]
        losses = [t["pnl"] for t in trips if t["pnl"] < 0]
        equity = _num(a.get("equity"))
        start = start or 100000.0
        return {"ok": True, "total_profit": round(sum(wins), 2), "total_loss": round(sum(losses), 2),
                "realized": round(sum(wins) + sum(losses), 2), "wins": len(wins), "losses": len(losses),
                "trades": len(trips), "overall": round(equity - start, 2), "start": round(start, 2),
                "equity": equity, "cash": _num(a.get("cash")), "buying_power": _num(a.get("buying_power")),
                "first_trade": trips[0]["opened"][:10] if trips and trips[0].get("opened") else None,
                "as_of": datetime.now(timezone.utc).isoformat()}
    return cached("money", 60, fetch)


# ---------- scorecard: the success-criteria verdict, from evaluate.py itself ----------
def read_csv(path):
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def claude_spend_total():
    total = 0.0
    for line in tail(HERE / "llm_log.jsonl", 50_000_000, 10_000_000):
        try:
            total += float(json.loads(line).get("cost_usd") or 0)
        except Exception:
            pass
    return round(total, 2)


def scorecard():
    """The verdict for the strategy that is trading now (evaluate.current), plus its paper numbers."""
    def build():
        try:
            import evaluate
            r = evaluate.current(HERE)
        except Exception as e:
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:150])}
        c, strat = r["criteria"], r["strategy"]
        jr = read_csv(HERE / "journal.csv")
        start = c.get("start_date", "")
        closed = [x for x in jr if x.get("status") == "closed" and x.get("pnl") not in ("", None)
                  and (x.get("strategy") or "orb") == strat and x.get("date", "") >= start]
        rs = [_num(x.get("r")) for x in closed]
        if "forward" in c:
            led = [x for x in read_csv(HERE / "research" / "forward_ledger.csv")
                   if x.get("variant") == c["forward"]["variant"] and x.get("date", "") >= start]
            control = {"label": "Forward test (unfiltered rules)", "a": len(led), "b": None,
                       "need": c["forward"]["min_trades"]}
        else:
            sr = read_csv(HERE / "shadow.csv")
            control = {"label": "Claude vs control signals", "a": sum(1 for x in sr if x.get("in_shortlist") == "yes"),
                       "b": sum(1 for x in sr if x.get("in_shortlist") == "no"),
                       "need": c["claude_vs_control"]["min_signals_in_each_group"]}
        return {"ok": True, "strategy": strat, "verdict": r["verdict"], "lock_warning": r["lock_warning"],
                "checks": [{"name": n, "status": st, "detail": d} for n, st, d in r["checks"]],
                "paper": {"trades": len(closed), "days": len({x.get("date") for x in closed}),
                          "pnl": round(sum(_num(x.get("pnl")) for x in closed), 2),
                          "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
                          "win_rate": round(sum(1 for x in rs if x > 0) / len(rs), 3) if rs else None,
                          "need_trades": c["paper"]["min_trades"], "need_days": c["paper"]["min_trading_days"]},
                "control": control, "claude_spend_total": claude_spend_total()}
    return cached("score", 60, build)


# ---------- status ----------
PNL = re.compile(r"day P&L (-?[\d.]+)")
MODE = re.compile(r"mode=(\S+)")


def build_status(now=None):
    now = now or datetime.now(timezone.utc)
    hb = read_json(HERE / "heartbeat.json")
    age = None
    if hb:
        try:
            age = round((now - datetime.fromisoformat(hb["time"])).total_seconds())
        except Exception:
            pass
    pid = running("bot")
    submit = bool(hb and hb.get("submit"))
    state = read_json(HERE / ("bot_state.json" if submit or not hb else "bot_state_dryrun.json"), {}) or {}
    log_lines = tail(HERE / "bot.log")
    pnl = None
    for line in reversed(log_lines):
        m = PNL.search(line)
        if m:
            pnl = float(m.group(1))
            break
    inputs = read_json(HERE / "inputs.json", {}) or {}
    adv = read_json(HERE / "advisor_state.json", {}) or {}
    decisions = []
    for line in tail(HERE / "llm_log.jsonl", 400000, 400)[-8:]:
        try:
            d = json.loads(line)
            decisions.append({"time": d["time"][11:16], "tag": d["tag"], "model": d["model"],
                              "cost": d["cost_usd"], "text": d["response"][:260]})
        except Exception:
            pass
    return {
        "now": now.isoformat(),
        "bot": {"running": bool(pid), "pid": pid, "mode": ("paper orders" if submit else "dry run") if pid else None,
                "heartbeat_age": age, "market_open": hb.get("market_open") if hb else None,
                "stopped": hb.get("stopped") if hb else None, "errors": hb.get("errors_in_a_row") if hb else None,
                "stopping": (HERE / "STOP").exists()},
        "watchdog": {"running": bool(running("watchdog"))},
        "streams": (hb or {}).get("streams"),
        "pnl": pnl, "state": {"date": state.get("date"), "tried": state.get("tried", []), "trades": state.get("trades", []),
                              "halted": state.get("halted"), "flattened": state.get("flattened")},
        "claude": {"date": inputs.get("date"), "trade_today": inputs.get("trade_today"), "note": inputs.get("note", ""),
                   "source": inputs.get("source"), "max_trades": inputs.get("max_trades"),
                   "symbols": [s.get("symbol") if isinstance(s, dict) else s for s in inputs.get("symbols", [])],
                   "calls": adv.get("calls"), "spend": adv.get("spend_usd"), "halted": adv.get("halted"),
                   "decisions": decisions},
        "account": live_account(),
        "score": scorecard(),
        "money": money_to_date(),
        "paused": (HERE / "PAUSE").exists(),
        "automation": automation_status(),
        "missing_env": missing_env(),
        "telegram": telegram_status(),
        "settings": dict(apphome.info(), keys=apphome.key_status()),
        "log": log_lines[-80:],
    }


# ---------- automation: start each trading day, recover from a crash, keep the PC awake ----------
NY = ZoneInfo("America/New_York")
AUTO_FILE = "automation.json"
AUTO_DEFAULTS = {"autostart": False, "start_at": "08:45", "keep_awake": True, "learn": True, "learn_at": "16:20"}
CRASH_RESTARTS_PER_DAY = 3


def automation():
    return dict(AUTO_DEFAULTS, **(read_json(HERE / AUTO_FILE, {}) or {}))


def save_automation(**changes):
    a = automation()
    a.update(changes)
    (HERE / AUTO_FILE).write_text(json.dumps(a, indent=2), encoding="utf-8")
    return a


def note_user_stop(now=None):
    """A stop or kill by YOU (button or Telegram): the automation leaves the bot off for the rest of that NY day."""
    save_automation(user_stopped_on=(now or datetime.now(NY)).astimezone(NY).date().isoformat())


def _hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def automation_status(now=None):
    a = automation()
    now = (now or datetime.now(NY)).astimezone(NY)
    return {"autostart": a["autostart"], "start_at_ny": a["start_at"], "keep_awake": a["keep_awake"],
            "awake_held": bool(_awake["held"]), "last_action": a.get("last_action"),
            "learn": a["learn"], "learn_at": a["learn_at"], "last_learning": a.get("last_learning"),
            "user_stopped_today": a.get("user_stopped_on") == now.date().isoformat()}


def auto_tick(now=None, start_fn=None, alert=None):
    """One pass of the scheduler (every 30 s). Returns what it did, or None.
    * Weekdays from start_at (08:45 New York) to 15:30: if the bot is not running, start it with paper orders and the
      watchdog, once a day. The bot itself idles on holidays and blackout days.
    * If the bot dies on its own later that day (no "stopped" note in its heartbeat), restart it, up to 3 times.
    * Never after YOU stopped it that day (button or /stop): that choice holds until the next trading day."""
    now = (now or datetime.now(NY)).astimezone(NY)
    a, today = automation(), now.date().isoformat()
    start_fn = start_fn or start_bot_with_watchdog
    if not a["autostart"] or now.weekday() >= 5 or a.get("user_stopped_on") == today:
        return None
    m = now.hour * 60 + now.minute
    if not (_hhmm(a["start_at"]) <= m < _hhmm("15:30")) or running("bot"):
        return None                                  # (a STOP file with no bot running is a leftover: start() removes it)
    if a.get("started_on") != today:
        what, extra = "start", {"started_on": today, "crash_restarts": 0}
    else:
        hb = read_json(HERE / "heartbeat.json") or {}
        if hb.get("stopped") or a.get("crash_restarts", 0) >= CRASH_RESTARTS_PER_DAY:
            return None                              # it stopped on purpose, or keeps dying: leave it to you
        what, extra = "restart", {"crash_restarts": a.get("crash_restarts", 0) + 1}
    ok, msg = start_fn("paper")
    save_automation(last_action="%s %s NY: %s" % (what, now.strftime("%Y-%m-%d %H:%M"), msg), **(extra if ok else {}))
    if alert:
        alert(("Auto-%s: %s" % (what, msg)) if ok else ("Auto-%s FAILED: %s" % (what, msg)))
    return what if ok else "failed"


def user_start(mode):
    """Started by YOU (button or /start): this also lifts today's 'you stopped it' hold."""
    ok, msg = start_bot_with_watchdog(mode)
    if ok:
        save_automation(user_stopped_on=None)
    return ok, msg


def launch_learning():
    """Start research/daily.py in the background (data, forward test, Claude's review). Never blocks the dashboard."""
    if not (apphome.CODE / "research" / "daily.py").exists():
        return False, "research/daily.py not found"
    flags = 0x08000000 if os.name == "nt" else 0
    (HERE / "research").mkdir(exist_ok=True)
    with open(HERE / "research" / "daily_last.log", "w", encoding="utf-8") as out:
        subprocess.Popen(apphome.script_cmd("research/daily.py"), cwd=str(HERE), stdin=subprocess.DEVNULL, stdout=out,
                         stderr=subprocess.STDOUT, creationflags=flags, env=dict(os.environ, PYTHONUTF8="1", ORB_HOME=str(HERE)))
    return True, "Learning step started: data update, forward test, Claude's review. A Telegram summary follows."


def learning_tick(now=None, launch=None):
    """Weekdays from learn_at (16:20 New York, after the close and the journal): run the learning step once."""
    now = (now or datetime.now(NY)).astimezone(NY)
    a, today = automation(), now.date().isoformat()
    if not a["learn"] or now.weekday() >= 5 or a.get("learned_on") == today:
        return None
    if now.hour * 60 + now.minute < _hhmm(a["learn_at"]):
        return None
    ok, msg = (launch or launch_learning)()
    save_automation(learned_on=today, last_learning="%s NY: %s" % (now.strftime("%Y-%m-%d %H:%M"), msg))
    return "learn" if ok else "failed"


def lessons_text():
    try:
        ls = json.loads((HERE / "research" / "lessons.json").read_text(encoding="utf-8"))
    except Exception:
        return "No lessons yet: the first nightly review runs after the next session (16:20 New York)."
    act = sorted((l for l in ls if l.get("status") == "active"), key=lambda l: l["confirmed"] - l["contradicted"], reverse=True)
    old = sum(1 for l in ls if l.get("status") != "active")
    if not act:
        return "No active lessons (%d retired)." % old
    return "Active lessons (+confirmed / -contradicted), %d retired:\n" % old + "\n".join(
        "%s (+%d/-%d) %s" % (l["id"], l["confirmed"], l["contradicted"], l["text"]) for l in act)


_awake = {"held": False}


def keep_awake(want):
    """Windows only: ask the PC not to sleep while the bot runs (the screen may still turn off). It is a request
    held by this thread, cleared when the dashboard closes; it does not change your power settings."""
    if os.name != "nt" or want == _awake["held"]:
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if want else 0))
        _awake["held"] = want
    except Exception:
        pass


def scheduler_loop(alert=None, every=30):
    while True:
        try:
            auto_tick(alert=alert)
            learning_tick()
            keep_awake(automation()["keep_awake"] and bool(running("bot")))
        except Exception:
            pass
        time.sleep(every)


# ---------- Telegram remote control ----------
TG_COMMANDS = [
    ("status", "What the bot is doing"),
    ("pnl", "Today's P&L and equity, live from Alpaca"),
    ("money", "Profit, loss and overall P/L to date, and money in the account"),
    ("positions", "Open positions with their stop and target"),
    ("trades", "Today's trades and how they ended"),
    ("plan", "Claude's shortlist and reasoning for today"),
    ("score", "Success-criteria verdict and progress"),
    ("start", "Start the bot with PAPER orders (and the watchdog)"),
    ("stop", "Close all positions and stop the bot"),
    ("pause", "No new trades; open trades keep being managed"),
    ("resume", "Allow new trades again"),
    ("autostart", "on / off / status: daily auto-start"),
    ("lessons", "What the nightly reviews have learned"),
    ("learn", "Run the nightly learning step now"),
    ("log", "The last log lines"),
    ("help", "List the commands")]
TG_HELP = "Commands:\n" + "\n".join("/%s - %s" % c for c in TG_COMMANDS)
TG_MAX_AGE = 120            # seconds: an older command (sent while the PC was off) is ignored, not run late


def money(x):
    return "n/a" if x is None else ("-" if x < 0 else "+") + "${:,.2f}".format(abs(x))


def status_text(s=None):
    s = s or build_status()
    b, c, st, acct = s["bot"], s["claude"], s["state"], s.get("account") or {}
    if b["running"]:
        bot = "RUNNING (%s)" % b["mode"]
        if b["heartbeat_age"] is not None:
            bot += ", last heartbeat %ss ago" % b["heartbeat_age"]
        if b["stopping"]:
            bot += ", STOPPING (closing positions)"
    else:
        bot = "STOPPED" + (" (%s)" % b["stopped"] if b["stopped"] else "")
    lines = ["Bot: " + bot + (" | PAUSED: no new trades" if s.get("paused") else ""),
             "Watchdog: " + ("running" if s["watchdog"]["running"] else "NOT running"),
             "Market: " + ("open" if b["market_open"] else "closed")]
    if acct.get("ok"):
        lines.append("Account (live): day P&L %s, equity ${:,.2f}, %d open".format(acct["equity"]) % (
            money(acct["day_pnl"]), len(acct["positions"])))
    if st.get("date"):
        lines.append("Bot day %s: %d trades%s" % (st["date"], len(st.get("trades") or []),
                     (", tried " + ", ".join(st["tried"])) if st.get("tried") else ""))
        if st.get("halted"):
            lines.append("HALTED for the day (daily loss limit)")
    if c.get("date"):
        lines.append("Claude (%s): %s, max %s trades. %s" % (c["date"], "trading" if c["trade_today"] else "NOT trading",
                     c.get("max_trades"), ", ".join(c.get("symbols") or []) or "no shortlist"))
    last = [l for l in s["log"] if "STATUS" in l or "Market closed" in l]
    if last:
        lines.append("Latest: " + last[-1][11:19] + " " + last[-1].split(" INFO ", 1)[-1][:300])
    au = s.get("automation") or {}
    if au:
        lines.append("Auto-start: %s%s" % ("ON at %s NY" % au["start_at_ny"] if au["autostart"] else "OFF",
                     " (off today: you stopped it)" if au.get("user_stopped_today") and au["autostart"] else ""))
    if b["errors"]:
        lines.append("Errors in a row: %s" % b["errors"])
    return "\n".join(lines)


def pnl_text():
    a = live_account()
    if not a.get("ok"):
        return "Could not reach Alpaca: %s" % a.get("error")
    upl = sum(p["upl"] for p in a["positions"])
    return "Day P&L: %s\nEquity: ${:,.2f}\nOpen: %d position(s), unrealized %s".format(a["equity"]) % (
        money(a["day_pnl"]), len(a["positions"]), money(upl))


def money_text():
    m = money_to_date()
    if not m.get("ok"):
        return "Could not reach Alpaca: %s" % m.get("error")
    return ("Money to date (paper account)\n"
            "Total profit: %s (winning trades: %d)\nTotal loss: %s (losing trades: %d)\n"
            "Closed trades net: %s\nOverall P/L: %s (equity now vs ${:,.2f} start, open positions included)\n"
            "Cash: ${:,.2f}\nEquity (cash + positions): ${:,.2f}\nBuying power: ${:,.2f}").format(
        m["start"], m["cash"], m["equity"], m["buying_power"]) % (
        money(m["total_profit"]), m["wins"], money(m["total_loss"]), m["losses"], money(m["realized"]),
        money(m["overall"]))


def positions_text():
    a = live_account()
    if not a.get("ok"):
        return "Could not reach Alpaca: %s" % a.get("error")
    if not a["positions"]:
        return "No open positions."
    out = []
    for p in a["positions"]:
        out.append("%s %s %g @ %.2f, now %.2f, %s | stop %s, target %s" % (
            p["symbol"], p["side"], p["qty"], p["entry"], p["price"], money(p["upl"]),
            "%.2f" % p["stop"] if p.get("stop") else "NONE", "%.2f" % p["target"] if p.get("target") else "none"))
    return "\n".join(out)


def trades_text():
    st = build_status()["state"]
    if not st.get("trades"):
        return "No trades today (%s)." % (st.get("date") or "no state yet")
    out = ["Trades on %s:" % st["date"]]
    for t in st["trades"]:
        x = t.get("exit")
        out.append("%s x%s entry %s stop %s target %s, max loss $%s: %s" % (
            t["symbol"], t["qty"], t.get("fill_price") or t.get("entry"), t.get("stop"), t.get("target"), t.get("max_loss"),
            ("%s %s" % (x.get("how"), money(x.get("pnl")))) if x else t.get("status", "open")))
    return "\n".join(out)


def plan_text():
    c = build_status()["claude"]
    if not c.get("date"):
        return "No plan yet today."
    return "Claude's plan for %s: %s, max %s trades\nShortlist: %s\n%s\n(%s calls, $%.3f today)" % (
        c["date"], "TRADING" if c["trade_today"] else "NOT trading", c.get("max_trades"),
        ", ".join(c.get("symbols") or []) or "none", c.get("note") or "", c.get("calls") or 0, c.get("spend") or 0)


def score_text():
    sc = scorecard()
    if not sc.get("ok"):
        return "Scorecard unavailable: %s" % sc.get("error")
    p, cv = sc["paper"], sc["control"]
    lines = ["Strategy: %s | Verdict: %s" % (sc["strategy"], sc["verdict"]),
             "Paper: %d/%d trades over %d/%d days, P&L %s, avg R %s" % (
                 p["trades"], p["need_trades"], p["days"], p["need_days"], money(p["pnl"]),
                 "n/a" if p["avg_r"] is None else "%+.3f" % p["avg_r"]),
             "%s: %d%s (need %d)" % (cv["label"], cv["a"], "" if cv["b"] is None else " / %d" % cv["b"], cv["need"]),
             "Claude spend so far: $%.2f" % sc["claude_spend_total"]]
    lines += ["%s %s: %s" % ({"PASS": "OK", "FAIL": "FAIL", "WAIT": "WAIT"}.get(c["status"], c["status"]), c["name"], c["detail"])
              for c in sc["checks"]]
    return "\n".join(lines)


def log_text(n=15):
    lines = tail(HERE / "bot.log", 20000, n)
    return "\n".join(l[11:19] + " " + l[24:][:180] for l in lines) or "(no log yet)"


def set_pause(on):
    f = HERE / "PAUSE"
    if on:
        f.write_text("paused from Telegram/dashboard\n", encoding="utf-8")
        return True, "Paused: no NEW trades. Open trades keep their stop, target and the 15:55 close. /resume to undo."
    existed = f.exists()
    f.unlink(missing_ok=True)
    return True, ("Resumed: new trades allowed again." if existed else "Was not paused.")


def handle_command(text):
    """Turn one Telegram message into a reply. Only ever called for the owner's chat."""
    words = (text or "").strip().split()
    cmd = words[0].split("@")[0].lower() if words else ""
    arg = words[1].lower() if len(words) > 1 else ""
    if cmd == "/status":
        return status_text()
    if cmd == "/start":
        ok, msg = user_start("paper")
        return ("OK: " if ok else "Not started: ") + msg
    if cmd == "/stop":
        ok, msg = stop("bot")
        note = ""
        if automation()["autostart"]:
            note = "\nAuto-start will NOT restart it today; it starts again next trading day. /autostart off to keep it off."
        return ("OK: " if ok else "") + msg + note
    if cmd == "/pnl":
        return pnl_text()
    if cmd == "/money":
        return money_text()
    if cmd == "/positions":
        return positions_text()
    if cmd == "/trades":
        return trades_text()
    if cmd == "/plan":
        return plan_text()
    if cmd == "/score":
        return score_text()
    if cmd in ("/pause", "/resume"):
        return set_pause(cmd == "/pause")[1]
    if cmd == "/log":
        return log_text()
    if cmd == "/lessons":
        return lessons_text()
    if cmd == "/learn":
        return launch_learning()[1]
    if cmd == "/autostart":
        if arg in ("on", "off"):
            save_automation(autostart=(arg == "on"))
        a = automation()
        return "Auto-start is %s (weekdays from %s New York, i.e. before Claude's 09:00 premarket call)." % (
            "ON" if a["autostart"] else "OFF", a["start_at"])
    return TG_HELP


class TelegramControl(threading.Thread):
    """Long-polls Telegram for /start, /stop and /status from YOUR chat only (TELEGRAM_CHAT_ID).
    Messages from anyone else are ignored and never answered."""

    def __init__(self, token, chat_id, api=None, clock=time.time):
        super().__init__(daemon=True)
        self.token, self.chat_id, self.clock = token, str(chat_id), clock
        self.api = api or self._http
        self.offset, self.last_error, self.menu_set, self.stop_flag = None, None, False, False

    def _http(self, method, params, timeout=40):
        req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (self.token, method),
                                     data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout, context=notify.tls()) as r:
            return json.loads(r.read().decode())

    def reply(self, text):
        self.api("sendMessage", {"chat_id": self.chat_id, "text": text[:4000]})

    def poll_once(self, wait=25):
        params = {"timeout": wait, "allowed_updates": ["message"]}
        if self.offset is not None:
            params["offset"] = self.offset
        for u in self.api("getUpdates", params).get("result", []):
            self.offset = u["update_id"] + 1
            m = u.get("message") or {}
            if str((m.get("chat") or {}).get("id")) != self.chat_id:
                continue                                        # not you: ignore silently
            text = m.get("text") or ""
            if self.clock() - m.get("date", 0) > TG_MAX_AGE:
                if text.strip().lower().startswith(("/start", "/stop")):
                    self.reply("Ignored an old %s sent while the dashboard was not running. Send it again if you "
                               "still want it." % text.strip().split()[0])
                continue
            try:
                answer = handle_command(text)
            except Exception as e:                              # never let one bad command kill the listener
                answer = "Error: %s: %s" % (type(e).__name__, str(e)[:200])
            self.reply(answer)

    def set_menu(self):
        """The "/" command list in Telegram. Retried until it works (a failure at start used to leave no menu)."""
        try:
            r = self.api("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in TG_COMMANDS]})
            self.menu_set = bool((r or {}).get("ok", True))
        except Exception as e:
            self.last_error = "command menu not set: %s" % notify.why(e)
        return self.menu_set

    def run(self):
        while not self.stop_flag:
            try:
                if not self.menu_set:
                    self.set_menu()
                self.poll_once()
                self.last_error = None
            except urllib.error.HTTPError as e:
                self.last_error = "HTTP %s" % e.code             # 409 = another dashboard is already listening
                time.sleep(30 if e.code == 409 else 10)
            except Exception as e:
                self.last_error = notify.why(e)
                time.sleep(10)


# ---------- settings: keys (saved in Windows Credential Manager, never shown back), tests, importing history ----------
def _http_json(req, timeout=15, context=None):
    with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
        return json.loads(r.read().decode())


def save_keys(values):
    try:
        saved = apphome.save_keys(values or {})
    except Exception as e:
        return False, "Could not save: %s" % e
    if not saved:
        return False, "Nothing to save: type a key into a box first."
    if {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"} & set(saved):
        restart_telegram()
    note = " The bot uses new Alpaca/Claude keys from its next start (Stop, then Start)." if running("bot") and (
        {"ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY"} & set(saved)) else ""
    where = "Windows Credential Manager" if os.name == "nt" else "the system key store"
    return True, "Saved %d key%s in %s.%s" % (len(saved), "" if len(saved) == 1 else "s", where, note)


def test_keys(which, values=None, http=None):
    """Try the keys (the ones typed in the boxes, else the saved ones) against the real service. Read-only, except a
    Telegram test message to your own chat."""
    http = http or _http_json
    v = lambda k: ((values or {}).get(k) or "").strip().strip('"').strip("'") or (os.environ.get(k) or "").strip()
    try:
        if which == "alpaca":
            key, sec = v("ALPACA_API_KEY"), v("ALPACA_SECRET_KEY")
            if not key or not sec:
                return False, "Enter the Alpaca key ID and secret first."
            acct = http(urllib.request.Request("https://paper-api.alpaca.markets/v2/account",
                                               headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}))
            if not str(acct.get("account_number", "")).startswith("PA"):
                return False, "These keys work but are not for a PAPER account. The bot only runs on paper."
            return True, "Alpaca OK: paper account %s, equity $%s." % (acct["account_number"], acct.get("equity"))
        if which == "claude":
            key = v("ANTHROPIC_API_KEY")
            if not key:
                return False, "Enter the Claude API key first."
            http(urllib.request.Request("https://api.anthropic.com/v1/models?limit=1",
                                        headers={"x-api-key": key, "anthropic-version": "2023-06-01"}))
            return True, "Claude OK: the key is accepted."
        if which == "telegram":
            token, chat = notify.clean_token(v("TELEGRAM_BOT_TOKEN")), notify.clean_token(v("TELEGRAM_CHAT_ID"))
            if not token or not chat:
                return False, "Enter the Telegram bot token and chat ID first."
            base = "https://api.telegram.org/bot%s/" % token
            me = http(urllib.request.Request(base + "getMe"), context=notify.tls()).get("result", {})
            http(urllib.request.Request(base + "sendMessage", data=json.dumps(
                {"chat_id": chat, "text": "Test from the ORB bot settings: Telegram works."}).encode(),
                headers={"Content-Type": "application/json"}), context=notify.tls())
            return True, "Telegram OK: @%s sent a test message to your chat." % me.get("username", "?")
        return False, "Unknown test."
    except urllib.error.HTTPError as e:
        hint = {"alpaca": "Use the PAPER account's keys (the key ID starts with PK), copied in full.",
                "claude": "Check the key at console.anthropic.com (API keys).",
                "telegram": "Check the token with @BotFather (/mybots), and that you have messaged your bot once."}
        return False, "%s refused (HTTP %s). %s" % (which.title(), e.code, hint.get(which, ""))
    except Exception as e:
        return False, "Could not reach %s: %s" % (which.title(), notify.why(e, v("TELEGRAM_BOT_TOKEN") or None))


def import_history(folder):
    if running("bot"):
        return False, "Stop the bot first, then import."
    try:
        n, size = apphome.import_from((folder or "").strip().strip('"'), HERE)
    except Exception as e:
        return False, str(e)
    _cache.clear()
    return True, "Imported %d files (%.1f MB) into %s." % (n, size / 1e6, HERE)


def start_telegram():
    token = notify.clean_token(os.environ.get("TELEGRAM_BOT_TOKEN"))
    chat = notify.clean_token(os.environ.get("TELEGRAM_CHAT_ID"))
    if not token or not chat:
        return None
    global TG
    t = TelegramControl(token, chat)
    t.start()
    TG = t
    return t


TG = None
NOTIFIER = notify.NOOP


def restart_telegram():
    """After new Telegram details are saved: stop the old listener and start one with the new token and chat."""
    global TG, NOTIFIER
    if TG is not None:
        TG.stop_flag = True
        TG = None
    try:
        cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    NOTIFIER = notify.Notifier.from_env(cfg, sync=True)
    return start_telegram()


def telegram_status():
    if TG is None:
        miss = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not notify.clean_token(os.environ.get(k))]
        return {"on": False, "why": ("add the Telegram token and chat ID in Settings" if miss else "not started")}
    return {"on": True, "menu": TG.menu_set, "error": TG.last_error}


# ---------- web ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "{}")
        if self.path == "/":
            return self._send(200, PAGE.replace("__TOKEN__", TOKEN), "text/html")
        if self.path == "/api/status":
            return self._send(200, json.dumps(build_status(), default=str))
        self._send(404, "{}")

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Token") != TOKEN:
            return self._send(403, json.dumps({"ok": False, "msg": "forbidden"}))
        origin = self.headers.get("Origin")
        if origin and origin.split("://")[-1].split(":")[0] not in ("127.0.0.1", "localhost"):
            return self._send(403, json.dumps({"ok": False, "msg": "forbidden"}))
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        mode = "paper" if body.get("mode") == "paper" else "dry"
        routes = {"/api/pause": lambda: set_pause(True),
                  "/api/resume": lambda: set_pause(False),
                  "/api/autostart": lambda: (True, "Auto-start %s" % ("ON" if save_automation(autostart=bool(body.get("on")))["autostart"] else "OFF")),
                  "/api/start": lambda: user_start(mode),
                  "/api/stop": lambda: stop("bot"),
                  "/api/kill": lambda: stop("bot", force=True),
                  "/api/watchdog/start": lambda: start("watchdog", mode),
                  "/api/watchdog/stop": lambda: stop("watchdog", force=True),
                  "/api/keys/save": lambda: save_keys(body.get("values")),
                  "/api/keys/test": lambda: test_keys(body.get("which"), body.get("values")),
                  "/api/import": lambda: import_history(body.get("folder")),
                  "/api/quit": lambda: (QUIT.set(), (True, "Closing the app. A running bot keeps running."))[1]}
        fn = routes.get(self.path)
        if not fn:
            return self._send(404, "{}")
        ok, msg = fn()
        self._send(200, json.dumps({"ok": ok, "msg": msg}))


def make_server(port=PORT):
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def setup():
    """Everything the dashboard runs besides the web page: keys, Telegram, the scheduler. Returns the web server."""
    global NOTIFIER
    apphome.seed_home()
    apphome.load_keys()                     # keys saved in Settings (environment variables still win)
    srv = make_server()
    url = "http://127.0.0.1:%d" % PORT
    print("Dashboard running at %s  (Ctrl+C to close it; the bot keeps running)" % url)
    print("Data folder: %s" % HERE)
    if start_telegram():
        print("Telegram commands ON: send /help to your bot for the list. Keep this window open.")
    else:
        print("Telegram commands OFF: add the Telegram token and chat ID in Settings (bottom of the dashboard).")
    try:
        cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    NOTIFIER = notify.Notifier.from_env(cfg, sync=True)
    threading.Thread(target=scheduler_loop, kwargs={"alert": lambda t: NOTIFIER.send(t, "warn")}, daemon=True).start()
    a = automation()
    print("Auto-start %s (weekdays %s New York). Keep PC awake while the bot runs: %s." % (
        "ON" if a["autostart"] else "OFF", a["start_at"], "ON" if a["keep_awake"] else "OFF"))
    return srv, url


def start_in_background():
    """For the app: the dashboard serves from a background thread while the app shows its own window."""
    srv, url = setup()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return url


QUIT = threading.Event()   # set by the app's "Close app" button (the app is waiting on it)


def main():
    srv, url = setup()
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ORB Bot</title><link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%232456d6'/%3E%3Cpath d='M12 44 L26 30 L36 38 L52 20' stroke='white' stroke-width='6' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E"><style>
:root{--bg:#f5f6f8;--card:#fff;--ink:#1b2430;--mute:#6b7685;--line:#e3e7ee;--ok:#16794c;--okbg:#e2f5ec;--warn:#9a6700;--warnbg:#fff4d6;--bad:#b42318;--badbg:#fde8e6;--acc:#2456d6}
@media(prefers-color-scheme:dark){:root{--bg:#10141a;--card:#181e27;--ink:#e8edf4;--mute:#8d98a8;--line:#273040;--ok:#4cc38a;--okbg:#12301f;--warn:#e3b341;--warnbg:#33290f;--bad:#ff7b72;--badbg:#3a1714;--acc:#6e9bff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,Segoe UI,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:16px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:14px}
h1{font-size:18px;margin:0}.sub{color:var(--mute);font-size:12px}
.pill{display:inline-block;padding:3px 10px;border-radius:99px;font-weight:600;font-size:12px}
.ok{background:var(--okbg);color:var(--ok)}.warn{background:var(--warnbg);color:var(--warn)}.bad{background:var(--badbg);color:var(--bad)}.off{background:var(--line);color:var(--mute)}
.bar{display:flex;flex-wrap:wrap;gap:8px}
button{font:inherit;font-weight:600;padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button:hover{border-color:var(--acc)}button.primary{background:var(--acc);border-color:var(--acc);color:#fff}button.danger{color:var(--bad)}button:disabled{opacity:.45;cursor:not-allowed}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.card h2{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);margin:0 0 8px}
.big{font-size:26px;font-weight:700}.mute{color:var(--mute)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);font-size:13px;white-space:nowrap}th{color:var(--mute);font-weight:600}
.scroll{overflow-x:auto}pre{margin:0;max-height:300px;overflow:auto;font:12px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
.e{color:var(--bad)}.w{color:var(--warn)}.t{color:var(--ok);font-weight:600}
.dec{border-top:1px solid var(--line);padding:8px 0;font-size:13px}.dec:first-child{border:0}
.money{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.money .lab{font-size:12px;color:var(--mute)}.money .val{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.keys{display:grid;grid-template-columns:minmax(150px,220px) minmax(160px,1fr) auto;gap:6px 10px;align-items:center}
input{font:inherit;padding:7px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink);width:100%}
.keys .pill{justify-self:start}
@media(max-width:600px){.keys{grid-template-columns:1fr}}
#msg{min-height:20px;margin-bottom:8px;font-weight:600}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
</style></head><body><div class="wrap">
<header><div><h1>ORB Bot Control <span id="mode" class="pill off">…</span></h1><div class="sub">Paper account only. Updates every 3 seconds. Closing this page does not stop the bot; closing the dashboard WINDOW stops auto-start, keep-awake and Telegram commands.</div></div>
<div class="bar"><button id="bDry">Start dry run</button><button id="bPaper" class="primary">Start paper trading</button>
<button id="bPause">Pause new trades</button><button id="bStop">Stop &amp; close positions</button><button id="bKill" class="danger">Force kill</button><button id="bQuit" style="display:none" title="Close the dashboard and Telegram commands. A running bot keeps running.">Close app</button></div></header>
<div id="msg" class="mute"></div><div id="env"></div>
<div class="grid">
<div class="card"><h2>Bot</h2><div class="big" id="state">…</div><div class="mute" id="beat"></div><div class="mute" id="wd"></div></div>
<div class="card"><h2>Day P&amp;L (account)</h2><div class="big" id="pnl">–</div><div class="mute" id="pnlsrc"></div><div class="mute" id="cnt"></div></div>
<div class="card"><h2>Live feeds</h2><div id="streams" class="mute">–</div></div>
<div class="card"><h2>Claude today</h2><div id="claude">–</div></div></div>
<div class="card" style="margin-bottom:12px"><h2>Money to date (live from Alpaca)</h2><div class="money" id="money"><div class="mute">–</div></div><div class="mute" id="moneysrc" style="margin-top:8px"></div></div>
<div class="card" style="margin-bottom:12px"><h2>Open positions (live from Alpaca)</h2><div class="scroll"><table><thead><tr><th>Stock</th><th>Side</th><th>Qty</th><th>Entry</th><th>Now</th><th>Unrealized</th><th>Stop</th><th>Target</th></tr></thead><tbody id="pos"></tbody></table></div></div>
<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(340px,1fr))">
<div class="card"><h2>Scorecard (success criteria)</h2><div id="score" class="mute">–</div></div>
<div class="card"><h2>Automation</h2><div id="auto" class="mute">–</div><div class="bar" style="margin-top:8px"><button id="bAuto">…</button></div></div></div>
<div class="card" style="margin-bottom:12px"><h2>Today's trades</h2><div class="scroll"><table><thead><tr><th>Stock</th><th>Qty</th><th>Entry</th><th>Stop</th><th>Target</th><th>Max loss</th><th>Status</th><th>Exit</th><th>Claude gate</th></tr></thead><tbody id="trades"></tbody></table></div></div>
<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(340px,1fr))">
<div class="card"><h2>Claude's latest decisions</h2><div id="dec" class="mute">–</div></div>
<div class="card"><h2>Activity log</h2><pre id="log"></pre></div></div>
<div class="bar" style="margin-top:4px"><button id="wStart">Start watchdog</button><button id="wStop">Stop watchdog</button></div>
<div class="card" id="settings" style="margin-top:12px"><h2>Settings</h2>
<div class="mute" id="setInfo"></div>
<div style="margin:10px 0 6px"><b>Keys</b> <span class="mute">Saved in Windows Credential Manager (never in a file, never shown again). Leave a box empty to keep what is saved. Use your PAPER Alpaca keys.</span></div>
<div id="keyRows" class="keys"></div>
<div class="bar" style="margin-top:8px"><button id="kSave" class="primary">Save keys</button><button id="kTA">Test Alpaca</button><button id="kTC">Test Claude</button><button id="kTT">Test Telegram</button></div>
<div id="impBox" style="margin-top:14px"><b>Bring over your history</b> <span class="mute">Copies the journal, logs, scorecard and research data from your old bot folder (program files are not copied).</span>
<div class="bar" style="margin-top:6px"><input id="impPath" placeholder="C:\Users\you\...\Orb_bot" style="flex:1;min-width:220px"><button id="impGo">Import</button></div></div>
</div>
</div><script>
const T="__TOKEN__";const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
async function post(p,b){const r=await fetch(p,{method:"POST",headers:{"X-Token":T,"Content-Type":"application/json"},body:JSON.stringify(b||{})});const j=await r.json();$("msg").textContent=j.msg||"";$("msg").style.color=j.ok?"var(--ok)":"var(--bad)";refresh()}
$("bDry").onclick=()=>post("/api/start",{mode:"dry"});
$("bPaper").onclick=()=>{if(confirm("Start the bot with PAPER orders ON (virtual money, real market data)?"))post("/api/start",{mode:"paper"})};
$("bStop").onclick=()=>{if(confirm("Close all positions and stop the bot?"))post("/api/stop")};
$("bKill").onclick=()=>{if(confirm("Force-kill the bot now? Open positions keep their broker stop and target, but nothing manages them."))post("/api/kill")};
$("bPause").onclick=()=>post(window._paused?"/api/resume":"/api/pause");
$("bAuto").onclick=()=>post("/api/autostart",{on:!window._auto});
$("wStart").onclick=()=>post("/api/watchdog/start",{mode:"paper"});$("wStop").onclick=()=>post("/api/watchdog/stop");
function pill(el,t,c){el.textContent=t;el.className="pill "+c}
const KEYS=[["ALPACA_API_KEY","Alpaca paper key ID"],["ALPACA_SECRET_KEY","Alpaca paper secret"],["ANTHROPIC_API_KEY","Claude (Anthropic) API key"],["TELEGRAM_BOT_TOKEN","Telegram bot token"],["TELEGRAM_CHAT_ID","Telegram chat ID"]];
$("keyRows").innerHTML=KEYS.map(([k,l])=>'<label for="k_'+k+'">'+l+'</label><input id="k_'+k+'" type="password" autocomplete="off" spellcheck="false" placeholder="'+(k=="TELEGRAM_CHAT_ID"?"e.g. 123456789":"paste here")+'"><span id="ks_'+k+'" class="pill off">…</span>').join("");
const typed=()=>Object.fromEntries(KEYS.map(([k])=>[k,$("k_"+k).value]));
$("kSave").onclick=async()=>{await post("/api/keys/save",{values:typed()});if($("msg").style.color.includes("ok"))KEYS.forEach(([k])=>$("k_"+k).value="")};
$("kTA").onclick=()=>post("/api/keys/test",{which:"alpaca",values:typed()});$("kTC").onclick=()=>post("/api/keys/test",{which:"claude",values:typed()});$("kTT").onclick=()=>post("/api/keys/test",{which:"telegram",values:typed()});
$("bQuit").onclick=()=>{if(confirm("Close the app? Telegram commands and auto-start stop until you open it again. A running bot keeps running."))post("/api/quit").then(()=>window.close())};
$("impGo").onclick=()=>{const f=$("impPath").value.trim();if(f&&confirm("Copy your history from\n"+f+"\ninto the app's data folder? Files with the same name are replaced."))post("/api/import",{folder:f})};
function dot(ok){return '<span class="dot" style="background:var(--'+(ok?'ok':'bad')+')"></span>'}
async function refresh(){let s;try{s=await (await fetch("/api/status")).json()}catch(e){pill($("mode"),"dashboard offline","bad");return}
const b=s.bot,age=b.heartbeat_age;
const stale=b.running&&age!=null&&age>400;
if(!b.running){pill($("mode"),"STOPPED","off");$("state").textContent="Not running"}
else if(b.stopping){pill($("mode"),"STOPPING…","warn");$("state").textContent="Closing positions, then exiting"}
else if(stale){pill($("mode"),"NOT RESPONDING","bad");$("state").textContent="Running but silent"}
else{pill($("mode"),b.mode.toUpperCase(),b.mode=="dry run"?"warn":"ok");$("state").textContent=b.market_open===false?"Running · market closed":"Running"}
$("beat").textContent=b.running?("pid "+b.pid+" · heartbeat "+(age==null?"–":age+"s ago")+(b.errors?" · "+b.errors+" errors in a row":"")):(b.stopped?"last stop: "+b.stopped:"");
$("wd").innerHTML=dot(s.watchdog.running)+"Watchdog "+(s.watchdog.running?"running":"not running");
$("bDry").disabled=$("bPaper").disabled=b.running;$("bStop").disabled=$("bKill").disabled=!b.running;
$("wStart").disabled=s.watchdog.running;$("wStop").disabled=!s.watchdog.running;
const SE=s.settings||{};$("setInfo").innerHTML='Data folder: <b>'+esc(SE.home)+'</b>'+(SE.frozen?' (the app)':' (running with Python)')+(SE.vault?'':' · <span class="w">no safe key store found: install the keyring package</span>');
(SE.keys||[]).forEach(k=>{const e=$("ks_"+k.name);if(e){e.textContent=k.set?("Set"+(k.hint?" "+k.hint:"")+" · "+k.source):"Not set";e.className="pill "+(k.set?"ok":"warn")}});
$("bQuit").style.display=SE.frozen?"":"none";
$("impBox").style.display=(SE.home&&SE.code&&SE.home!==SE.code)?"":"none";
const TGs=s.telegram||{};$("env").innerHTML=(s.missing_env.length?'<div class="pill warn" style="margin-bottom:10px">Keys missing: '+s.missing_env.join(", ")+' (add them in Settings at the bottom)</div> ':"")+(TGs.on?(TGs.error?'<div class="pill warn" style="margin-bottom:10px">Telegram: '+esc(TGs.error)+'</div>':'<div class="pill ok" style="margin-bottom:10px">Telegram commands ON'+(TGs.menu?"":" (menu pending)")+'</div>'):'<div class="pill bad" style="margin-bottom:10px">Telegram commands OFF: '+esc(TGs.why||"")+'</div>');
const A=s.account||{},live=A.ok,p=live?A.day_pnl:s.pnl;$("pnl").textContent=p==null?"–":(p<0?"-":"+")+"$"+Math.abs(p).toFixed(2);$("pnl").style.color=p==null?"":(p<0?"var(--bad)":"var(--ok)");
$("pnlsrc").textContent=live?("live from Alpaca · equity $"+A.equity.toLocaleString(undefined,{maximumFractionDigits:2})+" · "+new Date(A.as_of).toLocaleTimeString()):("from the bot log (may be old) · Alpaca: "+(A.error||"not reached"));
const M=s.money||{},usd=v=>"$"+Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}),sg=v=>(v<0?"-":v>0?"+":"")+usd(v),col=v=>v<0?"var(--bad)":v>0?"var(--ok)":"";
const tile=(l,v,c,sub)=>'<div><div class="lab">'+l+'</div><div class="val" style="color:'+(c||"")+'">'+v+'</div>'+(sub?'<div class="mute" style="font-size:12px">'+sub+'</div>':'')+'</div>';
if(M.ok){$("money").innerHTML=tile("Total profit to date",sg(M.total_profit),col(M.total_profit),M.wins+" winning trade"+(M.wins==1?"":"s"))+tile("Total loss to date",sg(M.total_loss),col(M.total_loss),M.losses+" losing trade"+(M.losses==1?"":"s"))+tile("Overall P/L",sg(M.overall),col(M.overall),"equity vs "+usd(M.start)+" start")+tile("Money in Alpaca (equity)",usd(M.equity),"","cash "+usd(M.cash)+" · buying power "+usd(M.buying_power));
$("moneysrc").textContent=M.trades+" closed trades"+(M.first_trade?" since "+M.first_trade:"")+" · closed trades net "+sg(M.realized)+" · overall P/L also counts open positions · paper account · "+new Date(M.as_of).toLocaleTimeString()}
else{$("money").innerHTML='<div class="mute">Alpaca not reachable: '+esc(M.error||"–")+'</div>';$("moneysrc").textContent=""}
const P=(live&&A.positions)||[];$("pos").innerHTML=P.length?P.map(x=>"<tr><td><b>"+esc(x.symbol)+"</b></td><td>"+esc(x.side)+"</td><td>"+x.qty+"</td><td>"+x.entry.toFixed(2)+"</td><td>"+x.price.toFixed(2)+"</td><td style='color:var(--"+(x.upl<0?"bad":"ok")+")'>"+(x.upl<0?"-":"+")+"$"+Math.abs(x.upl).toFixed(2)+"</td><td>"+(x.stop?x.stop.toFixed(2):'<span class="e">none</span>')+"</td><td>"+(x.target?x.target.toFixed(2):"–")+"</td></tr>").join(""):'<tr><td colspan="8" class="mute">'+(live?"No open positions":"Alpaca not reachable")+'</td></tr>';
window._paused=s.paused;$("bPause").textContent=s.paused?"Resume new trades":"Pause new trades";$("bPause").disabled=!b.running&&!s.paused;
if(s.paused&&b.running)$("state").textContent+=" · PAUSED (no new trades)";
const S=s.score||{};if(S.ok){const pp=S.paper,cv=S.control,cls={"MET":"ok","KEEP RUNNING":"warn","FAILED":"bad"}[S.verdict]||"off";
$("score").innerHTML='<div><span class="pill '+cls+'">'+esc(S.verdict)+'</span> <span class="mute">'+esc(S.strategy==="gap_fade"?"gap-down strategy (experiment)":"opening-range breakout")+'</span></div><div style="margin-top:6px">Paper: <b>'+pp.trades+'</b>/'+pp.need_trades+' trades · '+pp.days+'/'+pp.need_days+' days · P&L '+(pp.pnl<0?"-":"+")+"$"+Math.abs(pp.pnl).toFixed(2)+(pp.avg_r==null?"":" · avg R "+(pp.avg_r>0?"+":"")+pp.avg_r.toFixed(3))+'</div><div>'+esc(cv.label)+': '+cv.a+(cv.b==null?'':' / '+cv.b)+' (need '+cv.need+') · Claude spend $'+S.claude_spend_total.toFixed(2)+'</div>'+S.checks.map(c=>'<div class="dec"><b class="'+({"PASS":"t","FAIL":"e","WAIT":"w"}[c.status]||"")+'">'+esc(c.status)+'</b> '+esc(c.name)+' <span class="mute">'+esc(c.detail)+'</span></div>').join("")}else{$("score").textContent="Scorecard unavailable: "+(S.error||"–")}
const U=s.automation||{};window._auto=U.autostart;$("bAuto").textContent=U.autostart?"Turn auto-start OFF":"Turn auto-start ON";
$("auto").innerHTML='<div><b>Auto-start '+(U.autostart?"ON":"OFF")+'</b>'+(U.autostart?' · weekdays '+esc(U.start_at_ny)+' New York':'')+'</div><div>'+(U.user_stopped_today?'<span class="w">You stopped it today: it stays off until the next trading day (or Start).</span>':'Restarts it (up to 3 times) if it crashes during the day.')+'</div><div>Keep PC awake while the bot runs: '+(U.keep_awake?(U.awake_held?"ON (holding)":"ON"):"OFF")+'</div><div>Nightly learning: '+(U.learn?"ON at "+esc(U.learn_at)+" New York":"OFF")+'</div><div class="mute">'+esc(U.last_action||"")+(U.last_learning?'<br>'+esc(U.last_learning):"")+'</div>';
const st=s.state;$("cnt").textContent=st.trades.length+" trades · tried: "+(st.tried.join(", ")||"none")+(st.halted?" · HALTED":"")+(st.flattened?" · flattened":"");
const sm=s.streams;$("streams").innerHTML=sm?Object.entries(sm).map(([k,v])=>dot(v)+k+" "+(v?"live":"silent")).join("<br>"):"Polling every 20 s (real-time off or bot stopped)";
const c=s.claude;$("claude").innerHTML=c.date?('<div><b>'+(c.trade_today?"Trading today":"No trading today")+'</b> · max '+esc(c.max_trades)+' trades</div><div>'+esc((c.symbols||[]).join(", ")||"no shortlist")+'</div><div class="mute">'+esc(c.note)+'</div><div class="mute" style="margin-top:6px">'+(c.calls??0)+' calls · $'+Number(c.spend||0).toFixed(3)+' today'+(c.halted?' · <span class="e">HALTED</span>':'')+'</div>'):"No plan yet today";
$("trades").innerHTML=st.trades.length?st.trades.map(t=>{const g=t.claude_gate||{},x=t.exit;return "<tr><td><b>"+esc(t.symbol)+"</b></td><td>"+t.qty+"</td><td>"+(t.fill_price||t.entry)+"</td><td>"+t.stop+"</td><td>"+t.target+"</td><td>$"+t.max_loss+"</td><td>"+esc(t.status)+"</td><td>"+(x?esc(x.how)+" "+(x.pnl<0?"-":"+")+"$"+Math.abs(x.pnl):"open")+"</td><td>"+(g.decision?esc(g.decision)+" ×"+g.size_factor+" "+esc((g.reason||"").slice(0,60)):"–")+"</td></tr>"}).join(""):'<tr><td colspan="9" class="mute">No trades yet</td></tr>';
$("dec").innerHTML=c.decisions.length?c.decisions.slice().reverse().map(d=>'<div class="dec"><b>'+esc(d.time)+' '+esc(d.tag)+'</b> <span class="mute">'+esc(d.model)+' · $'+d.cost+'</span><br>'+esc(d.text)+'</div>').join(""):"–";
const lg=$("log"),atBottom=lg.scrollTop+lg.clientHeight>=lg.scrollHeight-20;
lg.innerHTML=s.log.map(l=>{const k=/ERROR|CRITICAL|failed|VETO|HALT/.test(l)?"e":/WARNING|fallback|silent/.test(l)?"w":/ORDER|FILLED|CLOSED|BUY/.test(l)?"t":"";return '<div class="'+k+'">'+esc(l)+'</div>'}).join("");if(atBottom)lg.scrollTop=lg.scrollHeight}
refresh();setInterval(refresh,3000);
</script></body></html>"""

if __name__ == "__main__":
    main()
