"""
study3.py - runs PROTOCOL_3.md.

    python research\\study3.py choose        # discovery + luck test + validation -> pick3.json (and its lock)
    python research\\study3.py test          # the single run on 2024-01..2026-09, only after pick3.json exists

Reads research\\data\\*.csv.gz (made by fetch_data.py). Writes study3_*.json/.txt next to this file.
"""
import hashlib
import json
import sys
import time
from datetime import date
from multiprocessing import Pool
from pathlib import Path

import numpy as np

import lab

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DISC = (date(2016, 1, 1), date(2021, 12, 31))
VAL = (date(2022, 1, 1), date(2023, 12, 31))
TEST = (date(2024, 1, 1), date(2026, 9, 30))
GRID = lab.grid()
MIN_DISC_TRADES, MIN_VAL_TRADES, SHORTLIST = 300, 100, 10


def load(name, window):
    full = lab.load_csv(DATA / f"{name}.csv.gz", name)
    return full.window(*window)


def _symbol_job(args):
    name, window, variants = args
    sym = load(name, window)
    spy = load("SPY", window) if (DATA / "SPY.csv.gz").exists() else None
    sr = lab.spy_returns(spy, sym) if spy is not None else None
    out = []
    for i in variants:
        fam, p, _ = GRID[i]
        res = lab.results(sym, lab.signals(sym, fam, p, sr))
        out.append(None if res is None else {"date": res["date"], "r": res["r"]})
    return name, out


def run(window, variants, symbols, procs=2, log=print):
    """(len(variants) x days) matrices of daily R sums and trade counts, all symbols pooled."""
    per_var = [[] for _ in variants]
    t0 = time.time()
    with Pool(procs) as pool:
        for k, (name, out) in enumerate(pool.imap_unordered(_symbol_job, [(s, window, variants) for s in symbols]), 1):
            for v, res in enumerate(out):
                per_var[v].append(res)
            log(f"  {k}/{len(symbols)} {name} done ({time.time() - t0:.0f} s)")
    days = sorted({d for sets in per_var for res in sets if res is not None for d in res["date"]})
    S, N = lab.day_matrix(per_var, days)
    return S, N, days


def name(i):
    fam, p, _ = GRID[i]
    return f"{fam}{p}"


def choose(symbols):
    rep = []
    say = lambda x="": (print(x), rep.append(x))
    say(f"STUDY 3 - choose. {len(GRID)} variants x {len(symbols)} stocks. Discovery {DISC[0]}..{DISC[1]}")
    S, N, days = run(DISC, list(range(len(GRID))), symbols)
    n, avg, t = lab.stats_from_days(S, N)
    disc = [{"i": i, "name": name(i), "trades": int(n[i]), "avg_r": None if np.isnan(avg[i]) else round(float(avg[i]), 4),
             "t": round(float(t[i]), 3)} for i in range(len(GRID))]
    json.dump(disc, open(HERE / "study3_discovery.json", "w"), indent=0)
    elig = (n >= MIN_DISC_TRADES)
    say(f"\nDiscovery: {len(days)} days. Variants with >= {MIN_DISC_TRADES} trades: {int(elig.sum())}. "
        f"Positive avg R after costs: {int(((avg > 0) & elig).sum())}")
    fams = sorted({g[0] for g in GRID})
    for f in fams:
        ix = [i for i in range(len(GRID)) if GRID[i][0] == f and elig[i]]
        if ix:
            b = max(ix, key=lambda i: t[i])
            say(f"  {f:18s} best: avg R {avg[b]:+.3f}, t {t[b]:+.2f}, {int(n[b])} trades | family median avg R "
                f"{np.median(avg[ix]):+.3f}")
    watch = [int(i) for i in np.argsort(-t) if elig[i]][:SHORTLIST]       # forward-tested every night from now on
    p, t_best, _ = lab.reality_check(S, n_boot=1000, eligible=elig)
    say(f"\nLuck test (White's Reality Check, {int(elig.sum())} variants): best t {t_best:.2f}, p = {p:.3f}")
    result = {"protocol_sha256": hashlib.sha256((HERE / "PROTOCOL_3.md").read_bytes()).hexdigest(),
              "luck_p": p, "best_t": t_best, "pick": None}
    if p >= 0.05:
        say("STOP: the best variant is not better than luck across this many tries. Nothing goes to validation.")
    else:
        short = [i for i in np.argsort(-t) if elig[i] and avg[i] > 0][:SHORTLIST]
        say(f"\nShortlist ({len(short)}):")
        for i in short:
            say(f"  {name(i)}  disc avg R {avg[i]:+.3f}  t {t[i]:+.2f}  n {int(n[i])}")
        Sv, Nv, vdays = run(VAL, list(short), symbols)
        nv, av, tv = lab.stats_from_days(Sv, Nv)
        say(f"\nValidation {VAL[0]}..{VAL[1]}:")
        ok = []
        for k, i in enumerate(short):
            passed = nv[k] >= MIN_VAL_TRADES and av[k] > 0 and tv[k] > 1
            say(f"  {name(i)}  val avg R {av[k]:+.3f}  t {tv[k]:+.2f}  n {int(nv[k])}  {'PASS' if passed else 'fail'}")
            if passed:
                ok.append((av[k], int(i)))
        if ok:
            best = max(ok)[1]
            result["pick"] = {"i": best, "name": name(best), "family": GRID[best][0], "params": list(GRID[best][1]),
                              "window": list(GRID[best][2])}
            say(f"\nPICK: {name(best)}")
        else:
            say("\nSTOP: no shortlisted variant held up on 2022-2023.")
    if result["pick"] and result["pick"]["i"] not in watch:
        watch.append(result["pick"]["i"])
    (HERE / "forward_watch.json").write_text(json.dumps({"written": str(date.today()), "why": (
        "The 10 best discovery variants (and the pick, if any), tracked on every new day by daily.py. New days are data "
        "none of them was chosen on."), "variants": [{"i": i, "name": name(i)} for i in watch]}, indent=2))
    (HERE / "pick3.json").write_text(json.dumps(result, indent=2, default=float))
    (HERE / "pick3.lock").write_text(hashlib.sha256((HERE / "pick3.json").read_bytes()).hexdigest())
    (HERE / "study3_choose.txt").write_text("\n".join(rep) + "\n", encoding="utf-8")


def coin_flip(sym, res, win, both_sides, rng):
    """Same stock and days, same stop distance and target distance, random minute (and side for two-sided rules)."""
    n = len(res["r"])
    d = res["d"].copy()
    m = rng.integers(win[0], lab.LAST_SIG + 1, n)
    for k in range(n):                                    # move off minutes with no trade
        tries = 0
        while np.isnan(sym.C[d[k], m[k]]) and tries < 50:
            m[k] = rng.integers(win[0], lab.LAST_SIG + 1); tries += 1
    side = rng.choice([-1, 1], n) if both_sides else np.ones(n, np.int64)
    price = sym.C[d, m]
    stop = price - side * res["risk"]
    has_t = ~np.isnan(res["reward"])
    target = np.where(has_t, price + side * np.nan_to_num(res["reward"]), 0.0)
    keep = ~np.isnan(price)
    r = lab.results(sym, (d[keep], m[keep], side[keep].astype(np.int64), price[keep], stop[keep], target[keep], has_t[keep]))
    return r["r"] if r else np.zeros(0)


def test(symbols, runs=500):
    pick = json.loads((HERE / "pick3.json").read_text())
    if hashlib.sha256((HERE / "pick3.json").read_bytes()).hexdigest() != (HERE / "pick3.lock").read_text().strip():
        sys.exit("pick3.json changed after it was locked: the test would mean nothing.")
    if not pick.get("pick"):
        sys.exit("No strategy was picked, so there is nothing to test (that is the result).")
    i = pick["pick"]["i"]
    fam, p, win = GRID[i]
    both = p[-1] == 0
    rep = []
    say = lambda x="": (print(x), rep.append(x))
    say(f"STUDY 3 - TEST (run once). {name(i)} on {TEST[0]}..{TEST[1]}, {len(symbols)} stocks")
    rng = np.random.default_rng(2026)
    all_r, flips = [], np.zeros((runs, 0)).tolist()
    flip_sum, flip_n = np.zeros(runs), np.zeros(runs)
    spy = load("SPY", TEST)
    for s in symbols:
        sym = load(s, TEST)
        res = lab.results(sym, lab.signals(sym, fam, tuple(p), lab.spy_returns(spy, sym)))
        if res is None or not len(res["r"]):
            continue
        all_r.append(res["r"])
        for b in range(runs):
            fr = coin_flip(sym, res, win, both, rng)
            flip_sum[b] += fr.sum(); flip_n[b] += len(fr)
    r = np.concatenate(all_r) if all_r else np.zeros(0)
    if not len(r):
        say("No trades in the test period.")
    else:
        rng2 = np.random.default_rng(1)
        boots = r[rng2.integers(0, len(r), (5000, len(r)))].mean(1)
        flip = flip_sum / np.maximum(flip_n, 1)
        p_pos, beats = float((boots > 0).mean()), float((flip < r.mean()).mean())
        say(f"Trades {len(r)} | avg R {r.mean():+.3f} | win rate {(r > 0).mean():.0%} | "
            f"chance true avg R > 0: {p_pos:.0%} | random entries avg {flip.mean():+.3f} | beats random in {beats:.0%}")
        passed = r.mean() > 0 and p_pos >= 0.90 and beats >= 0.95
        say("VERDICT: " + ("PASS - beats a coin flip on years it never saw. Next: paper trade it." if passed else
                           "FAIL - did not beat a coin flip on the unseen years."))
    (HERE / "study3_test.txt").write_text("\n".join(rep) + "\n", encoding="utf-8")


def main():
    symbols = sorted(p.name.split(".")[0] for p in DATA.glob("*.csv.gz"))
    if not symbols:
        sys.exit("No data yet: run run_fetch_data.bat first.")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "choose":
        choose(symbols)
    elif cmd == "test":
        test(symbols)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
