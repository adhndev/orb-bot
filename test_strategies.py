"""The live gap-down rules must match research/lab.py's F5_gap, on real data (discovery years)."""
import sys
from datetime import date
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "research"))
import lab, strategies as S

DATA = ROOT / "research" / "data"


def test_gap_rules_match_research_engine():
    files = [DATA / f"{s}.csv.gz" for s in ("AMD", "TSLA", "NVDA", "AAPL")]
    files = [f for f in files if f.exists()]
    if not files:
        print("research data not present: skipped"); return
    checked = 0
    for f in files:
        full = lab.load_csv(f).window(date(2016, 1, 1), date(2021, 12, 31))
        p = (2.0, -1, 0, 0.2, 1, 0, 1)                       # F5_gap(2.0, -1, 0, 0.2, 1, 0, 1): the candidate
        d, m, side, price, stop, tgt, ht = lab.signals(full, "F5_gap", p)
        for k in range(min(len(d), 40)):
            i = int(d[k])
            hist = full.window(full.days[max(0, i - 40)], full.days[i - 1])
            facts = S.daily_facts(hist)
            assert abs(facts["prev_close"] - full.prev_close[i]) < 1e-6 and abs(facts["atr"] - full.atr[i]) < 1e-6
            sig = S.gap_signal(facts, full.O[i, 0], full.C[i, 0])
            assert sig is not None and abs(sig["entry"] - price[k]) < 1e-9
            assert abs(sig["stop"] - stop[k]) < 0.0101 and sig["stop"] <= stop[k] + 1e-9   # whole cents, never closer
            assert abs(sig["target"] - tgt[k]) < 0.0201
            checked += 1
        # and no live signal on days the research engine skipped
        sigdays = set(int(x) for x in d)
        for i in range(30, min(len(full.days), 400)):
            if i in sigdays or np.isnan(full.atr[i]):
                continue
            hist = full.window(full.days[max(0, i - 40)], full.days[i - 1])
            sig = S.gap_signal(S.daily_facts(hist), full.O[i, 0], full.C[i, 0])
            assert sig is None, (f.name, full.days[i])
    print("gap rules match the research engine on", checked, "signals")


def test_gap_signal_edges():
    facts = {"prev_close": 100.0, "atr": 3.0}
    assert S.gap_signal(facts, 98.5, 98.4) is None                         # -1.5 %: not enough
    s = S.gap_signal(facts, 97.9, 98.0)
    assert s and s["gap"] == -2.1 and s["stop"] == 97.4 and s["target"] == 98.6 and abs(s["risk"] - 0.6) < 1e-9
    assert S.gap_signal(facts, 102.5, 102.6) is None                       # gap UP: never
    assert S.gap_signal({"prev_close": 100.0, "atr": 0.1}, 97.0, 97.0) is None   # stop too tight
    assert S.gap_reprice(s, 98.2)["risk"] == 98.2 - 97.4 and S.gap_reprice(s, 98.2)["target"] == 98.6
    assert S.gap_reprice(s, 97.4) is None and S.gap_reprice(s, 98.6) is None
    assert S.gap_reprice(s, None) is s
