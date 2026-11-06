"""Study 2 (PROTOCOL_2.md): choose on QQQ 2020-01..2026-09 only."""
import itertools, json
from datetime import date
import sim
raw, trend = sim.load(sim.HERE / "qqq2020" if (sim.HERE / "qqq2020").exists() else sim.PROJ / "bt_cache")              # Dec 2019 loaded only to warm up the 20-day trend filter
days = {d: sim.Day(d, g) for d, g in raw.items() if d >= date(2020, 1, 1)}
h1 = {d: v for d, v in days.items() if d.year <= 2022}
h2 = {d: v for d, v in days.items() if d.year >= 2023}
rows = []
for or_min, side, stop, target, trd in itertools.product((5, 15, 30), ("long", "both"), ("mid", "far"), (1.0, 2.0, 3.0, None), (False, True)):
    p = dict(or_min=or_min, side=side, stop=stop, target=target, trend=trd)
    rows.append({"p": p, "all": sim.summary(sim.run(days, trend, p)),
                 "h1": sim.summary(sim.run(h1, trend, p)), "h2": sim.summary(sim.run(h2, trend, p))})
json.dump(rows, open("search2_results.json", "w"), default=str, indent=1)
k = lambda r: r["all"].get("avg_r", -9)
rows.sort(key=k, reverse=True)
print("QQQ 2020-01..2026-09, %d days. 96 variants: best avg R %+.3f, positive overall: %d" % (len(days), k(rows[0]), sum(k(r) > 0 for r in rows)))
print("\nTop 8 overall:")
for r in rows[:8]:
    print("  %-55s n=%4d all %+.3f | 2020-22 %+.3f | 2023-26 %+.3f" % (
        "OR%d %s stop=%s tgt=%s trend=%s" % tuple(r["p"].values()), r["all"]["n"], r["all"]["avg_r"], r["h1"]["avg_r"], r["h2"]["avg_r"]))
ok = [r for r in rows if r["all"].get("n", 0) >= 150 and r["h1"].get("avg_r", -1) > 0 and r["h2"].get("avg_r", -1) > 0]
pick = max(ok, key=k) if ok else None
print("\nQualified (>=150 trades, positive in both halves): %d" % len(ok))
print("PICK:", pick["p"] if pick else "NONE")
json.dump(pick, open("pick2.json", "w"), default=str, indent=1)
