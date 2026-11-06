"""
skills.py - lets the bot run your trader-* skill scripts and read what they print.

Skills used (each is a standalone Python script from your installed pack, run as a subprocess):
  trader-scanner      daily-chart setups (20-day breakout, bull flag, ...) for the universe
  trader-news         Yahoo headlines + SEC 8-K filings + "MOVING" flags
  trader-analyst      filed earnings numbers from SEC EDGAR for the shortlisted names
  trader-timemachine  10,000 replays + history of the trade plan, before entry
  trader-budget       an independent second sizing of every order
  trader-risk         an independent PASS/BLOCK check of the exact order

NOT used on purpose: trader-broker. The pack's own rules require your typed "yes" and a click on
Allow once for every order. The bot places PAPER orders under its own code limits instead; it never
fakes that approval.

Failure policy: a skill that is missing, slow or prints something unexpected is skipped and logged;
the bot carries on with its own rules. Only an explicit BLOCK or an explicit "strongly toward the
stop" verdict changes a decision.
"""
import json
import logging
import os
import subprocess

import apphome
import sys
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("skills")
SCRIPTS = {"scanner": ("trader-scanner", "scan.py"), "news": ("trader-news", "news.py"),
           "analyst": ("trader-analyst", "facts.py"), "timemachine": ("trader-timemachine", "timemachine.py"),
           "size": ("trader-budget", "size.py"), "risk": ("trader-risk", "check.py")}
CONFIG_KEYS = ["budget", "cash_buffer_pct", "max_position_pct", "max_order_usd", "min_order_usd",
               "max_loss_per_trade_usd", "max_daily_loss_usd", "max_trades_per_day", "max_orders_per_day",
               "min_reward_risk", "require_stop", "no_entries_before", "no_entries_after", "flat_by", "fractional",
               "broker", "paper", "blocked", "allow_options", "allow_margin", "allow_short", "pdt_guard"]


def valid_user_agent(ua):
    return bool(ua) and "@" in ua and "example.com" not in ua


def important_filings(items, now, hours):
    """{symbol: reason} for important SEC filings (earnings results, officer change, bankruptcy, delisting...) in the last N hours."""
    out = {}
    for it in items or []:
        if it.get("kind") != "filing" or not it.get("important"):
            continue
        t = it.get("time")
        try:
            when = datetime.fromisoformat(str(t))
            recent = (now - when.astimezone(now.tzinfo)) <= timedelta(hours=hours)
        except Exception:
            recent = True  # cannot read the time: be careful
        if recent:
            out[str(it.get("ticker", "")).upper()] = str(it.get("title", "important filing"))[:120]
    out.pop("", None)
    return out


def format_news(doc, limit=40):
    """Compact lines for a prompt: tagged items and important filings first, then MOVING stocks."""
    if not doc:
        return []
    items = sorted(doc.get("items", []), key=lambda i: str(i.get("time", "")), reverse=True)   # newest first
    items = sorted(items, key=lambda i: not i.get("important"))                                  # important filings lead
    lines = []
    for it in items:
        if not (it.get("tags") or it.get("important")):
            continue
        mark = "!" if it.get("important") else " "
        tags = ",".join(it.get("tags") or [])
        lines.append(f"{mark}[{str(it.get('time', ''))[5:16]}] {it.get('ticker', '?')} ({tags}): {str(it.get('title', ''))[:130]}")
        if len(lines) >= limit:
            break
    for sym, m in (doc.get("moves") or {}).items():
        if m.get("moving"):
            lines.append(f"MOVING: {sym} {m.get('pct', 0):+.1f}% versus yesterday's close")
    return lines


class Skills:
    def __init__(self, cfg, base_dir, runner=None):
        self.cfg, self.sc = cfg, cfg.get("skills", {})
        self.base = Path(base_dir)
        rel = self.sc.get("dir", "skills")
        here = self.base / rel
        self.dir = Path(os.environ.get("SKILLS_DIR") or (here if here.exists() else apphome.CODE / rel))
        self.timeout = self.sc.get("timeout_s", 45)
        self.ua = os.environ.get("SEC_USER_AGENT") or cfg.get("sec_user_agent", "")
        self._runner = runner or self._subprocess
        (self.base / "trader" / "state").mkdir(parents=True, exist_ok=True)
        self.sync_config()
        missing = [n for n in SCRIPTS if not self.path(n).exists()]
        if self.sc.get("enabled") and missing:
            log.warning("Skill scripts not found in %s: %s. Copy the 'skills' folder (trader-news, trader-scanner, ...) "
                        "next to the bot files, or set skills.dir in config.json. Those skills are OFF.",
                        self.dir, ", ".join(missing))

    def sync_config(self):
        """The skills read trader/config.json. Build it from the bot's config so there is one source of truth."""
        c = {k: self.cfg[k] for k in CONFIG_KEYS if k in self.cfg}
        strat = self.cfg.get("strategy", "orb")
        if strat != "orb":   # a strategy may set its own entry window / reward:risk floor for the risk skill
            c.update({k: v for k, v in (self.cfg.get(strat, {}).get("risk_skill") or {}).items() if k in CONFIG_KEYS})
        c["sec_user_agent"] = self.ua
        (self.base / "trader" / "config.json").write_text(json.dumps(c, indent=2))

    def path(self, name):
        folder, script = SCRIPTS[name]
        return self.dir / folder / script

    def enabled(self, name):
        flag = {"size": "size_check", "risk": "risk_check"}.get(name, name)
        return bool(self.sc.get("enabled")) and bool(self.sc.get(flag, True)) and self.path(name).exists()

    def _subprocess(self, script, args):
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")  # Windows pipes use a legacy encoding otherwise
        if self.ua:
            env["SEC_USER_AGENT"] = self.ua
        try:
            p = subprocess.run(apphome.script_cmd(script, *args), cwd=str(self.base), capture_output=True,
                               encoding="utf-8", errors="replace", timeout=self.timeout, env=env)
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "timed out after %ss" % self.timeout
        except Exception as e:
            return 127, "", str(e)

    def call(self, name, args):
        if not self.enabled(name):
            return None
        code, out, err = self._runner(self.path(name), args)
        if code not in (0, 1):
            log.warning("skill %s failed (exit %s): %s", name, code, (err or out).strip()[:200])
            return None
        return code, out

    @staticmethod
    def _json(out):
        try:
            return json.loads(out)
        except Exception:
            return None

    # ----- information skills -----
    def scan_daily(self, symbols):
        r = self.call("scanner", ["daily", "--symbols", ",".join(symbols), "--json", "--no-write", "--min-price", "5"])
        doc = self._json(r[1]) if r else None
        if not doc:
            if r:
                log.warning("scanner: unexpected output: %s", r[1][:200].strip())
            return None
        return [h for h in doc.get("hits", []) if isinstance(h, dict) and h.get("symbol")]

    def news(self, tickers, hours, new_only):
        args = ["--tickers", *tickers, "--hours", str(hours), "--json"]
        if not new_only:
            args += ["--all", "--dry-run"]
        if not valid_user_agent(self.ua):
            args.append("--no-sec")
        r = self.call("news", args)
        if not r:
            return None
        doc = self._json(r[1])
        if doc is None:
            if r[0] == 1:  # exit code 1 = nothing new
                return {"items": [], "moves": {}, "notes": []}
            log.warning("news: unexpected output: %s", r[1][:200].strip())
            return None
        moves = {}
        for m in doc.get("moves") or []:
            if isinstance(m, dict) and m.get("ticker"):
                moves[str(m["ticker"]).upper()] = m
        return {"items": [i for i in doc.get("items", []) if isinstance(i, dict)], "moves": moves, "notes": doc.get("notes", [])}

    def facts(self, tickers, max_chars=8000):
        if not valid_user_agent(self.ua):
            log.info("analyst skipped: put a real name and email in sec_user_agent (SEC requires it)")
            return None
        r = self.call("analyst", [*tickers, "--quarters", "4"])
        text = (r[1] if r else "").strip()
        return text[:max_chars] if text else None

    def timemachine(self, symbol, date, entry, stop, target, entry_time, until):
        r = self.call("timemachine", ["run", "--symbol", symbol, "--date", date, "--entry", "%.2f" % entry,
                                      "--stop", "%.2f" % stop, "--target", "%.2f" % target,
                                      "--entry-time", entry_time, "--until", until, "--json"])
        if not r:
            return None
        doc = self._json(r[1])
        if not doc or "mc" not in doc:
            return {"error": (r[1] or "no output").strip().splitlines()[0][:200] if (r[1] or "").strip() else "no output"}
        mc, hist = doc.get("mc", {}), doc.get("history", {})
        return {"verdict": str(doc.get("verdict", "")), "notes": list(doc.get("verdict_notes") or []),
                "p_target": mc.get("p_target"), "p_stop": mc.get("p_stop"),
                "history": {k: v for k, v in hist.items() if isinstance(v, (int, float, str))}}

    # ----- independent checks of the exact order -----
    def second_opinion(self, o, now):
        """o: symbol, qty, entry, stop, target, date, entry_time, flat_by.
        Returns {"qty": smaller of the two sizers, "skill_qty", "verdict": PASS|BLOCK, "reasons": [...]} or None if unavailable."""
        if not (self.enabled("size") or self.enabled("risk")):
            return None
        tdir, pin = self.base / "trader", ["--now", now.strftime("%Y-%m-%d %H:%M")]
        qty, skill_qty = int(o["qty"]), None
        if self.enabled("size"):
            (tdir / "plan.json").write_text(json.dumps({
                "symbol": o["symbol"], "side": "buy", "date": o["date"], "entry": o["entry"], "stop": o["stop"],
                "target": o["target"], "entry_time": o["entry_time"], "setup": self.cfg.get("strategy", "orb"), "flat_by": o["flat_by"]}))
            r = self.call("size", ["--json", *pin])
            doc = self._json(r[1]) if r else None
            if doc is None and r:
                log.warning("size: unexpected output: %s", r[1][:200].strip())
            if doc is not None:
                orders = doc.get("orders") or []
                if not orders:
                    why = str((doc.get("sizing") or {}).get("refusal") or "; ".join(str(n) for n in (doc.get("notes") or []))
                              or "the sizer refused this plan")
                    return {"qty": 0, "skill_qty": 0, "verdict": "BLOCK", "reasons": ["trader-budget: " + why[:200]]}
                skill_qty = int(float(orders[0]["qty"]))
                qty = min(qty, skill_qty)
        verdict, reasons = "PASS", []
        if self.enabled("risk") and qty >= 1:
            order = {"symbol": o["symbol"], "side": "buy", "qty": str(qty), "type": "market", "time_in_force": "day",
                     "order_class": "bracket", "take_profit_limit_price": "%.2f" % o["target"],
                     "stop_loss_stop_price": "%.2f" % o["stop"],
                     "plan": {"entry": o["entry"], "stop": o["stop"], "target": o["target"], "setup": self.cfg.get("strategy", "orb"),
                              "entry_time": o["entry_time"], "flat_by": o["flat_by"]}}
            (tdir / "orders.json").write_text(json.dumps({"created_by": "orb_bot", "status": "proposed",
                                                          "paper": True, "orders": [order]}))
            r = self.call("risk", ["--json", "--no-write", *pin])
            doc = self._json(r[1]) if r else None
            if doc is None and r:
                log.warning("risk: unexpected output: %s", r[1][:200].strip())
            if doc and doc.get("results"):
                res = doc["results"][0]
                verdict, reasons = res.get("verdict", "PASS"), list(res.get("reasons") or [])
        return {"qty": qty, "skill_qty": skill_qty, "verdict": verdict, "reasons": reasons}
