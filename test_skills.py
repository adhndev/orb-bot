"""Offline tests for the skills integration. The sizer and risk checker run for real (they need no network);
the other skills are replaced by canned output shaped like what their scripts print. Run: python test_skills.py"""
import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
import pandas as pd
import pytest
import advisor as adv
import orb_bot as b
import skills
import test_advisor as ta
import test_orb_bot as to

CFG = json.load(open(b.HERE / "config.json"))
CFG["strategy"] = "orb"            # these tests are about the opening-range rules; gap mode has its own tests
CFG["strategy"] = "orb"            # these tests are about the opening-range rules; gap mode has its own tests
NY = b.NY
NOW = datetime(2026, 10, 27, 9, 5, tzinfo=NY)
T = b.HERE
NEEDS_SKILLS = pytest.mark.skipif(not (b.HERE / "skills" / "trader-risk" / "check.py").exists(),
                                  reason="needs the trader-* skill scripts in skills/ (not in the repository)")


def real_skills(runner=None):
    tmp = Path(tempfile.mkdtemp())
    cfg = dict(CFG, skills=dict(CFG["skills"], dir=str((b.HERE / "skills").resolve())))   # point at the real folder from the start
    return skills.Skills(cfg, tmp, runner), tmp


@NEEDS_SKILLS
def test_real_sizer_and_risk():
    sk, tmp = real_skills()
    assert sk.enabled("size") and sk.enabled("risk"), (
        "The 'skills' folder was not found at %s. Copy the whole 'skills' folder (it holds trader-budget, trader-risk, "
        "trader-news, ...) into the same folder as test_skills.py." % sk.dir)
    now = datetime(2026, 10, 26, 9, 52, tzinfo=NY)
    o = dict(symbol="TSLA", qty=25, entry=394.27, stop=388.78, target=405.25, date="2026-10-26", entry_time="09:52 AM", flat_by="15:55")
    r = sk.second_opinion(o, now)
    assert r == {"qty": 25, "skill_qty": 25, "verdict": "PASS", "reasons": []}
    assert sk.second_opinion(dict(o, qty=50), now)["qty"] == 25                    # the smaller of the two sizers wins
    r = sk.second_opinion(dict(o, stop=300.0), now)                                # reward:risk far too low
    assert r["verdict"] == "BLOCK" and "reward:risk" in r["reasons"][0]
    r = sk.second_opinion(o, datetime(2026, 10, 26, 9, 31, tzinfo=NY))             # before the 9:45 entry window
    assert r["verdict"] == "BLOCK" and any("window" in x or "before" in x for x in r["reasons"]), r
    assert json.load(open(tmp / "trader" / "config.json"))["max_loss_per_trade_usd"] == 250
    shutil.rmtree(tmp)


def runner_for(table, calls=None):
    def run(script, args):
        name = Path(script).parent.name
        if calls is not None:
            calls.append((name, list(args)))
        v = table.get(name)
        return v(args) if callable(v) else (v if v is not None else (127, "", "missing"))
    return run


@NEEDS_SKILLS
def test_parsers_and_failures():
    now = datetime(2026, 10, 27, 9, 5, tzinfo=NY)
    news_doc = {"checked": "x", "moves": [{"ticker": "NVDA", "price": 150, "prev_close": 140, "pct": 7.1, "moving": True}],
                "items": [{"kind": "filing", "important": True, "ticker": "TSLA", "title": "Tesla filed a 8-K: results", "tags": ["earnings"],
                           "time": "2026-10-27T07:30:00-04:00", "items": ["2.02"]},
                          {"kind": "filing", "important": True, "ticker": "OLD", "title": "old", "tags": [], "time": "2026-10-24T07:30:00-04:00"},
                          {"kind": "headline", "important": False, "ticker": "AMD", "title": "AMD plans a chip", "tags": ["deal"], "time": "2026-10-27T08:00:00-04:00"},
                          {"kind": "headline", "important": False, "ticker": "AMD", "title": "boring", "tags": [], "time": "2026-10-27T08:01:00-04:00"}],
                "notes": []}
    calls = []
    sk, tmp = real_skills(runner_for({
        "trader-news": (0, json.dumps(news_doc), ""),
        "trader-scanner": (0, json.dumps({"hits": [{"symbol": "NVDA", "setups": ["breakout_20d"], "rsi": 61.0}], "counts": {}}), ""),
        "trader-analyst": (0, "NVDA  revenue 46.7B +56%\n" * 3, ""),
        "trader-timemachine": (0, json.dumps({"plan": {}, "mc": {"p_target": 0.24, "p_stop": 0.5}, "history": {"avg_r": -0.06, "n": 33, "x": [1]},
                                              "verdict": "Strongly toward the stop: ...", "verdict_notes": ["note"]}), "")}, calls))
    sk.ua = "Ada Lovelace ada@analytical.org"
    d = sk.news(["TSLA", "AMD"], 20, new_only=False)
    assert "--all" in calls[0][1] and "--dry-run" in calls[0][1] and "--no-sec" not in calls[0][1]
    assert d["moves"]["NVDA"]["moving"] and len(d["items"]) == 4
    assert skills.important_filings(d["items"], now, 18) == {"TSLA": "Tesla filed a 8-K: results"}   # the 3-day-old filing is ignored
    lines = skills.format_news(d)
    assert lines[0].startswith("!") and "TSLA" in lines[0] and any("MOVING: NVDA" in x for x in lines) and not any("boring" in x for x in lines)
    assert sk.scan_daily(["NVDA"])[0]["symbol"] == "NVDA"
    assert sk.facts(["NVDA"]).startswith("NVDA")
    tm = sk.timemachine("NVDA", "2026-10-27", 101.01, 100.0, 103.03, "09:45 AM", "15:55")
    assert tm["verdict"].startswith("Strongly toward the stop") and tm["p_stop"] == 0.5 and tm["history"] == {"avg_r": -0.06, "n": 33}
    sk.ua = "Your Name you@example.com"                                                           # placeholder: SEC parts are skipped
    calls.clear(); sk.news(["TSLA"], 1, new_only=True)
    assert "--no-sec" in calls[0][1] and "--all" not in calls[0][1] and sk.facts(["TSLA"]) is None
    # failures never raise: crash, timeout, garbage, "nothing new", missing script, switched off
    for res in ((2, "", "boom"), (124, "", "timed out"), (0, "not json", "")):
        sk2, t2 = real_skills(runner_for({"trader-news": res, "trader-scanner": res, "trader-timemachine": res}))
        assert sk2.news(["A"], 1, True) is None and sk2.scan_daily(["A"]) is None
        assert sk2.timemachine("A", "d", 1, 1, 1, "t", "u") in (None, {"error": "not json"})
        shutil.rmtree(t2)
    sk3, t3 = real_skills(runner_for({"trader-news": (1, "Nothing new since the last check.", "")}))
    assert sk3.news(["A"], 1, True) == {"items": [], "moves": {}, "notes": []}
    sk3.sc = dict(sk3.sc, news=False); assert sk3.news(["A"], 1, True) is None
    sk3.dir = Path("/nonexistent"); assert not sk3.enabled("scanner")
    sk4, t4 = real_skills(runner_for({"trader-timemachine": (1, "Refusing: the entry bar is not in the data.", "")}))
    assert sk4.timemachine("A", "d", 1, 1, 1, "t", "u") == {"error": "Refusing: the entry bar is not in the data."}
    for t in (tmp, t2, t3, t4):
        shutil.rmtree(t, ignore_errors=True)


class FakeSkills:
    def __init__(self, on=("scanner", "news", "analyst", "timemachine"), hits=None, premarket_news=None, intraday_news=None, facts="NVDA facts", tm=None):
        self.on, self.hits, self.facts_text, self.tm = set(on), hits, facts, tm or {}
        self.premarket_news, self.intraday_news, self.tm_calls = premarket_news, intraday_news, []

    def enabled(self, n):
        return n in self.on

    def scan_daily(self, syms):
        return self.hits

    def news(self, tickers, hours, new_only):
        return self.intraday_news if new_only else self.premarket_news

    def facts(self, tickers):
        return self.facts_text

    def timemachine(self, sym, *a):
        self.tm_calls.append(sym)
        return self.tm.get(sym)


def filing(sym, hours_ago=2):
    return {"kind": "filing", "important": True, "ticker": sym, "title": f"{sym} filed a 8-K: results", "tags": ["earnings"],
            "time": datetime(2026, 10, 27, 9 - hours_ago // 2 if hours_ago < 9 else 7, 0, tzinfo=NY).isoformat()}


def test_premarket_with_skills():
    p1 = '{"trade_today": true, "max_trades": 2, "note": "picks", "symbols": [{"symbol": "NVDA"}, {"symbol": "AMD"}, {"symbol": "TSLA"}]}'
    p2 = '{"trade_today": true, "max_trades": 2, "note": "AMD may report today", "symbols": [{"symbol": "NVDA"}]}'
    llm = ta.FakeLLM(p1, p2)
    a, path, _ = ta.make(llm)
    a.skills = FakeSkills(hits=[{"symbol": "NVDA", "setups": ["breakout_20d"], "rsi": 61.0}],
                          premarket_news={"items": [filing("TSLA")], "moves": {}, "notes": []})
    a.premarket(NOW)
    d = json.loads(path.read_text())
    assert [s["symbol"] for s in d["symbols"]] == ["NVDA"]                       # TSLA removed by code, AMD removed in review
    assert "SCANNER" in llm.prompts[0] and "breakout_20d" in llm.prompts[0] and "REMOVED BY CODE" in llm.prompts[0]
    assert "TSLA [" not in llm.prompts[0]                                         # Claude never even sees the blocked stock's chart
    assert "<filings_data>" in llm.prompts[1] and "NVDA facts" in llm.prompts[1] and a.st["calls"] == 2
    assert "review: AMD may report today" in d["note"]
    # review that tries to ADD a stock or loosen is ignored; unusable review keeps stage 1
    p2b = '{"trade_today": true, "max_trades": 3, "symbols": [{"symbol": "NVDA"}, {"symbol": "AMD"}, {"symbol": "META"}]}'
    a, path, _ = ta.make(ta.FakeLLM('{"trade_today": true, "max_trades": 2, "symbols": [{"symbol": "NVDA"}]}', p2b))
    a.skills = FakeSkills(premarket_news={"items": [], "moves": {}, "notes": []})
    a.premarket(NOW)
    d = json.loads(path.read_text()); assert [s["symbol"] for s in d["symbols"]] == ["NVDA"] and d["max_trades"] == 2
    a, path, _ = ta.make(ta.FakeLLM('{"trade_today": true, "symbols": [{"symbol": "NVDA"}, {"symbol": "AMD"}]}', "garbage"))
    a.skills = FakeSkills(premarket_news={"items": [], "moves": {}, "notes": []})
    a.premarket(NOW); assert len(json.loads(path.read_text())["symbols"]) == 2
    a, path, _ = ta.make(ta.FakeLLM('{"trade_today": true, "symbols": [{"symbol": "NVDA"}]}'))        # skills off -> original path
    a.skills = FakeSkills(on=())
    a.premarket(NOW); assert a.st["calls"] == 1 and json.loads(path.read_text())["trade_today"]


def test_intraday_with_skills():
    pre = '{"trade_today": true, "max_trades": 2, "note": "n", "symbols": [{"symbol": "NVDA"}, {"symbol": "AMD"}, {"symbol": "TSLA"}]}'
    keep = pre
    rng = {s: {"high": 101.0, "low": 99.0, "mid": 100.0} for s in ("NVDA", "AMD", "TSLA")}
    bot = NS(fetch_bars=lambda syms, now: {s: ta.daily() for s in syms}, ranges=rng, state={"tried": [], "trades": []},
             trading=NS(get_account=lambda: NS(equity="100000", last_equity="100000"), get_all_positions=lambda: []))
    t = datetime(2026, 10, 27, 10, 0, tzinfo=NY)
    tm_bad = {"verdict": "Strongly toward the stop: target first 20.0 % vs stop first 55.0 %.", "p_target": .2, "p_stop": .55}
    tm_ok = {"verdict": "Close to break-even: ...", "p_target": .3, "p_stop": .4}
    llm = ta.FakeLLM(pre, keep)
    a, path, _ = ta.make(llm, bot=bot, syms=("NVDA", "AMD", "TSLA"))
    a.skills = FakeSkills(premarket_news={"items": [], "moves": {}, "notes": []}, tm={"NVDA": tm_bad, "AMD": tm_ok, "TSLA": tm_ok},
                          intraday_news={"items": [filing("AMD")], "moves": {}, "notes": []})
    a.step(datetime(2026, 10, 27, 9, 0, tzinfo=NY), False, True)
    a.step(datetime(2026, 10, 27, 9, 47, tzinfo=NY), True, True)                 # too early for Time Machine (needs the 9:45 bar in Yahoo)
    assert a.skills.tm_calls == []
    a.st["last_intraday"] = None
    a.step(t, True, True)
    d = json.loads(path.read_text())
    assert len(a.skills.tm_calls) == 2                                            # at most 2 Time Machine runs per cycle
    syms = [s["symbol"] for s in d["symbols"]]
    assert syms == ["TSLA"]                                                       # NVDA by Time Machine verdict, AMD by new filing
    assert "code removed" in d["note"] and d["source"] in ("skill-block", "claude-intraday")
    assert "time_machine" in llm.prompts[-1]
    # a shortlist emptied by the code ends trading for the day without a model call
    llm = ta.FakeLLM(pre)
    a, path, _ = ta.make(llm, bot=bot, syms=("NVDA", "AMD", "TSLA"))
    a.skills = FakeSkills(premarket_news={"items": [], "moves": {}, "notes": []}, tm={s: tm_bad for s in ("NVDA", "AMD", "TSLA")},
                          intraday_news={"items": [], "moves": {}, "notes": []})
    a.step(datetime(2026, 10, 27, 9, 0, tzinfo=NY), False, True); n = a.st["calls"]
    a.step(t, True, True); a.step(t.replace(minute=7), True, True)
    d = json.loads(path.read_text())
    assert d["trade_today"] is False and a.st["calls"] == n + 1 and len(a.skills.tm_calls) == 3   # one stock survived cycle 1, so one model call
    assert (a.skills.tm_calls.count("NVDA") == 1)                                 # each stock is only ever analysed once


def test_bot_gate():
    bars = to.make_bars(closes_after=[100.9, 101.2, 101.25])
    now = datetime(2026, 10, 27, 9, 49, 5, tzinfo=NY)
    for op, expect_orders, expect_qty in ((None, 1, 98),
                                          ({"qty": 40, "skill_qty": 40, "verdict": "PASS", "reasons": []}, 1, 40),
                                          ({"qty": 0, "skill_qty": 0, "verdict": "BLOCK", "reasons": ["loss: too big"]}, 0, None),
                                          ({"qty": 98, "skill_qty": 98, "verdict": "BLOCK", "reasons": ["window"]}, 0, None)):
        bot, tr = to.fake_bot(bars)
        bot.skills = NS(second_opinion=lambda o, n, op=op: op)
        bot.tick(now)
        assert tr.submit_order.call_count == expect_orders, op
        if expect_orders:
            assert tr.submit_order.call_args[0][0].qty == expect_qty
            assert bot.state["trades"][0]["max_loss"] == round(expect_qty * 0.75, 2)
            assert bot.state["trades"][0]["gate"].get("risk_check") == (op["verdict"] if op else None)
        else:
            assert bot.state["tried"] == ["TSLA"]                                  # a veto is final for the day


if __name__ == "__main__":
    test_real_sizer_and_risk(); test_parsers_and_failures(); test_premarket_with_skills()
    test_intraday_with_skills(); test_bot_gate()
    for f in ("t_adv_state.json", "t_llm_log.jsonl", "t_inputs.json", "t_inputs.tmp", "test_state.json", "test_inputs.json"):
        (T / f).unlink(missing_ok=True)
    print("skills integration tests passed")
