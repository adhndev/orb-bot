"""
watchdog.py - a SECOND program that notices if the trading bot goes quiet while the market is open.

    python watchdog.py              # alerts only
    python watchdog.py --flatten    # alerts, and closes ALL positions if the bot stays silent too long

Run it in its own terminal window next to the bot. It reads heartbeat.json (the bot rewrites it every
loop) and asks Alpaca whether the market is open.

  * Heartbeat older than stale_seconds   -> Telegram alert ("bot silent, N positions open").
  * Silent longer than flatten_after_seconds and --flatten given -> cancels orders and closes all positions.
  * You stopped the bot yourself (Ctrl-C)  -> one alert, never an automatic close: you chose to stop.
  * The bot comes back                    -> an "all clear" alert.

HONEST LIMITS: this catches a crashed or closed bot window. It runs on the same computer, so if the
whole PC sleeps, loses power or loses internet, the watchdog stops too (the stops and targets already
at Alpaca still protect you until the close, then they expire). For protection from that, run
the watchdog on a second device.
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import notify

import apphome
HERE = apphome.HOME                         # data folder (the project folder, or Documents\OrbBot in the app)
HEARTBEAT = HERE / "heartbeat.json"
STATUS_FILE = HERE / "watchdog.json"   # the dashboard reads this to know a watchdog is already running


def write_status(flatten, path=None):
    path = path or STATUS_FILE
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "time": datetime.now(timezone.utc).isoformat(), "flatten": flatten}))
        tmp.replace(path)
    except Exception as e:
        log.warning("watchdog.json not written: %s", e)
log = logging.getLogger("watchdog")


def assess(hb, now, market_open, n_positions, cfg, flatten, state):
    """Pure decision logic. hb: heartbeat dict or None. now: aware datetime. Returns a list of
    ("alert", text, level) and ("flatten", reason) actions, and updates `state`."""
    acts, w = [], cfg
    if not market_open:
        state.clear()
        return acts
    age = None
    if hb:
        try:
            age = (now - datetime.fromisoformat(hb["time"])).total_seconds()
        except Exception:
            age = None
    if hb and hb.get("stopped") and age is not None and age < 6 * 3600:
        if not state.get("stop_alerted"):
            state["stop_alerted"] = True
            acts.append(("alert", "The bot was stopped by you (%s). %d positions are open with their stops at Alpaca; "
                         "those stops expire at today's close." % (hb["stopped"], n_positions), "warn"))
        return acts
    state.pop("stop_alerted", None)
    stale = age is None or age > w["stale_seconds"]
    if not stale:
        if state.pop("incident", None):
            state.clear()
            acts.append(("alert", "All clear: the bot is running again.", "info"))
        return acts
    since = state.setdefault("first_seen", now.timestamp())
    silent_for = max(age or 0, now.timestamp() - since)
    what = "no heartbeat file" if age is None else "silent for %d minutes" % (age // 60)
    if not state.get("incident"):
        state["incident"] = True
        acts.append(("alert", "The trading bot is not running (%s) while the market is open. %d positions are open. "
                     "%s" % (what, n_positions, "I will close them if it stays silent." if flatten and n_positions else
                             "Restart it, or close positions by hand."), "critical"))
    if silent_for >= w["flatten_after_seconds"] and n_positions > 0:
        if flatten and not state.get("flattened"):
            state["flattened"] = True
            acts.append(("flatten", "bot silent for %d minutes" % (silent_for // 60)))
            acts.append(("alert", "Watchdog is closing ALL positions: the bot was silent for %d minutes." % (silent_for // 60), "critical"))
        elif not flatten and now.timestamp() - state.get("last_reminder", 0) > 600:
            state["last_reminder"] = now.timestamp()
            acts.append(("alert", "Still no bot, %d positions open. Restart it or close them by hand." % n_positions, "critical"))
    return acts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flatten", action="store_true", help="close all positions if the bot stays silent")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = json.loads((HERE / "config.json").read_text())
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY (paper keys).")
    import orb_bot
    from alpaca.trading.client import TradingClient
    trading = orb_bot.add_timeout(TradingClient(key, secret, paper=True))
    if not str(trading.get_account().account_number).startswith("PA"):
        sys.exit("Not a paper account. Refusing to run.")
    notifier = notify.Notifier.from_env(cfg, sync=True)
    w, state = cfg["watchdog"], {}
    log.info("Watchdog running (%s). Heartbeat file: %s", "will CLOSE positions if the bot stays silent" if a.flatten else "alerts only", HEARTBEAT)
    notifier.send("Watchdog started (%s)." % ("auto-close ON" if a.flatten else "alerts only"))
    while True:
        write_status(a.flatten)
        try:
            clock = trading.get_clock()
            try:
                hb = json.loads(HEARTBEAT.read_text())
            except Exception:
                hb = None
            n = len(trading.get_all_positions()) if clock.is_open else 0
            for act in assess(hb, datetime.now(timezone.utc), clock.is_open, n, w, a.flatten, state):
                if act[0] == "alert":
                    log.warning(act[1])
                    notifier.send(act[1], act[2])
                else:
                    log.warning("FLATTEN: %s", act[1])
                    trading.close_all_positions(cancel_orders=True)
        except KeyboardInterrupt:
            log.info("Watchdog stopped.")
            STATUS_FILE.unlink(missing_ok=True)
            return
        except Exception as e:
            log.error("watchdog error: %s", e)
        time.sleep(w["poll_seconds"])


if __name__ == "__main__":
    main()
