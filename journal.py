"""
journal.py - trade journal, end-of-day close-out and the "no Claude" control.

Runs automatically ~2 minutes after the flat-by time (from orb_bot.py), or by hand:
    python journal.py reconcile     # rebuild today's journal rows from Alpaca's real fills
    python journal.py shadow        # re-run today's control simulation

journal.csv  One row per paper trade, from Alpaca's ACTUAL fills: entry and exit fill,
             how it ended (target / stop / flatten), P&L in dollars and in R (multiples
             of the planned risk), entry slippage, plus the trigger and Claude's note.
shadow.csv   What the same rules would have done TODAY on EVERY stock in your universe,
             simulated on the day's bars, flagged in_shortlist yes/no. Comparing the two
             groups over weeks shows whether Claude's picks beat picking blindly.
daily_inputs/  A copy of each day's final inputs.json, so you can see what Claude chose.
"""
import csv
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import engine

import apphome
HERE = apphome.HOME                         # data folder (the project folder, or Documents\OrbBot in the app)
JOURNAL = HERE / "journal.csv"
SHADOW = HERE / "shadow.csv"
J_COLS = ["date", "symbol", "order_id", "status", "qty", "planned_entry", "entry_fill", "entry_slippage", "stop",
          "target", "exit_fill", "exit_reason", "pnl", "r", "trigger", "source", "note", "entered_at",
          "qty_skill", "risk_check", "time_machine", "strategy"]
S_COLS = ["date", "symbol", "in_shortlist", "qty", "entry_fill", "stop", "target", "exit_fill", "exit_reason",
          "pnl", "r", "risk_usd"]
log = logging.getLogger("journal")


def _val(x):
    return getattr(x, "value", x)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _upsert(path, cols, rows, key):
    old = {}
    if Path(path).exists():
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                old[(r["date"], r[key])] = r
    for r in rows:
        old[(r["date"], str(r[key]))] = {c: r.get(c, "") for c in cols}
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(sorted(old.values(), key=lambda r: (r["date"], str(r.get(key, "")))))


def read_csv(path):
    if not Path(path).exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def reconcile(trading, state, now, path=JOURNAL, until=None):
    """Turn the day's bot trades into journal rows using Alpaca's real order fills.
    until: end of the window to read orders from (a catch-up for an earlier day must not see later days' orders)."""
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    req = dict(status=QueryOrderStatus.ALL, after=day0, limit=500, nested=True)
    if until is not None:
        req["until"] = until.astimezone(timezone.utc)
    orders = trading.get_orders(GetOrdersRequest(**req))
    by_id = {str(o.id): o for o in orders}
    rows = []
    for t in state["trades"]:
        oid = t.get("order_id")
        if not oid or oid == "dry-run":
            continue
        row = {"date": state["date"], "symbol": t["symbol"], "order_id": oid, "planned_entry": t["entry"],
               "stop": t["stop"], "target": t["target"], "trigger": t.get("trigger") or "",
               "source": t.get("source", ""), "note": t.get("note", ""), "entered_at": t["time"],
               "qty_skill": (t.get("gate") or {}).get("qty_skill", ""), "risk_check": (t.get("gate") or {}).get("risk_check", ""),
               "time_machine": t.get("tm_verdict", ""), "strategy": t.get("strategy", "orb")}
        o = by_id.get(str(oid))
        filled = _num(o.filled_qty) if o is not None else None
        if not filled:
            row.update(status="not_filled" if o is not None else "unknown", qty=0)
            rows.append(row)
            continue
        entry = _num(o.filled_avg_price)
        exit_px = exit_reason = None
        leg_ids, qty_out = set(), filled
        for leg in (o.legs or []):
            leg_ids.add(str(leg.id))
            if _num(leg.filled_qty):
                exit_px, qty_out = _num(leg.filled_avg_price), _num(leg.filled_qty)
                exit_reason = "target" if "limit" in str(_val(leg.order_type)) else "stop"
        if exit_px is None:  # closed by the flatten step or by hand
            for x in orders:
                if (x.symbol == t["symbol"] and str(_val(x.side)) == "sell" and _num(x.filled_qty)
                        and str(x.id) not in leg_ids and str(x.id) != str(oid)):
                    exit_px, qty_out, exit_reason = _num(x.filled_avg_price), _num(x.filled_qty), "flatten"
                    break
        row.update(qty=filled, entry_fill=entry, entry_slippage=round(entry - t["entry"], 4),
                   status="closed" if exit_px is not None else "open")
        if exit_px is not None:
            pnl = (exit_px - entry) * min(filled, qty_out)
            row.update(exit_fill=exit_px, exit_reason=exit_reason, pnl=round(pnl, 2),
                       r=round(pnl / t["max_loss"], 3) if t.get("max_loss") else "")
        rows.append(row)
    _upsert(path, J_COLS, rows, "order_id")
    return rows


def shadow(bot, now, path=SHADOW, inputs_path=None, keep_dir=None):
    """Simulate today's rules on every stock in the universe and flag which ones Claude shortlisted."""
    import orb_bot
    cfg = bot.cfg
    if orb_bot.blackout_for(cfg, now.date().isoformat())[0]:
        return []  # a day the bot never trades must not feed the "Claude vs everything else" comparison
    bars = bot.fetch_bars(list(cfg["universe"]), now)
    trades = engine.simulate_day(bars, cfg, portfolio=False)
    short, ipath = set(), Path(inputs_path or HERE / "inputs.json")
    try:
        raw = json.loads(ipath.read_text())
        if raw.get("date") == now.date().isoformat() and raw.get("trade_today"):
            short = {x["symbol"] for x in raw.get("symbols", [])}
        keep = Path(keep_dir or HERE / "daily_inputs")
        keep.mkdir(exist_ok=True)
        shutil.copy(ipath, keep / f"inputs_{now.date().isoformat()}.json")
    except Exception:
        pass
    rows = [dict(t, in_shortlist="yes" if t["symbol"] in short else "no", date=now.date().isoformat()) for t in trades]
    _upsert(path, S_COLS, rows, "symbol")
    return rows


CATCH_UP_TRIES = 5


def catch_up(trading, history, today, path=JOURNAL):
    """For each archived day (state_history/DATE.json) not yet in the journal: rebuild its rows from that day's
    Alpaca fills. A day with a trade still unresolved is retried on later runs, up to CATCH_UP_TRIES times.
    Returns [(date, rows)] for the days it wrote."""
    from datetime import date as _date, time as _time, timedelta
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    done = []
    for f in sorted(Path(history).glob("*.json")) if Path(history).exists() else []:
        try:
            s = json.loads(f.read_text())
            day = _date.fromisoformat(s["date"])
        except Exception:
            continue
        real = [t for t in s.get("trades", []) if t.get("order_id") and t["order_id"] != "dry-run"]
        if s.get("journaled") or day >= today or not real:
            continue
        start = datetime.combine(day, _time(4, 0), tzinfo=ny)
        rows = reconcile(trading, s, start, path, until=datetime.combine(day + timedelta(days=1), _time(0, 0), tzinfo=ny))
        s["journal_tries"] = s.get("journal_tries", 0) + 1
        if not any(r.get("status") in ("open", "unknown") for r in rows) or s["journal_tries"] >= CATCH_UP_TRIES:
            s["journaled"] = True
        f.write_text(json.dumps(s, indent=2))
        done.append((s["date"], rows))
    return done


def close_out(bot, now):
    jrows = []
    if bot.submit:
        rows = jrows = reconcile(bot.trading, bot.state, now)
        log.info("JOURNAL: %d trade rows written to journal.csv", len(rows))
        for r in rows:
            log.info("  %s %s x%s -> %s, P&L %s, R %s", r["symbol"], r.get("entry_fill", ""), r.get("qty", ""),
                     r.get("exit_reason", r["status"]), r.get("pnl", ""), r.get("r", ""))
    rows = shadow(bot, now)
    mine = [r for r in rows if r["in_shortlist"] == "yes"]
    log.info("CONTROL: %d signals across the universe today (%d from Claude's shortlist), shadow.csv updated",
             len(rows), len(mine))
    if bot.submit:
        done = [r for r in jrows if r.get("status") == "closed"]
        total = sum(float(r["pnl"]) for r in done)
        lines = ["%s x%s: %s, $%s (%sR)" % (r["symbol"], r["qty"], r.get("exit_reason"), r["pnl"], r["r"]) for r in done]
        bot.alert("End of day: %d trades closed, total $%.2f.\n%s\n%d open or unfilled rows: check the journal.%s" % (
            len(done), total, "\n".join(lines) or "(no trades today)", len(jrows) - len(done), verdict_line()))
        if now.weekday() == 4:                     # Friday: the week in one message
            bot.alert(weekly_report(now))


def _read(path):
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def verdict_line():
    """One line from evaluate.py: the verdict for the strategy that is trading now, and how far its sample has got."""
    try:
        import evaluate
        r = evaluate.current(HERE)
        fails = [name for name, st, _ in r["checks"] if st == "FAIL"]
        return "\nScorecard (%s): %s (%d/%d paper trades)%s" % (r["strategy"], r["verdict"], r["paper_trades"],
                                                              r["need_trades"], ("; failing: " + ", ".join(fails)) if fails else "")
    except Exception as e:
        return "\nScorecard unavailable (%s)" % type(e).__name__


def weekly_report(now):
    """Monday-Friday of this week from journal.csv, plus Claude's spend and the verdict."""
    monday = (now - __import__("datetime").timedelta(days=now.weekday())).date().isoformat()
    friday = now.date().isoformat()
    rows = [r for r in _read(JOURNAL) if monday <= r.get("date", "") <= friday and r.get("status") == "closed"
            and r.get("pnl") not in ("", None)]
    pnl = sum(float(r["pnl"]) for r in rows)
    rs = [float(r["r"]) for r in rows if r.get("r") not in ("", None)]
    spend = 0.0
    try:
        for line in (HERE / "llm_log.jsonl").read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            if monday <= d.get("time", "")[:10] <= friday:
                spend += float(d.get("cost_usd") or 0)
    except Exception:
        pass
    by_day = {}
    for r in rows:
        by_day[r["date"]] = by_day.get(r["date"], 0.0) + float(r["pnl"])
    days = "\n".join("%s: %s$%.2f" % (d, "-" if v < 0 else "+", abs(v)) for d, v in sorted(by_day.items()))
    return ("Weekly report %s to %s\nTrades: %d | P&L %s$%.2f | win rate %s | avg R %s\n%s\nClaude spend this week: $%.2f%s" % (
        monday, friday, len(rows), "-" if pnl < 0 else "+", abs(pnl),
        "%.0f%%" % (100 * sum(1 for x in rs if x > 0) / len(rs)) if rs else "n/a",
        "%+.2f" % (sum(rs) / len(rs)) if rs else "n/a", days or "(no closed trades)", spend, verdict_line()))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) < 2 or sys.argv[1] not in ("reconcile", "shadow"):
        sys.exit(__doc__)
    import orb_bot
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical import StockHistoricalDataClient
    cfg = json.loads((HERE / "config.json").read_text())
    key, secret = os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    trading = orb_bot.add_timeout(TradingClient(key, secret, paper=True))
    data = orb_bot.add_timeout(StockHistoricalDataClient(key, secret))
    bot = orb_bot.Bot(cfg, trading, data, True)
    now = datetime.now(orb_bot.NY)
    if sys.argv[1] == "reconcile":
        print(len(reconcile(trading, bot.state, now)), "rows written to journal.csv")
    else:
        print(len(shadow(bot, now)), "rows written to shadow.csv")


if __name__ == "__main__":
    main()
