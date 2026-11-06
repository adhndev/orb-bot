"""
evaluate.py - judge the paper run against success_criteria.json, which was written in advance.

    python evaluate.py

Reads journal.csv (real paper fills), shadow.csv (the nightly "no Claude" control) and
backtest_summary.json (if you have run backtest.py). Prints PASS / FAIL / WAIT per bar and one verdict.
WAIT means "not enough data yet", which is not a failure.
"""
import hashlib
import json
from pathlib import Path

import engine
import journal

import apphome
HERE = apphome.HOME                         # data folder (the project folder, or Documents\OrbBot in the app)
CRIT = HERE / "success_criteria.json"
LOCK = HERE / "criteria.lock"


def check_lock(path=CRIT, lock=LOCK):
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if not Path(lock).exists():
        Path(lock).write_text(digest)
        return None
    return None if Path(lock).read_text().strip() == digest else (
        "WARNING: success_criteria.json has CHANGED since the first evaluation. Results judged against moved "
        "goalposts mean nothing. Restore the original file, or start a fresh run with a new lock.")


def _trade(r):
    return {"date": r.get("date", ""), "pnl": float(r["pnl"]), "r": float(r["r"])}


def evaluate(journal_rows, shadow_rows, backtest, c):
    """Returns a list of (name, status, detail) and an overall verdict."""
    out = []
    closed = [_trade(r) for r in journal_rows if r.get("status") == "closed" and r.get("pnl") not in ("", None)]
    p, m = c["paper"], engine.summarize(closed)
    n, days = m["n"], m.get("trading_days", 0)
    enough = n >= p["min_trades"] and days >= p["min_trading_days"]
    out.append(("Paper sample size", "PASS" if enough else "WAIT",
                f"{n} trades over {days} days (need {p['min_trades']} trades and {p['min_trading_days']} days)"))
    if enough:
        bs = engine.bootstrap([t["r"] for t in closed])
        pos = bs[2] if bs else 0.0
        out += [("Average R after costs", "PASS" if m["avg_r"] >= p["min_avg_r"] else "FAIL",
                 f"{m['avg_r']:+.3f} (need >= {p['min_avg_r']:+.2f})"),
                ("Profit factor", "PASS" if m["profit_factor"] >= p["min_profit_factor"] else "FAIL",
                 f"{m['profit_factor']} (need >= {p['min_profit_factor']})"),
                ("Worst drawdown", "PASS" if m["max_drawdown_usd"] <= p["max_drawdown_usd"] else "FAIL",
                 f"${m['max_drawdown_usd']:,.0f} (limit ${p['max_drawdown_usd']:,})"),
                ("Chance the edge is real", "PASS" if pos >= p["min_prob_edge_positive"] else "FAIL",
                 f"{pos:.0%} that true avg R is above zero (need >= {p['min_prob_edge_positive']:.0%})")]
    cv = c["claude_vs_control"]
    mine = [_trade(r) for r in shadow_rows if r.get("in_shortlist") == "yes"]
    rest = [_trade(r) for r in shadow_rows if r.get("in_shortlist") == "no"]
    if min(len(mine), len(rest)) < cv["min_signals_in_each_group"]:
        out.append(("Claude's picks vs the rest", "WAIT",
                    f"{len(mine)} shortlisted vs {len(rest)} other simulated signals (need {cv['min_signals_in_each_group']} each)"))
    else:
        a, b_ = engine.summarize(mine)["avg_r"], engine.summarize(rest)["avg_r"]
        out.append(("Claude's picks vs the rest", "PASS" if a - b_ >= cv["min_avg_r_advantage"] else "FAIL",
                    f"shortlist avg R {a:+.3f} vs others {b_:+.3f} (advantage {a - b_:+.3f}, need >= {cv['min_avg_r_advantage']:+.2f})"))
    bt = c["backtest_holdout"]
    h = (backtest or {}).get("holdout")
    if not h or h.get("n", 0) < bt["min_trades"]:
        out.append(("Backtest on held-back days", "WAIT",
                    f"{(h or {}).get('n', 0)} trades (need {bt['min_trades']}); run backtest.py with --holdout-from"))
    else:
        out.append(("Backtest on held-back days", "PASS" if h["avg_r"] >= bt["min_avg_r"] else "FAIL",
                    f"avg R {h['avg_r']:+.3f} over {h['n']} trades (need >= {bt['min_avg_r']:+.2f})"))
    st = {s for _, s, _ in out}
    verdict = "FAILED" if "FAIL" in st else ("KEEP RUNNING" if "WAIT" in st else "MET")
    return out, verdict


CRIT_BY_STRATEGY = {"orb": ("success_criteria.json", "criteria.lock"),
                    "gap_fade": ("success_criteria_gap.json", "criteria_gap.lock")}


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def evaluate_forward(journal_rows, ledger_rows, c):
    """For a strategy that came out of the research: judged on its own paper trades (rows tagged with it, from its
    start date), on the nightly forward test of the unfiltered rules, and on whether Claude's vetoes help or hurt."""
    out = []
    s, start = c["strategy"], c.get("start_date", "")
    mine = [r for r in journal_rows if r.get("strategy") == s and r.get("date", "") >= start
            and r.get("status") == "closed" and _num(r.get("pnl")) is not None and _num(r.get("r")) is not None]
    closed = [{"date": r["date"], "pnl": _num(r["pnl"]), "r": _num(r["r"])} for r in mine]
    p, m = c["paper"], engine.summarize(closed)
    n, days = m["n"], m.get("trading_days", 0)
    enough = n >= p["min_trades"] and days >= p["min_trading_days"]
    out.append(("Paper sample size", "PASS" if enough else "WAIT",
                f"{n} trades over {days} days (need {p['min_trades']} trades and {p['min_trading_days']} days)"))
    if enough:
        bs = engine.bootstrap([t["r"] for t in closed])
        pos = bs[2] if bs else 0.0
        out += [("Average R after costs", "PASS" if m["avg_r"] >= p["min_avg_r"] else "FAIL",
                 f"{m['avg_r']:+.3f} (need >= {p['min_avg_r']:+.2f})"),
                ("Profit factor", "PASS" if m["profit_factor"] >= p["min_profit_factor"] else "FAIL",
                 f"{m['profit_factor']} (need >= {p['min_profit_factor']})"),
                ("Worst drawdown", "PASS" if m["max_drawdown_usd"] <= p["max_drawdown_usd"] else "FAIL",
                 f"${m['max_drawdown_usd']:,.0f} (limit ${p['max_drawdown_usd']:,})"),
                ("Chance the edge is real", "PASS" if pos >= p["min_prob_edge_positive"] else "FAIL",
                 f"{pos:.0%} that true avg R is above zero (need >= {p['min_prob_edge_positive']:.0%})")]
    f = c["forward"]
    led = [_num(r["r"]) for r in ledger_rows if r.get("variant") == f["variant"] and r.get("date", "") >= start]
    led = [x for x in led if x is not None]
    if len(led) < f["min_trades"]:
        out.append(("Forward test of the rules (unfiltered)", "WAIT",
                    f"{len(led)} trades on new days (need {f['min_trades']})"))
    else:
        avg = sum(led) / len(led)
        out.append(("Forward test of the rules (unfiltered)", "PASS" if avg >= f["min_avg_r"] else "FAIL",
                    f"avg R {avg:+.3f} over {len(led)} trades on days it was never chosen on (need >= {f['min_avg_r']:+.2f})"))
    cv = c["claude_vs_unfiltered"]
    days_traded = {r["date"] for r in closed}
    same = [_num(r["r"]) for r in ledger_rows if r.get("variant") == f["variant"] and r.get("date") in days_traded]
    same = [x for x in same if x is not None]
    if min(len(closed), len(same)) < cv["min_trades_each"]:
        out.append(("Claude's news veto vs the unfiltered rules", "WAIT",
                    f"{len(closed)} paper vs {len(same)} unfiltered trades on the same days (need {cv['min_trades_each']} each)"))
    else:
        a, b_ = m["avg_r"], sum(same) / len(same)
        out.append(("Claude's news veto vs the unfiltered rules", "PASS" if a - b_ >= cv["min_avg_r_advantage"] else "FAIL",
                    f"paper avg R {a:+.3f} vs unfiltered {b_:+.3f} on the same days (need >= {cv['min_avg_r_advantage']:+.2f})"))
    st = {x for _, x, _ in out}
    verdict = "FAILED" if "FAIL" in st else ("KEEP RUNNING" if "WAIT" in st else "MET")
    return out, verdict, n


def current(here=None):
    """The verdict for the strategy in config.json. Returns {strategy, verdict, checks, paper_trades, need_trades,
    criteria, lock_warning}."""
    here = Path(here or HERE)
    cfg = json.loads((here / "config.json").read_text(encoding="utf-8"))
    strat = cfg.get("strategy", "orb")
    crit_name, lock_name = CRIT_BY_STRATEGY.get(strat, CRIT_BY_STRATEGY["orb"])
    c = json.loads((here / crit_name).read_text(encoding="utf-8"))
    warn = check_lock(here / crit_name, here / lock_name)
    jr, sr = journal.read_csv(here / "journal.csv"), journal.read_csv(here / "shadow.csv")
    if "forward" in c:
        checks, verdict, n = evaluate_forward(jr, journal.read_csv(here / "research" / "forward_ledger.csv"), c)
    else:
        bt = json.loads((here / "backtest_summary.json").read_text()) if (here / "backtest_summary.json").exists() else None
        checks, verdict = evaluate(jr, sr, bt, c)
        n = sum(1 for r in jr if r.get("status") == "closed" and r.get("pnl") not in ("", None))
    return {"strategy": strat, "verdict": verdict, "checks": checks, "paper_trades": n,
            "need_trades": c["paper"]["min_trades"], "criteria": c, "lock_warning": warn}


def main():
    r = current()
    c = r["criteria"]
    print(f"Strategy: {r['strategy']}. Success criteria written {c.get('written_on')}\n")
    if r["lock_warning"]:
        print(r["lock_warning"] + "\n")
    for name, status, detail in r["checks"]:
        print(f"[{status:4}] {name}: {detail}")
    print(f"\nVERDICT: {r['verdict']}\n{c['how_to_read_the_verdict'][r['verdict']]}")


def _old_main():
    c = json.loads(CRIT.read_text())
    warn = check_lock()
    bt = None
    if (HERE / "backtest_summary.json").exists():
        bt = json.loads((HERE / "backtest_summary.json").read_text())
    rows, verdict = evaluate(journal.read_csv(journal.JOURNAL), journal.read_csv(journal.SHADOW), bt, c)
    print(f"Success criteria written {c.get('written_on')}\n")
    if warn:
        print(warn + "\n")
    for name, status, detail in rows:
        print(f"[{status:4}] {name}: {detail}")
    print(f"\nVERDICT: {verdict}\n{c['how_to_read_the_verdict'][verdict]}")


if __name__ == "__main__":
    main()
