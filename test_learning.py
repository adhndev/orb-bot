"""Offline tests for the nightly learning step (research/daily.py) and how lessons reach the morning prompt."""
import gzip, json, shutil, sys, tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
import numpy as np, pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "research"))
import daily, advisor

NY = daily.NY


def bars(day, p0, rng):
    ts = pd.date_range(f"{day} 09:30", periods=390, freq="1min", tz=NY)
    rows, p = [], p0
    for _ in ts:
        o = p; p = p * (1 + rng.normal(0, 0.001)); rows.append((o, max(o, p) * 1.0003, min(o, p) * 0.9997, p, 1000.0))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=ts)


def setup(tmp, days, syms=("SPY", "AAA", "BBB")):
    rng = np.random.default_rng(3)
    (tmp / "data").mkdir()
    for s in syms:
        df = pd.concat([bars(d.date(), 100 + 10 * syms.index(s), rng) for d in days])
        with gzip.open(tmp / "data" / f"{s}.csv.gz", "wt", newline="") as f:
            df.to_csv(f, index_label="timestamp")
    for k, v in dict(DATA=tmp / "data", NOTES=tmp / "notes", LESSONS=tmp / "lessons.json", LEDGER=tmp / "ledger.csv",
                     WATCH=tmp / "watch.json", STATE=tmp / "state.json", LLM_LOG=tmp / "llm_log.jsonl", ROOT=tmp).items():
        setattr(daily, k, v)
    shutil.copy(ROOT / "config.json", tmp / "config.json")
    return rng


def test_session_day():
    assert daily.session_day(datetime(2026, 10, 29, 16, 25, tzinfo=NY)) == date(2026, 10, 29)
    assert daily.session_day(datetime(2026, 10, 29, 12, 0, tzinfo=NY)) == date(2026, 10, 28)   # today not finished
    assert daily.session_day(datetime(2026, 11, 2, 9, 0, tzinfo=NY)) == date(2026, 10, 30)     # Monday morning -> Friday


def test_data_append_and_forward_test():
    tmp = Path(tempfile.mkdtemp())
    days = pd.bdate_range("2026-07-01", "2026-10-27")
    rng = setup(tmp, days)
    import lab
    assert daily.last_date_in(tmp / "data" / "AAA.csv.gz") == date(2026, 10, 27)
    new_day = date(2026, 10, 28)
    fetch = lambda client, sym, s, e: pd.concat([bars(d.date(), 120, rng) for d in pd.bdate_range(s, e)])
    st = {}
    added = daily.update_data(None, new_day, st, fetch=fetch)
    assert added == {"AAA": 390, "BBB": 390, "SPY": 390} and st["last_data_day"]["AAA"] == "2026-10-28"
    assert daily.update_data(None, new_day, st, fetch=fetch) == {}                       # nothing twice
    sym = lab.load_csv(tmp / "data" / "AAA.csv.gz")                                    # the appended file reads as one
    assert sym.days[-1] == new_day and len(sym.days) == len(days) + 1
    g = lab.grid()
    pick = [i for i, x in enumerate(g) if x[0] == "F3_vwap_pullback" and x[1][-1] == 0][:3]
    (tmp / "watch.json").write_text(json.dumps({"variants": [{"i": i, "name": f"v{i}"} for i in pick]}))
    rows = daily.forward_test(new_day)
    assert rows and all(r["date"] == "2026-10-28" for r in rows)                       # only the new day
    assert daily.forward_test(new_day) == []                                           # never twice
    s = daily.ledger_summary()
    assert s and {"variant", "trades", "days", "avg_r"} <= set(s[0])


def test_lessons_bookkeeping():
    L = []
    L = daily.apply_review(L, {"new_lessons": ["Avoid semis on CPI mornings", "Prefer stocks above VWAP at 9:45", "third"]},
                           date(2026, 10, 28))
    assert [l["id"] for l in L] == ["L1", "L2"]                                        # at most two new a night
    for _ in range(3):
        L = daily.apply_review(L, {"verdicts": {"L1": "contradicted", "L2": "confirmed"}}, date(2026, 10, 29))
    assert L[0]["status"] == "retired" and L[1]["status"] == "active" and L[1]["confirmed"] == 3
    L = daily.apply_review(L, {"verdicts": {"L1": "confirmed", "L99": "confirmed"}}, date(2026, 10, 30))
    assert L[0]["confirmed"] == 0                                                      # retired lessons stay retired
    many = [{"id": f"L{i}", "text": "x", "created": "", "confirmed": 0, "contradicted": 0, "status": "active"} for i in range(12)]
    assert len(daily.apply_review(many, {"new_lessons": ["one more"]}, date(2026, 10, 30))) == 12   # capped at 12 active


class FakeClaude:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []
        self.messages = self

    def create(self, **kw):
        self.prompts.append(kw)
        return NS(content=[NS(type="text", text=self.reply)], usage=NS(input_tokens=4000, output_tokens=300))


def test_review_and_full_run():
    tmp = Path(tempfile.mkdtemp())
    days = pd.bdate_range("2026-08-01", "2026-10-28")
    setup(tmp, days)
    (tmp / "journal.csv").write_text("date,symbol,status,exit_reason,pnl,r\n2026-10-28,MU,closed,flatten,33.55,0.31\n")
    (tmp / "shadow.csv").write_text("date,symbol,in_shortlist,exit_reason,pnl,r\n2026-10-28,MU,yes,flatten,30,0.3\n"
                                    "2026-10-28,AMD,no,stop,-40,-1.1\n")
    (tmp / "daily_inputs").mkdir()
    (tmp / "daily_inputs" / "2026-10-28.json").write_text(json.dumps({"symbols": [{"symbol": "MU"}], "note": "semis"}))
    (tmp / "llm_log.jsonl").write_text(json.dumps({"time": "2026-10-28T09:00:00-04:00", "tag": "premarket", "model": "m",
                                                   "cost_usd": 0.01, "prompt": "p", "response": "picked MU"}) + "\n")
    fake = FakeClaude('{"summary": "Semis led; the shortlist beat the rest.", "verdicts": {}, '
                      '"new_lessons": ["When SK Hynix reports strong, prefer US memory names (MU) at the open"]}')
    reply, cost = daily.claude_review(date(2026, 10, 28), client=fake)
    prompt = fake.prompts[0]["messages"][0]["content"]
    assert "MU closed exit=flatten pnl=33.55" in prompt and "picked MU" in prompt and "AMD(-) stop" in prompt
    assert "SPY" in prompt.split("MARKET:")[1].split("\n")[0]                          # market move included
    L = json.loads((tmp / "lessons.json").read_text())
    assert L[0]["text"].startswith("When SK Hynix") and (tmp / "notes" / "2026-10-28.md").exists()
    assert json.loads((tmp / "llm_log.jsonl").read_text().splitlines()[-1])["tag"] == "daily-review" and cost > 0
    # the whole run: data step (no new day to add), review, state, a message; and never twice for one day
    real = daily.claude_review
    daily.claude_review = lambda day, **k: (json.loads(fake.reply), 0.02)
    try:
        sent = []
        rep = daily.run(now=datetime(2026, 10, 28, 16, 30, tzinfo=NY), client=object(), alert=sent.append)
        assert rep["summary"].startswith("Semis") and sent and sent[0].startswith("Daily learning, 2026-10-28")
        assert daily.run(now=datetime(2026, 10, 28, 17, 0, tzinfo=NY), client=object())["skipped"] == "already done"
        # a day with no session at all (no data, no journal, no control rows) is skipped, not "learned"
        rep = daily.run(now=datetime(2026, 11, 26, 17, 0, tzinfo=NY), client=object())
        assert "nothing to learn" in rep["note"]
    finally:
        daily.claude_review = real


def test_lessons_reach_the_morning_prompt():
    tmp = Path(tempfile.mkdtemp()) / "lessons.json"
    tmp.write_text(json.dumps([{"id": "L1", "text": "weak one", "confirmed": 0, "contradicted": 1, "status": "active"},
                               {"id": "L2", "text": "strong one", "confirmed": 5, "contradicted": 0, "status": "active"},
                               {"id": "L3", "text": "retired one", "confirmed": 0, "contradicted": 4, "status": "retired"}]))
    blk = advisor.lessons_block(tmp)
    assert blk.index("strong one") < blk.index("weak one") and "retired one" not in blk and "never change the risk" in blk
    cfg = json.loads((ROOT / "config.json").read_text())
    p = advisor.build_premarket_prompt(datetime(2026, 10, 29, 9, 0, tzinfo=NY), {}, [], cfg, lessons=blk)
    assert "strong one" in p and p.rstrip().endswith("Choose today's shortlist.")
    assert advisor.lessons_block(Path(tempfile.mkdtemp()) / "none.json") == ""         # no file: nothing added


def test_sip_request_never_reaches_the_last_15_minutes():
    from datetime import date as D
    now = datetime(2026, 10, 29, 16, 20, tzinfo=daily.NY)
    assert daily.sip_end(D(2026, 10, 29), now) == datetime(2026, 10, 29, 16, 4, tzinfo=daily.NY)
    assert daily.sip_end(D(2026, 10, 28), now) == datetime(2026, 10, 29, 0, 0, tzinfo=daily.NY)
