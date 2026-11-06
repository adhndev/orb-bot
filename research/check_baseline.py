"""The fast simulator must reproduce engine.py (the backtester) trade for trade on the bot's current rules."""
import sys, time
import sim
raw, trend = sim.load(sim.HERE / "train" if (sim.HERE / "train").exists() else sim.PROJ / "bt_cache", last_year=2020)
days = {d: sim.Day(d, g) for d, g in raw.items()}
base = dict(or_min=15, side="long", stop="mid", target=2.0, trend=False)
t0 = time.time(); fast = sim.run(days, trend, base); t1 = time.time()
slow = []
for d, g in raw.items():
    slow += sim.engine.simulate_day({"QQQ": g}, sim.CFG)
t2 = time.time()
print(f"fast {len(fast)} trades in {t1-t0:.1f}s | engine {len(slow)} trades in {t2-t1:.1f}s")
assert len(fast) == len(slow)
bad = 0
for a, e in zip(fast, slow):
    if str(a["date"]) != e["date"] or a["qty"] != e["qty"] or abs(a["r"] - e["r"]) > 0.002 or a["reason"] != e["exit_reason"]:
        bad += 1
        if bad < 5: print("MISMATCH", a, e)
print("mismatches:", bad)
import collections
by = collections.defaultdict(list)
for t in fast: by[t["date"].year].append(t["r"])
print({y: (len(v), round(sum(v)/len(v),3)) for y, v in sorted(by.items())})
