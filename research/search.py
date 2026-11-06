import itertools, json, sys
from datetime import date
import numpy as np
import sim
raw, trend = sim.load(sim.HERE / "train" if (sim.HERE / "train").exists() else sim.PROJ / "bt_cache", last_year=2020)
days = {d: sim.Day(d, g) for d, g in raw.items()}
assert max(days) <= date(2020, 12, 31), "test data must not be loaded here"
tr = {d: v for d, v in days.items() if d.year <= 2018}
va = {d: v for d, v in days.items() if d.year in (2019, 2020)}
rows = []
for or_min, side, stop, target, trd in itertools.product((5, 15, 30), ("long", "both"), ("mid", "far"), (1.0, 2.0, 3.0, None), (False, True)):
    p = dict(or_min=or_min, side=side, stop=stop, target=target, trend=trd)
    a, v = sim.summary(sim.run(tr, trend, p)), sim.summary(sim.run(va, trend, p))
    rows.append({"p": p, "train": a, "val": v})
json.dump(rows, open("search_results.json", "w"), default=str, indent=1)
key = lambda x: x["train"].get("avg_r", -9)
print("all 96, train avg R: best %.3f, median %.3f, worst %.3f; positive: %d" % (
    max(map(key, rows)), float(np.median([key(r) for r in rows])), min(map(key, rows)), sum(key(r) > 0 for r in rows)))
cands = [r for r in rows if r["train"].get("n", 0) >= 100 and r["train"]["avg_r"] > 0]
cands.sort(key=key, reverse=True)
print("\nstep 1: %d variants with >=100 train trades and train avg R > 0" % len(cands))
top = cands[:5]
for r in top:
    print("  ", r["p"], "| train n=%s avgR=%s | val n=%s avgR=%s" % (r["train"]["n"], r["train"]["avg_r"], r["val"].get("n"), r["val"].get("avg_r")))
ok = [r for r in top if r["val"].get("n", 0) >= 60 and r["val"]["avg_r"] > 0]
pick = max(ok, key=lambda r: r["val"]["avg_r"]) if ok else None
print("\nPICK:", pick["p"] if pick else "NONE - no variant passed train and validation")
json.dump(pick, open("pick.json", "w"), default=str, indent=1)
