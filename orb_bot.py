#!/usr/bin/env python3
"""
Opening-range-breakout bot for Alpaca PAPER trading. Long only, whole shares.

Rules (all in config.json):
  * Opening range = first 15 minutes (9:30-9:45 New York).
  * Entry: a completed 1-minute bar closes above the range high, not too
    stretched, range between 0.3% and 3% of price.
  * Stop = range midpoint. Target = entry + 2 x risk. Sent as ONE bracket
    order, so Alpaca itself holds the stop and target.
  * Size = the smallest of: max loss / risk per share, position cap, max
    order, cash left after the buffer.
  * Daily guard: no entry if today's loss + open risk + new risk would pass
    the daily loss limit. If the day's P&L reaches the limit, flatten and stop.
  * Everything is closed by flat_by (15:55). Create a file named STOP next to
    this script to flatten and exit at any time.

Claude's daily input: the bot trades ONLY the stocks listed in inputs.json,
and only if that file is dated today (New York date) with "trade_today": true.
Missing, stale or invalid file = no trading. The file can tighten limits
(fewer trades, smaller max loss, an extra price trigger) but never loosen
config.json. See inputs.example.json.

It does NOT submit orders unless you pass --submit. Default is a dry run.
Keys come from environment variables ALPACA_API_KEY and ALPACA_SECRET_KEY
(paper keys only). The script refuses to run on a non-paper account.
"""
import argparse
import json
import threading
import logging
import math
import os
import queue
import sys
import time
from datetime import datetime, timedelta, timezone, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
import apphome
HERE = apphome.HOME                         # data folder (the project folder, or Documents\OrbBot in the app)
STATE_FILE = HERE / "bot_state.json"
STOP_FILE = HERE / "STOP"
STATE_HISTORY = HERE / "state_history"   # each finished day's state (paper runs), so a missed journal can be filled in
PAUSE_FILE = HERE / "PAUSE"   # exists = no NEW entries; open trades, stops, targets, flat-by and loss limits all keep working
INPUTS_FILE = HERE / "inputs.json"
HEARTBEAT = HERE / "heartbeat.json"
MAX_INPUT_SYMBOLS = 5
log = logging.getLogger("orb")
import notify


# ---------- pure logic (unit-tested, no network) ----------
def hhmm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


def opening_range(bars, cfg):
    """bars: DataFrame indexed by New York timestamps with high/low columns."""
    start = dtime(9, 30)
    end = (datetime(2000, 1, 1, 9, 30) + timedelta(minutes=cfg["or_minutes"])).time()
    w = bars[(bars.index.time >= start) & (bars.index.time < end)]
    if len(w) < cfg["min_or_bars"]:
        return None
    hi, lo = float(w["high"].max()), float(w["low"].min())
    return {"high": hi, "low": lo, "mid": (hi + lo) / 2}


def breakout_signal(price, orng, cfg, trigger=None):
    hi, lo, mid = orng["high"], orng["low"], orng["mid"]
    rng = hi - lo
    rng_pct = rng / hi * 100
    if rng_pct < cfg["min_range_pct"] or rng_pct > cfg["max_range_pct"]:
        return None
    level = max(hi, trigger) if trigger else hi
    if price <= level:
        return None
    if price - level > cfg["max_extension_of_range"] * rng:
        return None  # already ran too far
    stop = math.floor(mid * 100) / 100
    risk = price - stop
    if risk < 0.05 or risk / price * 100 < cfg["min_risk_pct"]:
        return None  # stop too tight to survive the spread
    target = round(price + cfg["reward_risk"] * risk, 2)
    return {"entry": price, "stop": stop, "target": target, "risk": risk}


def size_position(entry, risk, cfg, open_value):
    """Whole shares. Returns (qty, deciding_limit)."""
    limits = {
        "max loss": cfg["max_loss_per_trade_usd"] / risk,
        "position cap": cfg["budget"] * cfg["max_position_pct"] / 100 / entry,
        "max order": cfg["max_order_usd"] / entry,
        "cash": (cfg["budget"] * (1 - cfg["cash_buffer_pct"] / 100) - open_value) / entry,
    }
    name = min(limits, key=limits.get)
    return max(0, int(limits[name])), name


def daily_risk_ok(pnl, open_risk, new_risk, max_daily):
    return max(0.0, -pnl) + open_risk + new_risk <= max_daily


def blackout_for(cfg, day_iso):
    """(whole day blacked out?, set of blacked-out symbols) from config.json."""
    bo = cfg.get("blackout", {})
    return day_iso in bo.get("dates", []), {s for s, ds in bo.get("symbols", {}).items() if day_iso in ds}


# ---------- network safety ----------
def add_timeout(client, seconds=15):
    """alpaca-py sets no network timeout; a hung connection would freeze the bot."""
    try:
        from requests.adapters import HTTPAdapter

        class TimeoutAdapter(HTTPAdapter):
            def send(self, request, **kw):
                if kw.get("timeout") is None:
                    kw["timeout"] = seconds
                return super().send(request, **kw)

        adapter = TimeoutAdapter()
        client._session.mount("https://", adapter)
        client._session.mount("http://", adapter)
    except Exception as e:
        log.warning("could not set network timeout: %s", e)
    return client


# ---------- the bot ----------
class Bot:
    def __init__(self, cfg, trading, data, submit, advisor=None, skills=None, notifier=None, events=None, hub=None):
        self.cfg, self.trading, self.data, self.submit = cfg, trading, data, submit
        self.advisor, self.skills = advisor, skills
        self.notifier = notifier or notify.NOOP
        self._errs, self._prev_pos = 0, None
        self.events, self.hub = events, hub                       # real-time feeds (None = polling only)
        self.buffers, self._recent_entries, self._announced = {}, {}, {}
        self._last_pnl, self._last_positions, self._seen_news = 0.0, {}, set()
        self._stream_warned = False
        self.lock = threading.RLock()
        self.market_open = False
        self.close_time = None
        self.ranges = {}
        self.strategy = cfg.get("strategy", "orb")      # "orb" (opening-range breakout) or "gap_fade" (strategies.py)
        self.facts, self._facts_tried = {}, None        # gap_fade: yesterday's close and ATR per stock, once a day
        self.state = self._load_state(datetime.now(NY).date().isoformat())

    # state survives restarts, resets each new day
    def _load_state(self, today):
        if STATE_FILE.exists():
            try:
                s = json.loads(STATE_FILE.read_text())
                if s.get("date") == today:
                    return s
                self._archive(s)                  # an earlier day: keep it before starting fresh
            except Exception:
                pass
        return {"date": today, "tried": [], "trades": [], "halted": False, "flattened": False}

    def _archive(self, s):
        """Keep a finished day's state. If its end-of-day step never ran (the bot was stopped first), it is marked
        for the journal catch-up."""
        if not self.submit or not s.get("date") or not s.get("trades"):
            return
        try:
            STATE_HISTORY.mkdir(exist_ok=True)
            path = STATE_HISTORY / ("%s.json" % s["date"])
            if not path.exists():
                path.write_text(json.dumps(dict(s, journaled=bool(s.get("closed_out"))), indent=2))
        except Exception as e:
            log.warning("could not archive the state of %s: %s", s.get("date"), e)

    def catch_up_journal(self):
        """Fill in the journal for any earlier day whose end-of-day step never ran. Never raises."""
        if not self.submit:
            return
        try:
            import journal
            done = journal.catch_up(self.trading, STATE_HISTORY, datetime.now(NY).date())
            for day, rows in done:
                closed = [r for r in rows if r.get("status") == "closed"]
                log.info("JOURNAL CATCH-UP %s: %d rows from Alpaca's fills", day, len(rows))
                self.alert("Journal catch-up for %s (the bot was stopped before its end-of-day step): %d trades, "
                           "total $%.2f." % (day, len(closed), sum(float(r["pnl"]) for r in closed)))
        except Exception as e:
            log.warning("journal catch-up failed: %s", e)

    def _save(self):
        STATE_FILE.write_text(json.dumps(self.state, indent=2))

    def _new_day_check(self, now):
        if self.state["date"] != now.date().isoformat():
            self._archive(self.state)
            self.state = self._load_state(now.date().isoformat())
            self.ranges, self.buffers, self._recent_entries, self._seen_news = {}, {}, {}, set()
            self._save()
            self.catch_up_journal()

    def fetch_bars(self, symbols, now):
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed
        start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Minute,
                               start=start, feed=DataFeed.IEX)
        df = self.data.get_stock_bars(req).df
        out = {}
        if df is None or df.empty:
            return out
        have = set(df.index.get_level_values(0))
        for s in symbols:
            if s in have:
                d = df.xs(s, level=0).copy()
                d.index = d.index.tz_convert(NY)
                out[s] = d
        return out

    def alert(self, text, level="info"):
        try:
            self.notifier.send(text, level)
        except Exception as e:
            log.warning("alert failed: %s", type(e).__name__)

    def write_heartbeat(self, stopped=None):
        """A tiny file the watchdog reads, to notice if this bot goes quiet."""
        try:
            doc = {"time": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), "submit": self.submit,
                   "market_open": self.market_open, "errors_in_a_row": self._errs, "stopped": stopped,
                   "streams": self.hub.status() if self.hub else None}
            tmp = HEARTBEAT.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc))
            tmp.replace(HEARTBEAT)
        except Exception as e:
            log.warning("heartbeat not written: %s", e)

    def _watch_positions(self, positions, pnl):
        """Tell the phone when a position opens or closes (stop, target or flatten)."""
        prev = self._prev_pos
        if prev is not None:
            recent = lambda sym, k: (now_ := datetime.now(NY)) and (sym, k) in self._announced and \
                (now_ - self._announced[(sym, k)]).total_seconds() < 180
            for sym, p in positions.items():
                if sym not in prev and not recent(sym, "filled"):
                    try:
                        self.alert("FILLED: %s x%s at about %.2f" % (sym, p.qty, float(p.avg_entry_price)))
                    except Exception:
                        self.alert("FILLED: %s" % sym)
            for sym in prev:
                if sym not in positions and not recent(sym, "closed"):
                    self.alert("CLOSED: %s (stop, target or end-of-day exit). Day P&L so far $%.2f" % (sym, pnl))
        self._prev_pos = dict(positions)

    def n_trades(self):
        """Trades that count toward the daily limit (an order Alpaca rejected or cancelled does not)."""
        return sum(1 for t in self.state["trades"] if t.get("status") != "failed")

    # ----- real-time events -----
    def recent_bars(self, symbols, now):
        """Today's 1-minute bars: from the live buffers when they are fresh, otherwise from the REST API."""
        out, missing = {}, []
        for s in symbols:
            b_ = self.buffers.get(s)
            if b_ is not None and len(b_) and b_.index[-1] + timedelta(minutes=3) >= now:
                out[s] = b_
            else:
                missing.append(s)
        if missing:
            out.update(self.fetch_bars(missing, now))
        return out

    def _ingest_bar(self, sym, bar, now):
        import pandas as pd
        row = pd.DataFrame([{k: bar[k] for k in ("open", "high", "low", "close", "volume")}],
                           index=[pd.Timestamp(bar["time"]).tz_convert(NY)])
        buf = self.buffers.get(sym)
        if buf is None:  # first bar for this stock today: fill in the day so far from the REST API
            buf = self.fetch_bars([sym], now).get(sym)
            if buf is None:
                buf = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        buf = pd.concat([buf, row])
        self.buffers[sym] = buf[~buf.index.duplicated(keep="last")].sort_index()

    def _realtime_entry_ok(self, now):
        """The same entry conditions as the 20-second loop, for the event path. Returns today's inputs or None."""
        st = self.state
        if not st.get("stale_checked") or st["halted"] or st["flattened"]:
            return None
        nb, na, fb = self.times()
        if not (nb <= now.time() < na):
            return None
        inputs = self.load_inputs(now)
        if inputs is None or self.n_trades() >= inputs["max_trades"]:
            return None
        return inputs

    def handle_event(self, ev, now):
        with self.lock:
            self._new_day_check(now)
            kind = ev[0]
            if kind == "bar":
                self._on_bar(ev[1], ev[2], now)
            elif kind == "news":
                self._on_news(ev[1], now)
            elif kind == "trade":
                self._on_trade(ev[1], now)

    def _on_bar(self, sym, bar, now):
        if not self.market_open:
            return
        raw = self.load_inputs(now)
        wanted = set(raw["symbols"]) if raw else set()
        if sym not in wanted and sym not in ("SPY", "QQQ"):
            return
        self._ingest_bar(sym, bar, now)
        if sym not in wanted:
            return
        inputs = self._realtime_entry_ok(now)
        if inputs is None or sym in self.state["tried"] or sym in self._last_positions:
            return
        self.scan(now, self._last_pnl, self._last_positions, inputs, bars={sym: self.buffers[sym]}, only=sym)

    def _on_news(self, n, now):
        if n["id"] in self._seen_news:
            return
        self._seen_news.add(n["id"])
        try:
            if (now - n["time"].astimezone(NY)) > timedelta(minutes=20):
                return  # stale, not breaking
        except Exception:
            pass
        syms = [x for x in n["symbols"] if x in set(self.cfg.get("universe", []))]
        if not syms or not self.advisor:
            return
        when = now.strftime("%H:%M")
        for sym in syms:
            self.advisor.remember_headline(sym, n["headline"], when)
        raw = self.load_inputs(now)
        short = set(raw["symbols"]) if raw else set()
        for sym in syms:
            if sym in self._last_positions and self.cfg.get("realtime", {}).get("position_review", True):
                p = self._last_positions[sym]
                res = self.advisor.position_review(now, sym, n["headline"], {"qty": getattr(p, "qty", "?"),
                                                   "avg_entry": getattr(p, "avg_entry_price", "?"),
                                                   "unrealized_pl": getattr(p, "unrealized_pl", "?")})
                log.info("NEWS REVIEW %s -> %s: %s", sym, res["action"].upper(), res["reason"])
                if res["action"] == "exit":
                    self.alert("CLAUDE EXIT %s on news: \"%s\". Reason: %s" % (sym, n["headline"][:140], res["reason"]), "critical")
                    if self.close_symbol(sym):
                        for t in self.state["trades"]:
                            if t["symbol"] == sym and not t.get("exit"):
                                t["closed_by"] = "claude-news"
                        self._save()
            elif sym in short:
                self.advisor.force = True  # look at the shortlist now, not at the next 5-minute mark

    def _on_trade(self, t, now):
        st, tr = self.state, None
        label = {"stop": "stop", "limit": "target", "market": "market exit"}
        for x in st["trades"]:
            if x.get("order_id") == t["order_id"]:
                tr = x
        if t["side"] == "buy" and tr:
            if t["event"] == "fill":
                tr["status"], tr["fill_price"] = "filled", t["avg_price"] or t["price"]
                self._announced[(tr["symbol"], "filled")] = now
                self.alert("FILLED (confirmed): %s x%s at %.2f (planned %.2f)" % (tr["symbol"], tr["qty"], tr["fill_price"] or 0, tr["entry"]))
            elif t["event"] in ("canceled", "rejected", "expired"):
                tr["status"] = "failed"
                self.alert("ORDER %s: %s was NOT filled. It does not count toward today's trade limit." % (t["event"].upper(), tr["symbol"]), "warn")
        elif t["side"] == "sell" and t["event"] == "fill":
            for x in reversed(st["trades"]):
                if x["symbol"] == t["symbol"] and x.get("status") in ("filled", "submitted") and not x.get("exit"):
                    px = t["avg_price"] or t["price"] or 0.0
                    pnl = (px - (x.get("fill_price") or x["entry"])) * x["qty"]
                    x["exit"] = {"price": px, "how": label.get(t["order_type"], t["order_type"]), "pnl": round(pnl, 2)}
                    self._announced[(x["symbol"], "closed")] = now
                    self.alert("CLOSED: %s at %.2f via %s, P&L $%.2f (%.2fR)" % (
                        x["symbol"], px, x["exit"]["how"], pnl, pnl / x["max_loss"] if x.get("max_loss") else 0.0))
                    break
        self._save()

    def _idle(self, seconds):
        """Wait up to `seconds`, but act on real-time events the moment they arrive."""
        if self.events is None:
            for _ in range(max(1, int(seconds))):
                if STOP_FILE.exists():            # a STOP request cuts any wait short (it can be up to 5 minutes)
                    return
                time.sleep(1)
            return
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0 or STOP_FILE.exists():
                return
            try:
                ev = self.events.get(timeout=min(left, 1.0))
            except queue.Empty:
                continue
            try:
                self.handle_event(ev, datetime.now(NY))
            except Exception as e:
                log.error("event error (%s): %s", ev[0], e)

    def _check_streams(self, now):
        if not self.hub or not self.market_open or now.time() < dtime(9, 40):
            return
        ok = self.hub.status(self.cfg.get("realtime", {}).get("stream_stale_seconds", 150)).get("bars", True)
        if not ok and not self._stream_warned:
            self._stream_warned = True
            log.warning("Real-time bars are silent: falling back to the 20-second polling loop.")
            self.alert("Real-time price feed is silent. The bot is using slower 20-second polling instead.", "warn")
        elif ok and self._stream_warned:
            self._stream_warned = False
            log.info("Real-time bars are flowing again.")

    def times(self):
        """(no entries before, no entries after, flat by), pulled earlier on half days using Alpaca's real close time."""
        cfg = self.cfg
        nb, na, fb = hhmm(cfg["no_entries_before"]), hhmm(cfg["no_entries_after"]), hhmm(cfg["flat_by"])
        if self.close_time is not None:
            ct = self.close_time
            na = min(na, (ct - timedelta(minutes=30)).time())
            fb = min(fb, (ct - timedelta(minutes=5)).time())
        return nb, na, fb

    def close_symbol(self, sym):
        """Cancel the symbol's open orders (its stop and target), then close the position."""
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        if not self.submit:
            return True
        try:
            for o in self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[sym])):
                try:
                    self.trading.cancel_order_by_id(o.id)
                except Exception:
                    pass
            self.trading.close_position(sym)
            return True
        except Exception as e:
            log.error("close %s FAILED, will retry: %s", sym, e)
            return False

    def tick(self, now):
        with self.lock:
            return self._tick(now)

    def _final_status(self, now):
        """A status line with the day's settled P&L right after the end-of-day close, so the last
        number in the log (and on the dashboard) is the real result, not a reading from earlier."""
        try:
            acct = self.trading.get_account()
            pnl = float(acct.equity) - float(acct.last_equity)
            positions = {p.symbol: p for p in self.trading.get_all_positions()}
        except Exception as e:
            log.warning("final status not logged: %s", e)
            return
        self._hb = None
        self._heartbeat(now, pnl, positions)

    def _heartbeat(self, now, pnl, positions):
        """One status line every 15 minutes, so a quiet bot is never a mystery."""
        last = getattr(self, "_hb", None)
        if last and (now - last).total_seconds() < 900:
            return
        self._hb = now
        raw = None
        try:
            raw = json.loads(INPUTS_FILE.read_text())
        except Exception:
            pass
        shortlist = [x.get("symbol") for x in raw.get("symbols", [])] if raw else None
        st = self.state
        log.info("STATUS %s NY | shortlist=%s trade_today=%s | trades=%d tried=%s | open=%s | day P&L %.2f | mode=%s",
                 now.strftime("%H:%M"), shortlist, raw.get("trade_today") if raw else None,
                 len(st["trades"]), st["tried"], list(positions) or "none", pnl,
                 ("SUBMIT (paper orders)" if self.submit else "DRY RUN") + (" | PAUSED" if PAUSE_FILE.exists() else ""))

    def load_inputs(self, now, whole_universe=False):
        """Claude's input for today, or None (= no trading). Can only tighten limits.
        whole_universe (gap_fade): every universe stock may trade unless blocked; listed stocks keep their tighter limits."""
        cfg = self.cfg
        try:
            raw = json.loads(INPUTS_FILE.read_text())
            if raw.get("date") != now.date().isoformat() or raw.get("trade_today") is not True:
                return None
            all_day, bsyms = blackout_for(cfg, now.date().isoformat())
            if all_day:
                return None
            syms = {}
            if whole_universe:
                for sym in cfg["universe"]:
                    if sym not in cfg.get("blocked", []) and sym not in bsyms:
                        syms[sym] = {"trigger": None, "max_loss": cfg["max_loss_per_trade_usd"]}
            for item in raw["symbols"][:MAX_INPUT_SYMBOLS]:
                sym = str(item["symbol"]).upper()
                if not sym.replace(".", "").isalpha() or len(sym) > 6 or sym in cfg.get("blocked", []) or sym in bsyms:
                    continue
                trig = item.get("trigger")
                loss = item.get("max_loss_usd", cfg["max_loss_per_trade_usd"])
                syms[sym] = {"trigger": float(trig) if trig else None,
                             "max_loss": min(float(loss), cfg["max_loss_per_trade_usd"])}
            if not syms:
                return None
            return {"symbols": syms, "note": str(raw.get("note", ""))[:300], "source": str(raw.get("source", ""))[:40],
                    "max_trades": min(int(raw.get("max_trades", cfg["max_trades_per_day"])),
                                      cfg["max_trades_per_day"])}
        except Exception as e:
            if not getattr(self, "_warned_inputs", False):
                log.warning("inputs.json missing or invalid (%s): not trading", e)
                self._warned_inputs = True
            return None

    def flatten(self, reason):
        """Close everything. Returns True only if Alpaca accepted it, so a failure is retried."""
        log.warning("FLATTEN (%s)", reason)
        if not self.submit:
            log.warning("dry run: would close all positions and cancel orders")
            return True
        try:
            res = self.trading.close_all_positions(cancel_orders=True)
        except Exception as e:
            log.error("flatten FAILED, will retry next loop: %s", e)
            self.alert("Closing positions FAILED (will retry): %s. Reason was: %s" % (type(e).__name__, reason), "critical")
            return False
        # Alpaca answers per symbol INSIDE a successful reply: a refused close (for example while the bracket's
        # stop and target are still being cancelled) does not raise. So read the replies, then check the account.
        for r in res if isinstance(res, list) else []:
            code = getattr(r, "status", None)
            if code is not None and not 200 <= int(code) < 300:
                log.warning("Alpaca did not close %s (HTTP %s): %s", getattr(r, "symbol", "?"), code,
                            str(getattr(r, "body", ""))[:200])
        left = self._still_open()
        if left:                                   # second try, symbol by symbol, once its orders are gone
            log.warning("still open after closing all: %s. Closing them one by one.", ", ".join(left))
            for sym in left:
                self.close_symbol(sym)
            left = self._still_open()
        if left:
            log.error("flatten NOT confirmed: %s still open, will retry next loop", ", ".join(left))
            self.alert("Closing positions NOT confirmed: %s still open (its stop may be cancelled). Retrying every "
                       "loop. Reason was: %s" % (", ".join(left), reason), "critical")
            return False
        level = "critical" if "loss limit" in reason else ("info" if "flat-by" in reason else "warn")
        self.alert("Closed ALL positions and cancelled orders (confirmed flat). Reason: %s" % reason, level)
        return True

    def _still_open(self, wait=None):
        """Symbols still held, after giving the close orders `wait` seconds to fill."""
        wait = self.cfg.get("flatten_confirm_seconds", 15) if wait is None else wait
        deadline = time.time() + wait
        while True:
            try:
                left = sorted(p.symbol for p in self.trading.get_all_positions())
            except Exception as e:
                log.warning("could not read positions to confirm the close: %s", e)
                left = ["(unknown: positions could not be read)"]
            if not left or time.time() >= deadline:
                return left
            self._sleep(1)

    def _sleep(self, seconds):
        time.sleep(seconds)

    def enter(self, sym, sig, qty):
        from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
        req = MarketOrderRequest(
            symbol=sym, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=sig["target"]),
            stop_loss=StopLossRequest(stop_price=sig["stop"]))
        if not self.submit:
            log.info("DRY RUN, would BUY %s x%d bracket stop %.2f target %.2f", sym, qty, sig["stop"], sig["target"])
            return "dry-run"
        order = self.trading.submit_order(req)
        log.info("SUBMITTED BUY %s x%d bracket stop %.2f target %.2f id=%s", sym, qty, sig["stop"], sig["target"], order.id)
        return str(order.id)

    def _tick(self, now):
        """One pass of the logic while the market is open."""
        self._new_day_check(now)
        st, cfg, t = self.state, self.cfg, now.time()
        nb, na, fb = self.times()
        acct = self.trading.get_account()
        pnl = float(acct.equity) - float(acct.last_equity)
        positions = {p.symbol: p for p in self.trading.get_all_positions()}
        self._watch_positions(positions, pnl)
        self._last_pnl, self._last_positions = pnl, positions
        self._recent_entries = {k: v for k, v in self._recent_entries.items() if k not in positions and (now - v).total_seconds() < 90}

        if not st.get("stale_checked"):
            mine = {x["symbol"] for x in st["trades"]}
            stale = [sym for sym in positions if sym not in mine]
            if not stale:
                st["stale_checked"] = True
                self._save()
            else:
                log.warning("Open positions this run did not open (leftover from an earlier day?): %s", stale)
                if self.submit and cfg.get("flatten_stale_positions", True):
                    if self.flatten("leftover positions: " + ",".join(stale)):
                        st["stale_checked"] = True
                        self._save()
                    return
        self._heartbeat(now, pnl, positions)       # every 15 min the whole session, not just the entry window
        if not st["flattened"] and t >= fb:
            if self.flatten("flat-by time"):
                st["flattened"] = True
                self._save()
                self._final_status(now)
            return
        if (st["flattened"] and not st.get("closed_out") and not positions
                and now >= datetime.combine(now.date(), fb, tzinfo=NY) + timedelta(minutes=2)):
            st["closed_out"] = True
            self._save()
            try:
                import journal
                journal.close_out(self, now)
            except Exception as e:
                log.error("end-of-day close-out failed: %s", e)
        if st["flattened"] or st["halted"]:
            return
        if pnl <= -cfg["max_daily_loss_usd"]:
            if self.flatten("daily loss limit reached, P&L %.2f" % pnl):
                st["halted"] = True
                self._save()
            return
        if self.strategy == "gap_fade":
            return self._tick_gap(now, t, pnl, positions, na)
        if t < nb or t >= na:
            return
        inputs = self.load_inputs(now)
        if inputs is None:
            if not getattr(self, "_logged_idle", False):
                log.info("No valid inputs.json for %s: not trading today.", now.date())
                self._logged_idle = True
            return
        if self.n_trades() >= inputs["max_trades"]:
            if not getattr(self, "_logged_max", False):
                log.info("Trade limit reached today (%d of %d): not scanning for more.", self.n_trades(), inputs["max_trades"])
                self._logged_max = True
            return
        self.scan(now, pnl, positions, inputs)

    def fresh_price(self, sym):
        """Latest traded price (IEX), or None if it cannot be had. Never raises."""
        try:
            from alpaca.data.requests import StockLatestTradeRequest
            from alpaca.data.enums import DataFeed
            res = self.data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=sym, feed=DataFeed.IEX))
            p = getattr(res[sym], "price", None)
            return float(p) if isinstance(p, (int, float)) and p > 0 else None
        except Exception:
            return None

    def scan(self, now, pnl, positions, inputs, bars=None, only=None):
        st, cfg = self.state, self.cfg
        if PAUSE_FILE.exists():                   # every entry path (loop and real-time bars) comes through here
            if not getattr(self, "_logged_pause", False):
                log.info("PAUSED: no new entries until the PAUSE file is removed (/resume). Open trades are still managed.")
                self._logged_pause = True
            return
        self._logged_pause = False
        if self.strategy != "orb":
            return                                    # real-time bar events call scan(); other strategies enter elsewhere
        todo = [s for s in inputs["symbols"] if s not in st["tried"] and s not in positions and (only is None or s == only)]
        if not todo:
            return
        if bars is None:
            bars = self.fetch_bars(todo, now)
        open_value = sum(abs(float(p.market_value)) for p in positions.values())
        fresh = {s for s in self._recent_entries if s not in positions}
        for sym in todo:
            d = bars.get(sym)
            if d is None:
                continue
            d = d[d.index + timedelta(minutes=1) <= now]  # completed bars only
            if d.empty or d.index[-1] + timedelta(minutes=3) < now:
                continue  # no recent data
            if sym not in self.ranges:
                or_end = datetime.combine(now.date(), dtime(9, 30)) + timedelta(minutes=cfg["or_minutes"])
                if d.index[-1].time() < (or_end - timedelta(minutes=1)).time() and now.time() < (or_end + timedelta(minutes=3)).time():
                    continue  # the last minutes of the opening range have not arrived yet: do not lock in a partial range
                orng = opening_range(d, cfg)
                if orng is None:
                    st["tried"].append(sym)
                    log.info("%s skipped: too few opening-range bars", sym)
                    continue
                self.ranges[sym] = orng
            price = float(d["close"].iloc[-1])
            if price < cfg.get("min_price", 5):
                st["tried"].append(sym); self._save()
                continue
            sig = breakout_signal(price, self.ranges[sym], cfg, inputs["symbols"][sym]["trigger"])
            if sig is None:
                continue
            outcome, open_value = self._attempt(
                sym, sig, now, pnl, positions, inputs, fresh, open_value,
                lambda fp, sym=sym: breakout_signal(fp, self.ranges[sym], cfg, inputs["symbols"][sym]["trigger"]))
            if outcome == "halt":
                return


    # ---------------- gap-down reversion (strategies.py) ----------------
    def gap_params(self):
        import strategies
        return dict(strategies.GAP_DEFAULTS, **{k: v for k, v in self.cfg.get("gap_fade", {}).items()
                                                if not k.startswith("_") and k != "risk_skill"})

    def prepare_gap_facts(self, now, force=False):
        """Yesterday's close and the 14-session ATR for every stock, from full-market (SIP) minute bars of the last
        ~5 weeks, split-adjusted, computed exactly like the research engine. Once a day; retried every 2 minutes."""
        today = now.date().isoformat()
        if self.facts.get("date") == today:
            return True
        if not force and self._facts_tried and (now - self._facts_tried).total_seconds() < 120:
            return False
        self._facts_tried = now
        try:
            import pandas as pd
            if str(apphome.CODE / "research") not in sys.path:
                sys.path.insert(0, str(apphome.CODE / "research"))
            import lab
            import strategies
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
            from alpaca.data.enums import DataFeed
            start = datetime.combine(now.date() - timedelta(days=36), dtime(0, 0), tzinfo=NY)
            end = datetime.combine(now.date(), dtime(0, 0), tzinfo=NY)        # history only: nothing from today
            df = self.data.get_stock_bars(StockBarsRequest(symbol_or_symbols=list(self.cfg["universe"]),
                                                           timeframe=TimeFrame.Minute, start=start, end=end,
                                                           feed=DataFeed.SIP)).df
            by = {}
            for sym in self.cfg["universe"]:
                try:
                    d = df.xs(sym, level=0).copy()
                except KeyError:
                    continue
                d.index = pd.to_datetime(d.index, utc=True).tz_convert(NY)
                f = strategies.daily_facts(lab.Sym(sym, d.sort_index()), self.gap_params()["atr_days"])
                if f:
                    by[sym] = f
            if not by:
                raise RuntimeError("no history came back")
            self.facts = {"date": today, "by": by}
            log.info("GAP FACTS ready for %d stocks (yesterday's close, 14-day ATR).", len(by))
            return True
        except Exception as e:
            log.error("gap facts unavailable (%s): no gap-down trades until they load", e)
            return False

    def _tick_gap(self, now, t, pnl, positions, na):
        p = self.gap_params()
        if not (dtime(9, 31) <= t < min(hhmm(p["last_entry"]), na)):
            return
        inputs = self.load_inputs(now, whole_universe=True)
        if inputs is None:
            if not getattr(self, "_logged_idle", False):
                log.info("No valid inputs.json for %s (or trade_today is false): no gap-down trades today.", now.date())
                self._logged_idle = True
            return
        if self.n_trades() >= inputs["max_trades"]:
            return
        self.scan_gap(now, pnl, positions, inputs)

    def scan_gap(self, now, pnl, positions, inputs, bars=None):
        """At 9:31: every stock whose 9:30 bar opened 2 %+ below yesterday's close is a candidate. Biggest gaps first,
        each through the same checks as any trade (_attempt). Stops and targets come from the signal, as tested."""
        import strategies
        st, cfg, p = self.state, self.cfg, self.gap_params()
        if PAUSE_FILE.exists():
            if not getattr(self, "_logged_pause", False):
                log.info("PAUSED: no new entries until the PAUSE file is removed (/resume). Open trades are still managed.")
                self._logged_pause = True
            return
        if not self.prepare_gap_facts(now):
            return
        facts = self.facts["by"]
        todo = [s for s in inputs["symbols"] if s not in st["tried"] and s not in positions and s in facts]
        if not todo:
            return
        bars = bars if bars is not None else self.fetch_bars(todo, now)
        cands = []
        for sym in todo:
            d = bars.get(sym)
            if d is None or d.empty:
                continue
            first = d[(d.index.hour == 9) & (d.index.minute == 30)]
            if first.empty or now < first.index[0] + timedelta(minutes=1):
                continue                                           # the 9:30 bar is not complete (or not here) yet
            st["tried"].append(sym)                                # one look per stock per day, as in the backtest
            if float(first["close"].iloc[0]) < cfg.get("min_price", 5):
                continue
            sig = strategies.gap_signal(facts[sym], float(first["open"].iloc[0]), float(first["close"].iloc[0]), p)
            if sig is not None:
                cands.append((sig["gap"], sym, sig))
        self._save()
        if not cands:
            return
        cands.sort()                                               # biggest gap-down first
        log.info("GAP-DOWN candidates: %s", ", ".join("%s %.1f%%" % (s, g) for g, s, _ in cands))
        open_value = sum(abs(float(x.market_value)) for x in positions.values())
        fresh = {s for s in self._recent_entries if s not in positions}
        for gap, sym, sig in cands:
            outcome, open_value = self._attempt(sym, sig, now, pnl, positions, inputs, fresh, open_value,
                                                lambda fp, sig=sig: strategies.gap_reprice(sig, fp))
            if outcome == "halt":
                return

    def _attempt(self, sym, sig, now, pnl, positions, inputs, fresh, open_value, reprice):
        """Everything after a strategy says "buy": open-position and sector limits, sizing, the daily loss limit,
        the risk skills, Claude's gate, a re-price (reprice(fresh_price) -> new signal or None), the bracket order.
        Returns ("next" | "halt" | "entered", open_value). Shared by every strategy, so the safety checks are the same."""
        st, cfg = self.state, self.cfg
        if len(positions) + len(fresh) >= cfg["max_open_positions"]:
            return "halt", open_value
        sec = cfg.get("sectors", {}).get(sym)
        if cfg.get("one_per_sector") and sec and any(cfg.get("sectors", {}).get(o) == sec for o in list(positions) + list(fresh)):
            return "next", open_value  # already holding something in this sector; try again later
        scfg = dict(cfg, max_loss_per_trade_usd=inputs["symbols"][sym]["max_loss"])
        qty, why = size_position(sig["entry"], sig["risk"], scfg, open_value)
        if qty < 1:
            st["tried"].append(sym); self._save()
            log.info("%s signal skipped: size is zero (%s)", sym, why)
            return "next", open_value
        max_loss = qty * sig["risk"]
        open_risk = sum(x["max_loss"] for x in st["trades"] if x["symbol"] in positions or x["symbol"] in fresh)
        if not daily_risk_ok(pnl, open_risk, max_loss, cfg["max_daily_loss_usd"]):
            st["tried"].append(sym); self._save()
            log.info("%s signal skipped: would break the daily loss limit", sym)
            return "next", open_value
        gate = {}
        if self.skills:
            op = self.skills.second_opinion(
                {"symbol": sym, "qty": qty, "entry": sig["entry"], "stop": sig["stop"], "target": sig["target"],
                 "date": now.date().isoformat(), "entry_time": now.strftime("%I:%M %p"), "flat_by": cfg["flat_by"]}, now)
            if op:
                gate = {"qty_skill": op["skill_qty"], "risk_check": op["verdict"]}
                if op["verdict"] == "BLOCK" or op["qty"] < 1:
                    st["tried"].append(sym); self._save()
                    log.warning("%s signal VETOED by the skills (trader-budget / trader-risk): %s", sym, "; ".join(op["reasons"]) or "no shares")
                    self.alert("%s signal blocked by the risk skills: %s" % (sym, "; ".join(op["reasons"])[:200] or "no shares"))
                    return "next", open_value
                if op["qty"] < qty:
                    log.info("%s: trader-budget sized %d shares, smaller than the bot's %d. Using %d.", sym, op["qty"], qty, op["qty"])
                    qty = op["qty"]
                    max_loss = qty * sig["risk"]
        cg = {}
        if self.advisor and cfg.get("realtime", {}).get("gate", False):
            cg = self.advisor.entry_gate(now, sym, sig, qty, max_loss, pnl, positions, inputs)
            st.setdefault("gates", {})[sym] = cg
            if cg["decision"] != "approve":
                st["tried"].append(sym); self._save()
                log.warning("%s signal VETOED by Claude's entry gate: %s", sym, cg["reason"])
                self.alert("%s %s vetoed by Claude: %s" % (sym, "gap-down buy" if self.strategy == "gap_fade" else "breakout",
                                                       cg["reason"][:200]))
                return "next", open_value
            if cg["size_factor"] < 1.0:
                new_qty = max(1, int(qty * cg["size_factor"]))
                log.info("%s: Claude scaled size %d -> %d (%.2f): %s", sym, qty, new_qty, cg["size_factor"], cg["reason"])
                qty, max_loss = new_qty, new_qty * sig["risk"]
        # The checks above can take many seconds. Re-price now: the stop is fixed, so a price that ran up means
        # more risk per share than planned, and a price that fell back inside the range means the breakout failed.
        fp = self.fresh_price(sym)
        if fp is not None and fp != sig["entry"]:
            new = reprice(fp)
            if new is None:
                st["tried"].append(sym); self._save()
                log.warning("%s: price moved from %.2f to %.2f while the checks ran; the setup is no longer valid. Skipped.",
                            sym, sig["entry"], fp)
                return "next", open_value
            if new["risk"] > sig["risk"]:
                q2, _ = size_position(new["entry"], new["risk"], scfg, open_value)
                qty = min(qty, q2)
                if qty < 1:
                    st["tried"].append(sym); self._save()
                    log.warning("%s: after the price moved to %.2f the size is zero. Skipped.", sym, fp)
                    return "next", open_value
            log.info("%s: re-priced %.2f -> %.2f before sending the order", sym, sig["entry"], fp)
            sig = new
            max_loss = qty * sig["risk"]
            if not daily_risk_ok(pnl, open_risk, max_loss, cfg["max_daily_loss_usd"]):
                st["tried"].append(sym); self._save()
                log.info("%s signal skipped: re-priced order would break the daily loss limit", sym)
                return "next", open_value
        try:
            oid = self.enter(sym, sig, qty)
        except Exception as e:
            st["tried"].append(sym); self._save()
            log.error("order for %s failed: %s", sym, e)
            return "next", open_value
        st["tried"].append(sym)
        self.alert("%s: BUY %s x%d | stop %.2f | target %.2f | max loss $%.0f | %s" % (
            "ORDER SENT" if self.submit else "DRY RUN (nothing sent)", sym, qty, sig["stop"], sig["target"], max_loss,
            (inputs.get("note") or "")[:150]))
        st["trades"].append({"symbol": sym, "strategy": self.strategy, "gap": sig.get("gap"),
                             "qty": qty, "entry": sig["entry"], "stop": sig["stop"],
                             "target": sig["target"], "max_loss": round(max_loss, 2),
                             "limit": why, "time": now.isoformat(), "order_id": oid,
                             "trigger": inputs["symbols"][sym]["trigger"], "note": inputs.get("note", ""),
                             "source": inputs.get("source", ""), "gate": gate, "claude_gate": cg, "status": "submitted",
                             "tm_verdict": ((self.advisor.odds.get(sym) or {}).get("verdict", "") if self.advisor else "")})
        self._save()
        fresh.add(sym)
        self._recent_entries[sym] = now
        open_value += qty * sig["entry"]
        if self.n_trades() >= inputs["max_trades"]:
            return "halt", open_value
        return "entered", open_value

    def run(self):
        log.info("Bot running. submit=%s. Create the file STOP to flatten and exit.", self.submit)
        self.alert("Bot started (%s). Alerts are working." % ("paper orders ON" if self.submit else "DRY RUN"))
        if self.hub:
            self.hub.start()
            log.info("Real-time feeds starting: %s", ", ".join(self.hub.feeds))
        while True:
            self.write_heartbeat()
            try:
                if STOP_FILE.exists():
                    flat = any(self.flatten("STOP file found") for _ in range(3))
                    self.write_heartbeat(stopped="user (STOP file)")
                    if flat:
                        self.alert("Bot stopped by the STOP file. All positions were closed.", "warn")
                    else:
                        self.alert("Bot stopped by the STOP file, but positions could NOT be confirmed closed. "
                                   "Check Alpaca and close them by hand.", "critical")
                    log.info("Exiting."); return
                clock = self.trading.get_clock()
                now = datetime.now(NY)
                self.market_open = bool(clock.is_open)
                self._check_streams(now)
                self.close_time = clock.next_close.astimezone(NY) if clock.is_open else None
                trading_today = clock.is_open or clock.next_open.astimezone(NY).date() == now.date()
                if self.advisor:
                    try:
                        self.advisor.step(now, clock.is_open, trading_today)
                    except Exception as e:
                        log.error("advisor error: %s", e)
                if not clock.is_open:
                    wait = (clock.next_open - clock.timestamp).total_seconds()
                    if self.strategy == "gap_fade" and wait < 45 * 60:
                        self.prepare_gap_facts(now)        # ready before 9:31, so the first minute is not spent fetching
                    log.info("Market closed. Next open %s", clock.next_open.astimezone(NY).strftime("%a %H:%M %Z"))
                    self._idle(max(5, min(60 if wait < 2400 else 300, wait)))
                    continue
                self.tick(now)
                self._errs = 0
            except KeyboardInterrupt:
                log.info("Ctrl-C: stopping. Open positions are NOT closed; their stops stay at Alpaca.")
                self.write_heartbeat(stopped="user (Ctrl-C)")
                self.alert("Bot stopped by you (Ctrl-C). Open positions were NOT closed; their stops stay at Alpaca.", "warn")
                return
            except Exception as e:
                self._errs += 1
                log.error("loop error: %s", e)
                if self._errs in (5, 25, 100):
                    self.alert("The bot loop has failed %d times in a row. Latest error: %s: %s" % (
                        self._errs, type(e).__name__, str(e)[:200]), "critical")
            try:
                self._idle(self.cfg["poll_seconds"])
            except KeyboardInterrupt:
                log.info("Ctrl-C: stopping. Open positions are NOT closed; their stops stay at Alpaca.")
                self.write_heartbeat(stopped="user (Ctrl-C)")
                self.alert("Bot stopped by you (Ctrl-C). Open positions were NOT closed; their stops stay at Alpaca.", "warn")
                return


class _MaybeLock:
    """Take the bot's lock if it frees up within `timeout`, but act anyway if it does not.
    The main loop can hold the lock for a minute while Claude or a skill runs; a loss limit must never wait for that."""

    def __init__(self, lock, timeout=2.0):
        self.lock, self.timeout, self.got = lock, timeout, False

    def __enter__(self):
        self.got = self.lock.acquire(timeout=self.timeout)
        if not self.got:
            log.warning("risk monitor: main loop is busy, acting without waiting for it")
        return self

    def __exit__(self, *a):
        if self.got:
            self.lock.release()


class RiskMonitor(threading.Thread):
    """Checks the account every few seconds, independent of the main loop and of Claude."""

    def __init__(self, bot, seconds):
        super().__init__(daemon=True)
        self.bot, self.seconds, self.stop_evt = bot, seconds, threading.Event()

    def run(self):
        while not self.stop_evt.wait(self.seconds):
            try:
                self.check()
            except Exception as e:
                log.error("risk monitor error: %s", e)

    def check(self):
        bot = self.bot
        st, cfg = bot.state, bot.cfg
        if not bot.market_open or st["halted"] or st["flattened"]:
            return
        acct = bot.trading.get_account()
        pnl = float(acct.equity) - float(acct.last_equity)
        if pnl <= -cfg["max_daily_loss_usd"]:
            with _MaybeLock(bot.lock):
                if not st["halted"] and bot.flatten("FAST MONITOR: daily loss limit, P&L %.2f" % pnl):
                    st["halted"] = True
                    bot._save()
            return
        limit = cfg.get("emergency_loss_multiple", 1.5) * cfg["max_loss_per_trade_usd"]
        for p in bot.trading.get_all_positions():
            if -float(p.unrealized_pl) >= limit:
                with _MaybeLock(bot.lock):
                    log.warning("FAST MONITOR: %s is down $%.0f, past %.1fx the per-trade limit. Closing it.",
                                p.symbol, -float(p.unrealized_pl), cfg.get("emergency_loss_multiple", 1.5))
                    bot.alert("FAST MONITOR: %s is down $%.0f, past the emergency limit. Closing it now." % (p.symbol, -float(p.unrealized_pl)), "critical")
                    bot.close_symbol(p.symbol)


def clear_stale_stop():
    """A STOP file found at startup belongs to an earlier run, so remove it. Otherwise the new run would stop at once."""
    try:
        if STOP_FILE.exists():
            STOP_FILE.unlink()
            log.info("Removed a leftover STOP file from an earlier run.")
            return True
    except Exception as e:
        log.warning("could not remove the old STOP file (%s): delete it by hand", e)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--submit", action="store_true", help="actually send orders to the PAPER account")
    ap.add_argument("--no-llm", action="store_true", help="do not call Claude; use a hand-written inputs.json")
    ap.add_argument("--show-prompt", action="store_true", help="print the prompts Claude would see (sample data), then exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(HERE / "bot.log", encoding="utf-8")])
    cfg = json.loads(Path(args.config).read_text())
    try:  # freeze the success criteria the moment the bot first starts, before any results exist
        import evaluate
        evaluate.check_lock()
    except Exception as e:
        log.warning("could not store the success-criteria fingerprint: %s", e)
    global STATE_FILE
    if not args.submit:
        STATE_FILE = HERE / "bot_state_dryrun.json"  # dry-run "trades" must never count in a real paper run
    if args.show_prompt:
        return show_prompts(cfg)
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY (PAPER keys) as environment variables.")
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical import StockHistoricalDataClient
    trading = add_timeout(TradingClient(key, secret, paper=True))  # paper is hard-coded
    acct = trading.get_account()
    if not str(acct.account_number).startswith("PA"):
        sys.exit("This is not a paper account. Refusing to run.")
    log.info("Paper account %s, equity %s", acct.account_number, acct.equity)
    data = add_timeout(StockHistoricalDataClient(key, secret))
    notifier = notify.Notifier.from_env(cfg)
    skills = None
    if cfg.get("skills", {}).get("enabled"):
        import skills as skills_mod
        skills = skills_mod.Skills(cfg, HERE)
        on = [n for n in skills_mod.SCRIPTS if skills.enabled(n)]
        log.info("Skills ON: %s", ", ".join(on) or "none found (check skills.dir)")
        if not skills_mod.valid_user_agent(skills.ua):
            log.warning("sec_user_agent is not a real name+email: SEC filings and the earnings analyst are skipped. Fix it in config.json.")
    advisor = None
    if not args.no_llm:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("Set ANTHROPIC_API_KEY, or run with --no-llm and write inputs.json by hand.")
        import anthropic
        from alpaca.data.historical.news import NewsClient
        import advisor as adv
        advisor = adv.Advisor(cfg, anthropic.Anthropic(timeout=30, max_retries=1), data,
                              add_timeout(NewsClient(key, secret)), INPUTS_FILE, skills=skills)
    events = hub = None
    rt = cfg.get("realtime", {})
    if rt.get("enabled"):
        import stream as stream_mod
        events = queue.Queue()
        feeds = [f for f, k in (("bars", "bars"), ("news", "news"), ("trade_updates", "trade_updates")) if rt.get(k, True)]
        hub = stream_mod.StreamHub(key, secret, sorted(set(cfg["universe"]) | {"SPY", "QQQ"}), events, paper=True, feeds=feeds)
    bot = Bot(cfg, trading, data, args.submit, advisor, skills, notifier, events, hub)
    if advisor:
        advisor.notifier = notifier
    log.info("Phone alerts: %s", "ON (Telegram)" if getattr(notifier, "configured", False) else "OFF (see notify.py)")
    if advisor:
        advisor.bot = bot
        log.info("Claude advisor ON: %s before the open, %s every %s min. Caps: %d calls, $%.2f a day.",
                 cfg["llm"]["premarket_model"], cfg["llm"]["intraday_model"], cfg["llm"]["intraday_minutes"],
                 cfg["llm"]["max_calls_per_day"], cfg["llm"]["max_spend_usd_per_day"])
    clear_stale_stop()
    bot.catch_up_journal()                    # a day stopped early (before 15:57) gets its journal rows now
    if args.submit:
        RiskMonitor(bot, cfg.get("fast_monitor_seconds", 3)).start()
        log.info("Fast risk monitor ON: account checked every %ss.", cfg.get("fast_monitor_seconds", 3))
    try:
        bot.run()
    finally:
        flush = getattr(notifier, "flush", None)
        if flush and not flush(10):                    # let the last alerts ("bot stopped ...") reach the phone
            log.warning("some alerts were still unsent when the bot exited")


def show_prompts(cfg):
    """Print exactly what Claude would be sent, using made-up sample numbers."""
    import advisor as adv
    now = datetime(2026, 10, 27, 9, 20, tzinfo=NY)
    stats = {"NVDA": {"last": 141.2, "chg1d_pct": 1.1, "chg5d_pct": 3.4, "hi20": 142.5, "from_hi20_pct": -0.91,
                      "avg_iex_vol20_m": 180.2, "avg_range14_pct": 2.6},
             "AMD": {"last": 168.0, "chg1d_pct": -0.4, "chg5d_pct": -2.0, "hi20": 175.3, "from_hi20_pct": -4.16,
                     "avg_iex_vol20_m": 45.7, "avg_range14_pct": 3.1}}
    heads = ["[10-26 16:40] NVDA: (sample headline text goes here)"]
    print("=== PRE-MARKET: system ===\n" + adv.SYSTEM_PRE + "\n\n=== PRE-MARKET: user ===\n"
          + adv.build_premarket_prompt(now, stats, heads, cfg))
    cur = {"trade_today": True, "max_trades": 2, "note": "sample", "symbols": [{"symbol": "NVDA", "trigger": 142.5}]}
    now2 = now.replace(hour=10, minute=5)
    print("\n\n=== INTRADAY: system ===\n" + adv.SYSTEM_INTRA + "\n\n=== INTRADAY: user ===\n"
          + adv.build_intraday_prompt(now2, cur, {"NVDA": {"high": 142.1, "low": 140.6}}, {"NVDA": 141.8},
                                      set(), 0, 0.0, [], [],
                                      {"SPY": {"last": 760.1, "since_open_pct": -0.45, "day_range_pos": 0.2, "vs_prev_close_pct": -0.6},
                                       "QQQ": {"last": 590.3, "since_open_pct": -0.7, "day_range_pos": 0.15, "vs_prev_close_pct": -0.9}},
                                      {"NVDA": {"sector": "semis", "gap_pct": 0.3, "vol_pace": 1.4}}))


if __name__ == "__main__":
    main()
