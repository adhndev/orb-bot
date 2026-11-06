"""Checks for lab.py. Uses only discovery-period data (QQQ 2016-2020)."""
import time
import numpy as np
import pytest
import lab, sim

pytestmark = pytest.mark.skipif(not (sim.HERE / "train").is_dir() or not any((sim.HERE / "train").iterdir()),
                                reason="needs the QQQ 1-minute data in research/train (not stored in the repository)")

def test_kernel_matches_engine():
    raw, trend = sim.load(sim.HERE / "train", last_year=2020)
    days = {d: sim.Day(d, g) for d, g in raw.items()}
    base = dict(or_min=15, side="long", stop="mid", target=2.0, trend=False)
    ref = sim.run(days, trend, base)                               # = engine.py, verified trade by trade
    q = lab.load_months(sim.HERE / "train", "QQQ", last_year=2020)
    pos = {d: i for i, d in enumerate(q.days)}
    d_idx = np.array([pos[t["date"]] for t in ref]); m_idx = np.array([t["signal_i"] for t in ref])
    side = np.ones(len(ref), np.int64)
    price = np.array([float(days[t["date"]].c[t["signal_i"]]) for t in ref])
    stop = np.array([price[i] - t["risk_ps"] for i, t in enumerate(ref)])
    # rebuild the exact stop the bot used: floor(mid)
    stop = np.round(stop, 2)
    target = np.array([round(float(price[i]) + 2.0 * float(price[i] - stop[i]), 2) for i in range(len(ref))])
    sig = (d_idx, m_idx, side, price, stop, target, np.ones(len(ref), bool))
    res = lab.results(q, sig)
    assert len(res["r"]) == len(ref), (len(res["r"]), len(ref))
    diff = np.abs(res["r"] - np.array([t["r"] for t in ref]))
    reasons = {1: "stop", 2: "target", 3: "flatten", 4: "data_end"}
    same_reason = sum(reasons[int(a)] == t["reason"] for a, t in zip(res["reason"], ref))
    print(f"kernel vs engine: {len(ref)} trades, max |R diff| {diff.max():.4f}, same exit type {same_reason}/{len(ref)}")
    assert same_reason == len(ref) and diff.max() < 0.02

def test_families_run_and_never_look_ahead():
    q = lab.load_months(sim.HERE / "train", "QQQ", last_year=2016)
    spy = q                                                     # shape stand-in for the RS family
    sr = lab.spy_returns(spy, q)
    g = lab.grid()
    print("variants:", len(g), {f: sum(1 for x in g if x[0] == f) for f in sorted({x[0] for x in g})})
    t0 = time.time()
    for fam, p, win in g:
        s = lab.signals(q, fam, p, sr)
        d, m, side, price, stop, tgt, ht = s
        assert len(set(d.tolist())) == len(d)                    # one signal per day
        if len(d):
            assert ((m >= win[0]) & (m <= lab.LAST_SIG)).all(), (fam, p)
            assert np.allclose(price, q.C[d, m])                 # acted on the close of the signal bar itself
            assert ((price - stop) * side > 0).all()             # stop on the losing side
            assert (((tgt - price) * side > 0) | ~ht).all()      # target on the winning side
            if p[-1] == 1:
                assert (side == 1).all()                         # long-only really is long only
    print(f"all {len(g)} variants on 1 year of QQQ in {time.time()-t0:.1f}s")

def test_no_future_in_daily_facts():
    q = lab.load_months(sim.HERE / "train", "QQQ", last_year=2016)
    i = 30
    full_day_close = q.C[i - 1][~np.isnan(q.C[i - 1])][-1]
    assert q.prev_close[i] == full_day_close                     # yesterday's close, not today's
    q2 = q.window(q.days[0], q.days[i])                          # cutting off the future changes nothing before it
    assert np.allclose(q2.atr[:i + 1], q.atr[:i + 1], equal_nan=True) and (q2.trend == q.trend[:i + 1]).all()

if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("lab checks passed")
