"""
lab.py - study 3 engine (PROTOCOL_3.md): many intraday strategy families, all day, many stocks, after costs.

Bars are held as day x minute arrays (390 minutes, 9:30 = minute 0). A signal on minute m is acted on at the OPEN of
minute m+1 plus slippage, exactly like the bot and engine.py; stops are assumed hit before targets in the same bar;
a bar that opens through the stop fills at its open; everything is flat at 15:55 (minute 385).
"""
import math
from pathlib import Path

import numpy as np

try:
    import numba as nb
except ImportError:                       # without numba everything still works, just slower (fine for one day)
    class _NoNumba:
        @staticmethod
        def njit(*a, **k):
            if a and callable(a[0]):
                return a[0]
            return lambda f: f
    nb = _NoNumba()
import pandas as pd

NY = "America/New_York"
N_MIN = 390
FLAT_M = 385                 # 15:55
LAST_SIG = 358               # signal bar ends by 15:29, filled by 15:30 at the latest
SLIP_BPS, MIN_SLIP = 3.0, 0.01
SEC_PER_M, TAF, TAF_MAX = 20.60, 0.000166, 8.30
MAX_LOSS, POS_CAP = 250.0, 10000.0


# ---------------------------------------------------------------- data
class Sym:
    """One stock: O/H/L/C/V/VW as (days x 390) arrays (NaN where no trade), plus what is known before each open."""

    def __init__(self, name, df):
        self.name = name
        df = df[~df.index.duplicated()]
        day = df.index.date
        days = np.array(sorted(set(day)))
        pos = {d: i for i, d in enumerate(days)}
        di = np.fromiter((pos[d] for d in day), int, len(df))
        mi = (df.index.hour * 60 + df.index.minute).to_numpy() - 570
        keep = (mi >= 0) & (mi < N_MIN)
        di, mi = di[keep], mi[keep]
        shape = (len(days), N_MIN)
        arrs = {}
        for k in ("open", "high", "low", "close", "volume"):
            a = np.full(shape, np.nan)
            a[di, mi] = df[k].to_numpy(float)[keep]
            arrs[k] = a
        self.days, self.O, self.H, self.L, self.C, self.V = days, arrs["open"], arrs["high"], arrs["low"], arrs["close"], arrs["volume"]
        self.splits = adjust_splits(self)
        tp = (self.H + self.L + self.C) / 3
        pv = np.nan_to_num(tp * self.V)
        vv = np.nan_to_num(self.V)
        cv = np.cumsum(vv, 1)
        with np.errstate(invalid="ignore", divide="ignore"):
            self.VW = np.where(cv > 0, np.cumsum(pv, 1) / cv, np.nan)
        # daily facts, each known BEFORE that day's open
        d_open = np.array([r[~np.isnan(r)][0] if (~np.isnan(r)).any() else np.nan for r in self.O])
        d_close = np.array([r[~np.isnan(r)][-1] if (~np.isnan(r)).any() else np.nan for r in self.C])
        d_high, d_low = np.nanmax(self.H, 1), np.nanmin(self.L, 1)
        prev_c = np.r_[np.nan, d_close[:-1]]
        tr = np.nanmax(np.vstack([d_high - d_low, np.abs(d_high - prev_c), np.abs(d_low - prev_c)]), 0)
        atr = pd.Series(tr).rolling(14).mean().shift(1).to_numpy()
        sma = pd.Series(d_close).rolling(20).mean().shift(1).to_numpy()
        self.open_px, self.prev_close = d_open, prev_c
        self.prev_high, self.prev_low = np.r_[np.nan, d_high[:-1]], np.r_[np.nan, d_low[:-1]]
        self.atr = atr
        self.trend = np.where(np.isnan(sma) | np.isnan(prev_c), 0, np.where(prev_c > sma, 1, -1)).astype(np.int64)

    def window(self, start, end):
        """A new Sym view holding only days in [start, end] (dates)."""
        k = (self.days >= start) & (self.days <= end)
        s = object.__new__(Sym)
        s.name = self.name
        for a in ("days", "O", "H", "L", "C", "V", "VW", "open_px", "prev_close", "prev_high", "prev_low", "atr", "trend"):
            setattr(s, a, getattr(self, a)[k])
        return s


SPLIT_RATIOS = (2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 40, 50)


def adjust_splits(sym):
    """Alpaca's minute bars are not split-adjusted. A split shows up as an overnight jump to almost exactly 1/N (or N
    for a reverse split) of the prior close. Detect those and scale every earlier day, so gaps, ATR and the trend
    filter see real moves only. Returns [(date, ratio)]."""
    def first(r):
        k = ~np.isnan(r)
        return r[k][0] if k.any() else np.nan

    def last(r):
        k = ~np.isnan(r)
        return r[k][-1] if k.any() else np.nan
    found = []
    for i in range(1, len(sym.days)):
        pc, op = last(sym.C[i - 1]), first(sym.O[i])
        if not (pc > 0 and op > 0):
            continue
        r = pc / op
        for n in SPLIT_RATIOS:
            for ratio in (n, 1.0 / n):
                if abs(r / ratio - 1) < 0.06:              # within 6 % of an exact split ratio
                    found.append((sym.days[i], ratio))
                    sym.O[:i] /= ratio; sym.H[:i] /= ratio; sym.L[:i] /= ratio; sym.C[:i] /= ratio
                    sym.V[:i] *= ratio
                    break
            else:
                continue
            break
    return found


def load_csv(path, name=None):
    df = pd.read_csv(path, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(NY)
    return Sym(name or Path(path).name.split(".")[0], df.sort_index())


def load_months(folder, sym, last_year=None):
    parts = []
    for f in sorted(Path(folder).glob(f"{sym}_sip_*.csv")):
        if last_year and int(f.stem.split("_")[-1][:4]) > last_year:
            continue
        parts.append(pd.read_csv(f, index_col=0))
    df = pd.concat(parts)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(NY)
    return Sym(sym, df.sort_index())


# ---------------------------------------------------------------- trade simulation (compiled)
@nb.njit(cache=True)
def _slip(px):
    s = px * SLIP_BPS / 10000.0
    return s if s > MIN_SLIP else MIN_SLIP


@nb.njit(cache=True)
def simulate(O, H, L, C, d_idx, m_idx, side, stop, target, has_t):
    """Returns (entry, exit, reason) per signal; reason 0 skipped, 1 stop, 2 target, 3 flatten, 4 data end."""
    n = len(d_idx)
    entry = np.full(n, np.nan)
    exitp = np.full(n, np.nan)
    reason = np.zeros(n, np.int8)
    for k in range(n):
        d, f, s = d_idx[k], m_idx[k] + 1, side[k]
        if f >= N_MIN or np.isnan(O[d, f]):
            continue
        fo = O[d, f]
        e = fo + s * _slip(fo)
        entry[k] = e
        st, tg, ht = stop[k], target[k], has_t[k]
        done = False
        for j in range(f, N_MIN):
            o, h, l = O[d, j], H[d, j], L[d, j]
            if np.isnan(o):
                continue
            if j >= FLAT_M:
                exitp[k] = o - s * _slip(o); reason[k] = 3; done = True; break
            if s == 1:
                if j > f:
                    if o <= st:
                        exitp[k] = o - _slip(o); reason[k] = 1; done = True; break
                    if ht and o >= tg:
                        exitp[k] = o; reason[k] = 2; done = True; break
                if l <= st:
                    exitp[k] = st - _slip(st); reason[k] = 1; done = True; break
                if ht and h >= tg:
                    exitp[k] = tg; reason[k] = 2; done = True; break
            else:
                if j > f:
                    if o >= st:
                        exitp[k] = o + _slip(o); reason[k] = 1; done = True; break
                    if ht and o <= tg:
                        exitp[k] = o; reason[k] = 2; done = True; break
                if h >= st:
                    exitp[k] = st + _slip(st); reason[k] = 1; done = True; break
                if ht and l <= tg:
                    exitp[k] = tg; reason[k] = 2; done = True; break
        if not done:
            for j in range(N_MIN - 1, f - 1, -1):
                if not np.isnan(C[d, j]):
                    exitp[k] = C[d, j] - s * _slip(C[d, j]); reason[k] = 4; break
    return entry, exitp, reason


def results(sym, sig):
    """Simulate a signal set and return per-trade arrays with R after slippage and fees (sized like the bot)."""
    d, m, side, price, stop, target, has_t = sig
    if len(d) == 0:
        return None
    entry, exitp, reason = simulate(sym.O, sym.H, sym.L, sym.C, d, m, side, stop, target, has_t)
    ok = reason > 0
    risk = np.abs(price - stop)
    qty = np.floor(np.minimum(MAX_LOSS / risk, POS_CAP / price))
    ok &= qty >= 1
    sale = np.where(side == 1, exitp, entry)
    fees = np.ceil(np.round((sale * qty * SEC_PER_M / 1e6 + np.minimum(qty * TAF, TAF_MAX)) * 100, 6)) / 100
    pnl = side * (exitp - entry) * qty - fees
    r = pnl / (qty * risk)
    k = ok
    return {"d": d[k], "m": m[k], "side": side[k], "r": r[k], "risk": risk[k], "price": price[k],
            "reward": np.where(has_t[k], np.abs(target[k] - price[k]), np.nan), "reason": reason[k],
            "date": sym.days[d[k]]}


# ---------------------------------------------------------------- strategy families (compiled signal finders)
# Each returns arrays (d, m, side, price, stop, target, has_target): at most ONE signal per day (the first).
@nb.njit(cache=True)
def _side_ok(s, tr, use_trend, long_only):
    if long_only and s != 1:
        return False
    if use_trend and s != tr:
        return False
    return True


@nb.njit(cache=True)
def _mk(price, s, stop, rr, risk_min=0.05):
    """Validate a stop; build an R-multiple target (rr <= 0 means no target)."""
    risk = (price - stop) * s
    if risk < risk_min or risk < price * 0.001:
        return False, 0.0, False
    if rr > 0:
        return True, price + s * rr * risk, True
    return True, 0.0, False


@nb.njit(cache=True)
def _alloc(n):
    return (np.zeros(n, np.int64), np.zeros(n, np.int64), np.zeros(n, np.int64), np.zeros(n), np.zeros(n),
            np.zeros(n), np.zeros(n, np.bool_))


@nb.njit(cache=True)
def f_orb(O, H, L, C, atr, trend, or_n, stop_mode, rr, use_trend, long_only, win_end):
    D = C.shape[0]
    dd, mm, ss, pp, st, tg, ht = _alloc(D)
    n = 0
    for d in range(D):
        hi, lo, cnt = -1e18, 1e18, 0
        for j in range(or_n):
            if not np.isnan(H[d, j]):
                hi = max(hi, H[d, j]); lo = min(lo, L[d, j]); cnt += 1
        if cnt < or_n // 2 + 1 or hi <= lo or np.isnan(atr[d]):
            continue
        rng, mid = hi - lo, (hi + lo) / 2
        for m in range(or_n, min(win_end, LAST_SIG) + 1):
            c = C[d, m]
            if np.isnan(c):
                continue
            s = 0
            if c > hi and c - hi <= 0.5 * rng:
                s = 1
            elif c < lo and lo - c <= 0.5 * rng:
                s = -1
            if s == 0 or not _side_ok(s, trend[d], use_trend, long_only):
                continue
            if stop_mode == 0:
                sp = math.floor(mid * 100) / 100 if s == 1 else math.ceil(mid * 100) / 100
            elif stop_mode == 1:
                sp = lo if s == 1 else hi
            else:
                sp = c - s * 0.25 * atr[d]
            ok, t, h = _mk(c, s, sp, rr)
            if not ok:
                continue
            dd[n], mm[n], ss[n], pp[n], st[n], tg[n], ht[n] = d, m, s, c, sp, t, h
            n += 1
            break
    return dd[:n], mm[:n], ss[:n], pp[:n], st[:n], tg[:n], ht[:n]


@nb.njit(cache=True)
def f_fade(O, H, L, C, atr, trend, or_n, tgt_mode, use_trend, long_only):
    """A close back inside the opening range after a breakout: trade the other way."""
    D = C.shape[0]
    dd, mm, ss, pp, st, tg, ht = _alloc(D)
    n = 0
    for d in range(D):
        hi, lo, cnt = -1e18, 1e18, 0
        for j in range(or_n):
            if not np.isnan(H[d, j]):
                hi = max(hi, H[d, j]); lo = min(lo, L[d, j]); cnt += 1
        if cnt < or_n // 2 + 1 or hi <= lo:
            continue
        mid = (hi + lo) / 2
        up, dn, ext_hi, ext_lo = False, False, -1e18, 1e18
        for m in range(or_n, LAST_SIG + 1):
            c = C[d, m]
            if np.isnan(c):
                continue
            ext_hi = max(ext_hi, H[d, m]); ext_lo = min(ext_lo, L[d, m])
            s, sp = 0, 0.0
            if up and c < hi:
                s, sp = -1, ext_hi + 0.01
            elif dn and c > lo:
                s, sp = 1, ext_lo - 0.01
            if c > hi:
                up = True
            if c < lo:
                dn = True
            if s == 0 or not _side_ok(s, trend[d], use_trend, long_only):
                continue
            if tgt_mode == 2:
                ok, t, h = _mk(c, s, sp, 1.5)
            else:
                ok, t, h = _mk(c, s, sp, 0.0)
                t, h = (mid if tgt_mode == 0 else (lo if s == -1 else hi)), True
                if (t - c) * s <= 0:
                    ok = False
            if not ok:
                continue
            dd[n], mm[n], ss[n], pp[n], st[n], tg[n], ht[n] = d, m, s, c, sp, t, h
            n += 1
            break
    return dd[:n], mm[:n], ss[:n], pp[:n], st[:n], tg[:n], ht[:n]


@nb.njit(cache=True)
def f_vwap_pullback(O, H, L, C, VW, atr, trend, start, k, rr, use_trend, long_only):
    D = C.shape[0]
    dd, mm, ss, pp, st, tg, ht = _alloc(D)
    n = 0
    for d in range(D):
        if np.isnan(atr[d]):
            continue
        for m in range(max(start, 30), LAST_SIG + 1):
            c, v, v0 = C[d, m], VW[d, m], VW[d, m - 30]
            if np.isnan(c) or np.isnan(v) or np.isnan(v0):
                continue
            s = 0
            if c > v and v > v0 and L[d, m] <= v and c > O[d, m]:
                s = 1
            elif c < v and v < v0 and H[d, m] >= v and c < O[d, m]:
                s = -1
            if s == 0 or not _side_ok(s, trend[d], use_trend, long_only):
                continue
            ok, t, h = _mk(c, s, c - s * k * atr[d], rr)
            if not ok:
                continue
            dd[n], mm[n], ss[n], pp[n], st[n], tg[n], ht[n] = d, m, s, c, c - s * k * atr[d], t, h
            n += 1
            break
    return dd[:n], mm[:n], ss[:n], pp[:n], st[:n], tg[:n], ht[:n]


@nb.njit(cache=True)
def f_vwap_stretch(O, H, L, C, VW, atr, trend, z, k, tgt_mode, use_trend, long_only):
    D = C.shape[0]
    dd, mm, ss, pp, st, tg, ht = _alloc(D)
    n = 0
    for d in range(D):
        if np.isnan(atr[d]) or atr[d] <= 0:
            continue
        for m in range(30, LAST_SIG + 1):
            c, v = C[d, m], VW[d, m]
            if np.isnan(c) or np.isnan(v):
                continue
            dev = (c - v) / atr[d]
            s = -1 if dev >= z else (1 if dev <= -z else 0)
            if s == 0 or not _side_ok(s, trend[d], use_trend, long_only):
                continue
            sp = c - s * k * atr[d]
            if tgt_mode == 0:
                ok, t, h = _mk(c, s, sp, 0.0)
                t, h = v, True
            else:
                ok, t, h = _mk(c, s, sp, float(tgt_mode))
            if not ok:
                continue
            dd[n], mm[n], ss[n], pp[n], st[n], tg[n], ht[n] = d, m, s, c, sp, t, h
            n += 1
            break
    return dd[:n], mm[:n], ss[:n], pp[:n], st[:n], tg[:n], ht[:n]


@nb.njit(cache=True)
def f_gap(O, H, L, C, open_px, prev_close, atr, trend, g, mode, e, k, tgt_mode, use_trend, long_only):
    """tgt_mode 0 none, 1 = 1R, 2 = 2R, 3 = gap fill (yesterday's close; fade only)."""
    D = C.shape[0]
    dd, mm, ss, pp, st, tg, ht = _alloc(D)
    n = 0
    for d in range(D):
        if np.isnan(atr[d]) or np.isnan(prev_close[d]) or np.isnan(open_px[d]):
            continue
        gap = (open_px[d] - prev_close[d]) / prev_close[d] * 100
        if abs(gap) < g:
            continue
        s = (1 if gap > 0 else -1) * mode
        c = C[d, e]
        if np.isnan(c) or not _side_ok(s, trend[d], use_trend, long_only):
            continue
        sp = c - s * k * atr[d]
        if tgt_mode == 3:
            ok, t, h = _mk(c, s, sp, 0.0)
            t, h = prev_close[d], True
            if (t - c) * s <= 0:
                ok = False
        else:
            ok, t, h = _mk(c, s, sp, float(tgt_mode))
        if not ok:
            continue
        dd[n], mm[n], ss[n], pp[n], st[n], tg[n], ht[n] = d, e, s, c, sp, t, h
        n += 1
    return dd[:n], mm[:n], ss[:n], pp[:n], st[:n], tg[:n], ht[:n]


@nb.njit(cache=True)
def f_momentum(O, H, L, C, open_px, prev_close, atr, trend, base, t, entry_m, k, use_trend, long_only):
    """The first 30 minutes' move (from the open, or from yesterday's close) decides the side for the last half hour."""
    D = C.shape[0]
    dd, mm, ss, pp, st, tg, ht = _alloc(D)
    n = 0
    for d in range(D):
        ref = open_px[d] if base == 0 else prev_close[d]
        c30, c = C[d, 29], C[d, entry_m]
        if np.isnan(atr[d]) or np.isnan(ref) or np.isnan(c30) or np.isnan(c):
            continue
        r = (c30 / ref - 1) * 100
        if abs(r) < t or r == 0:
            continue
        s = 1 if r > 0 else -1
        if not _side_ok(s, trend[d], use_trend, long_only):
            continue
        sp = c - s * k * atr[d]
        ok, tt, h = _mk(c, s, sp, 0.0)
        if not ok:
            continue
        dd[n], mm[n], ss[n], pp[n], st[n], tg[n], ht[n] = d, entry_m, s, c, sp, tt, h
        n += 1
    return dd[:n], mm[:n], ss[:n], pp[:n], st[:n], tg[:n], ht[:n]


@nb.njit(cache=True)
def f_prior_break(O, H, L, C, prev_high, prev_low, atr, trend, start, k, rr, use_trend, long_only):
    D = C.shape[0]
    dd, mm, ss, pp, st, tg, ht = _alloc(D)
    n = 0
    for d in range(D):
        if np.isnan(atr[d]) or np.isnan(prev_high[d]):
            continue
        if not np.isnan(O[d, 0]) and (O[d, 0] > prev_high[d] or O[d, 0] < prev_low[d]):
            continue                                   # opened outside yesterday's range: that is a gap, not a break
        for m in range(start, LAST_SIG + 1):
            c = C[d, m]
            if np.isnan(c):
                continue
            s = 1 if c > prev_high[d] else (-1 if c < prev_low[d] else 0)
            if s == 0 or not _side_ok(s, trend[d], use_trend, long_only):
                continue
            sp = prev_high[d] - k * atr[d] if s == 1 else prev_low[d] + k * atr[d]
            ok, t, h = _mk(c, s, sp, rr)
            if not ok:
                continue
            dd[n], mm[n], ss[n], pp[n], st[n], tg[n], ht[n] = d, m, s, c, sp, t, h
            n += 1
            break
    return dd[:n], mm[:n], ss[:n], pp[:n], st[:n], tg[:n], ht[:n]


@nb.njit(cache=True)
def f_late_trend(O, H, L, C, VW, open_px, atr, trend, m0, kmove, k, use_trend, long_only):
    D = C.shape[0]
    dd, mm, ss, pp, st, tg, ht = _alloc(D)
    n = 0
    for d in range(D):
        c, v = C[d, m0], VW[d, m0]
        if np.isnan(atr[d]) or atr[d] <= 0 or np.isnan(c) or np.isnan(v) or np.isnan(open_px[d]):
            continue
        mv = (c - open_px[d]) / atr[d]
        s = 1 if (mv >= kmove and c > v) else (-1 if (mv <= -kmove and c < v) else 0)
        if s == 0 or not _side_ok(s, trend[d], use_trend, long_only):
            continue
        sp = c - s * k * atr[d]
        ok, t, h = _mk(c, s, sp, 0.0)
        if not ok:
            continue
        dd[n], mm[n], ss[n], pp[n], st[n], tg[n], ht[n] = d, m0, s, c, sp, t, h
        n += 1
    return dd[:n], mm[:n], ss[:n], pp[:n], st[:n], tg[:n], ht[:n]


@nb.njit(cache=True)
def f_rsi5(O, H, L, C, VW, atr, trend, lo_th, k, tgt_mode, use_trend, long_only):
    """RSI(14) on 5-minute closes within the day; oversold = buy, overbought = sell."""
    D = C.shape[0]
    dd, mm, ss, pp, st, tg, ht = _alloc(D)
    n = 0
    for d in range(D):
        if np.isnan(atr[d]):
            continue
        prev, ag, al, cnt = np.nan, 0.0, 0.0, 0
        for m in range(4, LAST_SIG + 1, 5):
            c = C[d, m]
            if np.isnan(c):
                continue
            if not np.isnan(prev):
                ch = c - prev
                g, l = (ch if ch > 0 else 0.0), (-ch if ch < 0 else 0.0)
                cnt += 1
                if cnt <= 14:
                    ag += g / 14; al += l / 14
                else:
                    ag = (ag * 13 + g) / 14; al = (al * 13 + l) / 14
            prev = c
            if cnt < 14:
                continue
            rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
            s = 1 if rsi < lo_th else (-1 if rsi > 100 - lo_th else 0)
            if s == 0 or not _side_ok(s, trend[d], use_trend, long_only):
                continue
            sp = c - s * k * atr[d]
            if tgt_mode == 0:
                ok, t, h = _mk(c, s, sp, 0.0)
                t, h = VW[d, m], True
                if np.isnan(t) or (t - c) * s <= 0:
                    ok = False
            else:
                ok, t, h = _mk(c, s, sp, float(tgt_mode))
            if not ok:
                continue
            dd[n], mm[n], ss[n], pp[n], st[n], tg[n], ht[n] = d, m, s, c, sp, t, h
            n += 1
            break
    return dd[:n], mm[:n], ss[:n], pp[:n], st[:n], tg[:n], ht[:n]


@nb.njit(cache=True)
def f_rel_strength(O, H, L, C, open_px, atr, trend, spy_ret, m0, x, k, rr, use_trend, long_only):
    """spy_ret[d] = SPY's move from its open to minute m0 that day (NaN if unknown)."""
    D = C.shape[0]
    dd, mm, ss, pp, st, tg, ht = _alloc(D)
    n = 0
    for d in range(D):
        c = C[d, m0]
        if np.isnan(atr[d]) or np.isnan(c) or np.isnan(open_px[d]) or np.isnan(spy_ret[d]):
            continue
        rs = (c / open_px[d] - 1) * 100 - spy_ret[d]
        s = 1 if rs >= x else (-1 if rs <= -x else 0)
        if s == 0 or not _side_ok(s, trend[d], use_trend, long_only):
            continue
        sp = c - s * k * atr[d]
        ok, t, h = _mk(c, s, sp, rr)
        if not ok:
            continue
        dd[n], mm[n], ss[n], pp[n], st[n], tg[n], ht[n] = d, m0, s, c, sp, t, h
        n += 1
    return dd[:n], mm[:n], ss[:n], pp[:n], st[:n], tg[:n], ht[:n]


# ---------------------------------------------------------------- the variant grid (fixed before the first run)
def grid():
    """List of (family, params, entry window for the coin-flip control). Every variant runs both-sides and long-only."""
    out = []
    P = lambda *a: a
    for or_n in (5, 15, 30, 60):
        for stop in (0, 1, 2):
            for rr in (1.0, 2.0, 3.0, 0.0):
                for tr in (0, 1):
                    for win in (120, 358):
                        out.append(("F1_orb", P(or_n, stop, rr, tr, win), (or_n, win)))
    for or_n in (5, 15, 30):
        for tm in (0, 1, 2):
            for tr in (0, 1):
                out.append(("F2_fade", P(or_n, tm, tr), (or_n, LAST_SIG)))
    for start in (30, 60):
        for k in (0.15, 0.3):
            for rr in (1.0, 2.0, 3.0, 0.0):
                for tr in (0, 1):
                    out.append(("F3_vwap_pullback", P(start, k, rr, tr), (start, LAST_SIG)))
    for z in (0.4, 0.6, 0.8):
        for k in (0.15, 0.3):
            for tm in (0, 1, 2):
                for tr in (0, 1):
                    out.append(("F4_vwap_stretch", P(z, k, tm, tr), (30, LAST_SIG)))
    for g in (0.5, 1.0, 2.0):
        for mode in (1, -1):
            for e in (0, 4, 14):
                for k in (0.2, 0.4):
                    for tm in ((0, 1, 2) if mode == 1 else (0, 1, 2, 3)):
                        out.append(("F5_gap", P(g, mode, e, k, tm, 0), (0, LAST_SIG)))
    for base in (0, 1):
        for t in (0.0, 0.25, 0.5):
            for em in (329, 358):
                for k in (0.2, 0.5):
                    out.append(("F6_momentum", P(base, t, em, k, 0), (0, LAST_SIG)))
    for start in (15, 30):
        for k in (0.15, 0.3):
            for rr in (1.0, 2.0, 0.0):
                for tr in (0, 1):
                    out.append(("F7_prior_break", P(start, k, rr, tr), (start, LAST_SIG)))
    for m0 in (240, 300):
        for km in (0.25, 0.5, 1.0):
            for k in (0.2, 0.4):
                out.append(("F8_late_trend", P(m0, km, k, 0), (0, LAST_SIG)))
    for lo_th in (20.0, 25.0, 30.0):
        for k in (0.2, 0.4):
            for tm in (0, 1, 2):
                for tr in (0, 1):
                    out.append(("F9_rsi5", P(lo_th, k, tm, tr), (74, LAST_SIG)))
    for m0 in (29, 59):
        for x in (0.5, 1.0):
            for k in (0.2, 0.4):
                for rr in (2.0, 0.0):
                    out.append(("F10_rel_strength", P(m0, x, k, rr, 0), (0, LAST_SIG)))
    full = []
    for fam, p, win in out:
        for lo in (0, 1):
            full.append((fam, p + (lo,), win))
    return full


def signals(sym, fam, p, spy_ret=None):
    a = (sym.O, sym.H, sym.L, sym.C)
    if fam == "F1_orb":
        or_n, stop, rr, tr, win, lo = p
        return f_orb(*a, sym.atr, sym.trend, or_n, stop, rr, tr, lo, win)
    if fam == "F2_fade":
        or_n, tm, tr, lo = p
        return f_fade(*a, sym.atr, sym.trend, or_n, tm, tr, lo)
    if fam == "F3_vwap_pullback":
        start, k, rr, tr, lo = p
        return f_vwap_pullback(*a, sym.VW, sym.atr, sym.trend, start, k, rr, tr, lo)
    if fam == "F4_vwap_stretch":
        z, k, tm, tr, lo = p
        return f_vwap_stretch(*a, sym.VW, sym.atr, sym.trend, z, k, tm, tr, lo)
    if fam == "F5_gap":
        g, mode, e, k, tm, tr, lo = p
        return f_gap(*a, sym.open_px, sym.prev_close, sym.atr, sym.trend, g, mode, e, k, tm, tr, lo)
    if fam == "F6_momentum":
        base, t, em, k, tr, lo = p
        return f_momentum(*a, sym.open_px, sym.prev_close, sym.atr, sym.trend, base, t, em, k, tr, lo)
    if fam == "F7_prior_break":
        start, k, rr, tr, lo = p
        return f_prior_break(*a, sym.prev_high, sym.prev_low, sym.atr, sym.trend, start, k, rr, tr, lo)
    if fam == "F8_late_trend":
        m0, km, k, tr, lo = p
        return f_late_trend(*a, sym.VW, sym.open_px, sym.atr, sym.trend, m0, km, k, tr, lo)
    if fam == "F9_rsi5":
        th, k, tm, tr, lo = p
        return f_rsi5(*a, sym.VW, sym.atr, sym.trend, th, k, tm, tr, lo)
    if fam == "F10_rel_strength":
        m0, x, k, rr, tr, lo = p
        if spy_ret is None or sym.name == "SPY":
            return tuple(np.zeros(0, t) for t in (np.int64, np.int64, np.int64, float, float, float, np.bool_))
        return f_rel_strength(*a, sym.open_px, sym.atr, sym.trend, spy_ret[m0], m0, x, k, rr, tr, lo)
    raise ValueError(fam)


def spy_returns(spy, sym):
    """For each of sym's days: SPY's % move from its open to minute m0 (m0 = 29 and 59)."""
    pos = {d: i for i, d in enumerate(spy.days)}
    out = {}
    for m0 in (29, 59):
        r = np.full(len(sym.days), np.nan)
        for i, d in enumerate(sym.days):
            j = pos.get(d)
            if j is not None and not np.isnan(spy.C[j, m0]) and not np.isnan(spy.open_px[j]):
                r[i] = (spy.C[j, m0] / spy.open_px[j] - 1) * 100
        out[m0] = r
    return out


# ---------------------------------------------------------------- statistics
def day_matrix(trade_sets, all_days):
    """trade_sets: list (one per variant) of lists of per-symbol result dicts. Returns (V x days) sums of R and counts."""
    pos = {d: i for i, d in enumerate(all_days)}
    S = np.zeros((len(trade_sets), len(all_days)))
    N = np.zeros((len(trade_sets), len(all_days)))
    for v, sets in enumerate(trade_sets):
        for res in sets:
            if res is None or len(res["r"]) == 0:
                continue
            idx = np.fromiter((pos[d] for d in res["date"]), int, len(res["date"]))
            np.add.at(S[v], idx, res["r"])
            np.add.at(N[v], idx, 1)
    return S, N


def stats_from_days(S, N):
    """Per variant: trades, avg R per trade, and a day-clustered t-statistic of the daily R sums."""
    n = N.sum(1)
    avg = np.where(n > 0, S.sum(1) / np.maximum(n, 1), np.nan)
    T = S.shape[1]
    mean, sd = S.mean(1), S.std(1, ddof=1)
    t = np.where(sd > 0, mean / (sd / np.sqrt(T)), 0.0)
    return n, avg, t


def stationary_bootstrap_idx(T, n_boot, mean_block=5, seed=11):
    rng = np.random.default_rng(seed)
    idx = np.empty((n_boot, T), np.int64)
    p = 1.0 / mean_block
    for b in range(n_boot):
        i = rng.integers(T)
        for t in range(T):
            if t > 0:
                i = rng.integers(T) if rng.random() < p else (i + 1) % T
            idx[b, t] = i
    return idx


def reality_check(S, n_boot=1000, eligible=None):
    """White's Reality Check (studentised): p-value that the best variant's t-statistic is no better than what the
    best of this many zero-edge variants would reach by luck. S: (V x days) daily R sums."""
    if eligible is not None:
        S = S[eligible]
    V, T = S.shape
    mean, sd = S.mean(1), S.std(1, ddof=1)
    se = np.where(sd > 0, sd / np.sqrt(T), np.inf)
    t_obs = (mean / se).max()
    idx = stationary_bootstrap_idx(T, n_boot)
    best = np.empty(n_boot)
    for b in range(n_boot):
        mb = S[:, idx[b]].mean(1)
        best[b] = ((mb - mean) / se).max()                 # recentred: every variant has zero edge here
    return float((best >= t_obs).mean()), float(t_obs), best
