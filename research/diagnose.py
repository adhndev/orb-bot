import json
import numpy as np
import sim
raw, trend = sim.load(sim.HERE / "train" if (sim.HERE / "train").exists() else sim.PROJ / "bt_cache", last_year=2020)
days = {d: sim.Day(d, g) for d, g in raw.items()}
rows = json.load(open("search_results.json"))
for r in rows:
    r["p"]["target"] = None if r["p"]["target"] in (None, "None") else float(r["p"]["target"])
rows.sort(key=lambda r: r["train"].get("avg_r", -9), reverse=True)
current = dict(or_min=15, side="long", stop="mid", target=2.0, trend=False)
picks = [current] + [r["p"] for r in rows[:5]]
def costs(on):
    sim.BT["slippage_bps"], sim.BT["min_slippage_usd"] = (3, 0.01) if on else (0, 0.0)
    sim.engine.FEE_DEFAULTS.update({"sec_per_million": 20.60 if on else 0, "taf_per_share": 0.000166 if on else 0})
print("2016-2020 (train + validation together), QQQ\n")
print("%-62s %5s %8s %8s %9s %8s" % ("rules", "n", "avgR", "noCost", "coinflip", "beats%"))
for p in picks:
    costs(True)
    t = sim.run(days, trend, p); s = sim.summary(t)
    cf = sim.coin_flip(days, t, p, n_runs=300)
    costs(False)
    g = sim.summary(sim.run(days, trend, p))
    cf0 = sim.coin_flip(days, sim.run(days, trend, p), p, n_runs=300)
    costs(True)
    name = ("CURRENT " if p == current else "") + "OR%d %s stop=%s tgt=%s trend=%s" % (p["or_min"], p["side"], p["stop"], p["target"], p["trend"])
    print("%-62s %5d %+8.3f %+8.3f %+9.3f %7.0f%%" % (name, s["n"], s["avg_r"], g["avg_r"], cf.mean(), 100 * (cf < s["avg_r"]).mean()))
    print("%62s %5s %8s %8s %+9.3f %7.0f%%   (no-cost: strategy vs no-cost coin flip)" % ("", "", "", "", cf0.mean(), 100 * (cf0 < g["avg_r"]).mean()))
