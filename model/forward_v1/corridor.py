#!/usr/bin/env python3
"""Liqzo market model, corridor candidate `corridor_c4_sh_v1`.  RESEARCH ONLY · NO_TRADE · no network, no keys.

What it is: the range in which the price is expected to close 4 and 24 hours ahead (deciles of the log return), for BTC
and ETH, recomputed at every closed hour. It carries no direction and is not a signal.

Engine (frozen, hashes in LOCK.json): engine/incumbent_engine.py (liqzo-scenario-lab-0.1, the tournament's Conditional)
with its two taker-flow features made constant (taker_buy_volume = volume / 2, as OKX candles carry no taker volume),
and engine/market.py's weighted_quantile.
Calibrator: one width multiplier per asset x horizon x volatility state x origin hour (UTC), taken from the last 180
matured forecasts of the same cell (at least 60, otherwise 1), applied to every decile around the median.
State: "spike" when BTC's 24-hour RMS return is not below its 168-hour RMS, otherwise "calm" (the tournament's rule).

  python corridor.py replay   --okx DIR --history FILE --start 2026-01-06T00:00 --end 2026-11-10T00:00 --out OUT.csv.gz
  python corridor.py score    --forecasts OUT.csv.gz [--start ...] [--end ...]
  python corridor.py snapshot --okx DIR --history FILE [--at 2026-11-11T05:00]
  python corridor.py selftest
"""
import argparse, glob, json, math, sys
from pathlib import Path
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "engine"))
from incumbent_engine import from_frames, ScenarioEngine        # noqa: E402  (frozen)
from market import weighted_quantile                             # noqa: E402  (frozen)

VERSION = "corridor_c4_sh_v1"
ASSETS = ("BTC", "ETH")
HORIZONS = (4, 24)
QC = [f"q{i}" for i in range(10, 100, 10)]
QL = np.arange(1, 10) / 10
WINDOW, MINIMUM, LEVEL = 180, 60, 0.8
COLS = ["open_time", "open", "high", "low", "close", "volume", "taker_buy_volume"]


def ts(s):
    return int(pd.Timestamp(s, tz="UTC").timestamp())


def iso(t):
    return pd.Timestamp(int(t), unit="s", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def load(directory, pattern):
    """Hourly candles -> the frozen engine's MarketData. pattern: '{a}-usdt-swap_1h_*.csv' (OKX) or 'um_{a}usdt_1h_*.csv'."""
    frames = {}
    for a in ASSETS:
        paths = sorted(glob.glob(str(Path(directory) / pattern.format(a=a.lower()))))
        if not paths:
            raise ValueError(f"no hourly files for {a} in {directory}")
        f = pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)
        f["taker_buy_volume"] = f["volume"] / 2          # the taker-flow features become constant and drop out
        frames[a] = f[COLS]
    return from_frames(frames)


def forecast(data, start, end, log=None):
    """Raw deciles at every hourly origin in [start, end] (bar-close times). y is NaN while the target has not closed."""
    eng = ScenarioEngine(data)
    idx = np.flatnonzero((data.time >= start) & (data.time <= end))
    rows, refused = [], []
    for n, i in enumerate(idx):
        try:
            fit = eng.fit(int(i))
        except ValueError as e:                              # no forecast is invented when the engine refuses
            refused.append((iso(data.time[i]), str(e)))
            continue
        state = "spike" if data.features[i][2] >= 0 else "calm"
        for ai, a in enumerate(ASSETS):
            for h in HORIZONS:
                q = weighted_quantile(fit["paths"][:, h - 1, ai] * fit["sigma"][ai], fit["weights"])
                y = np.nan
                if i + h < len(data.time) and np.isfinite(data.returns[i + 1:i + h + 1, ai]).all():
                    y = float(np.log(data.close[i + h, ai] / data.close[i, ai]))
                rows.append([int(data.time[i]), a, h, state, float(data.close[i, ai]), y, *q.tolist()])
        if log and n % 1000 == 0:
            print(f"  {n + 1} / {len(idx)}", file=log, flush=True)
    df = pd.DataFrame(rows, columns=["o", "asset", "horizon", "state", "price", "y", *QC])
    df["t"] = df.o + df.horizon * 3600
    df["hour"] = (df.o % 86400) // 3600
    return df, refused


def stretch(y, q):
    """By how much the 80% interval must be scaled around the median to just contain the outcome."""
    med = q[:, 4]
    return np.maximum((y - med) / (q[:, 8] - med), (med - y) / (med - q[:, 0]))


def multiplier(past):
    n = len(past)
    if n < MINIMUM:
        return 1.0
    return float(np.sort(past)[min(n, math.ceil((n + 1) * LEVEL)) - 1])


def calibrate(df, history=None):
    """Adds f (the multiplier), n (matured forecasts behind it) and the calibrated deciles c10..c90.
    history: rows o, asset, horizon, state, r from earlier matured forecasts of the same engine (may be None)."""
    df = df.sort_values(["o", "asset", "horizon"]).reset_index(drop=True)
    q = df[QC].to_numpy()
    r = stretch(df.y.to_numpy(), q)                         # NaN while the target has not closed
    f = np.ones(len(df)); used = np.zeros(len(df), int)
    hist = {}
    if history is not None and len(history):
        hh = history.assign(hour=(history.o % 86400) // 3600, t=history.o + history.horizon * 3600).sort_values("o")
        for key, g in hh.groupby(["asset", "horizon", "state", "hour"]):
            hist[key] = (g.t.to_numpy(), g.r.to_numpy())
    o, t = df.o.to_numpy(), df.t.to_numpy()
    for key, g in df.groupby(["asset", "horizon", "state", "hour"]):
        ix = g.index.to_numpy()
        ht, hr = hist.get(key, (np.empty(0, np.int64), np.empty(0)))
        for j, i in enumerate(ix):
            own = ix[:j]
            own = own[(t[own] <= o[i]) & np.isfinite(r[own])]
            past = np.r_[hr[ht <= o[i]], r[own]][-WINDOW:]
            f[i] = multiplier(past); used[i] = len(past)
    med = q[:, 4]
    c = med[:, None] + f[:, None] * (q - med[:, None])
    out = df.copy(); out["f"] = f; out["n"] = used
    for k, name in enumerate(QC):
        out["c" + name[1:]] = c[:, k]
    return out


def wis(y, q):
    e = y[:, None] - q
    return 2 * np.mean(np.maximum(QL * e, (QL - 1) * e), axis=1)


def _boot(day_table, stat, reps=10000, block=14, seed=11):
    n = len(day_table); rng = np.random.default_rng(seed); k = int(np.ceil(n / block))
    s = rng.integers(0, max(n - block + 1, 1), size=(reps, k))
    idx = (s[:, :, None] + np.arange(block)).reshape(reps, -1)[:, :n] % n
    vals = np.array([stat(day_table[i]) for i in idx])
    return np.quantile(vals, [0.025, 0.975], axis=0)


def score(df):
    """The forward criteria F1-F4 on matured forecasts (see PREREG.md). Returns a dict."""
    d = df[np.isfinite(df.y)].copy()
    y = d.y.to_numpy(); raw = d[QC].to_numpy(); cal = d[["c" + n[1:] for n in QC]].to_numpy()
    d["hit_raw"] = (raw[:, 0] <= y) & (y <= raw[:, 8]); d["hit"] = (cal[:, 0] <= y) & (y <= cal[:, 8])
    d["wis_raw"] = wis(y, raw); d["wis"] = wis(y, cal); d["day"] = d.o // 86400
    days = np.sort(d.day.unique()); pos = {v: k for k, v in enumerate(days)}
    # per-day counts: [spike hits, spike n, calm hits, calm n] for the candidate and for the raw engine
    tab = np.zeros((len(days), 8))
    for (day, state), g in d.groupby(["day", "state"]):
        k = 0 if state == "spike" else 2
        tab[pos[day], k:k + 2] = [g.hit.sum(), len(g)]; tab[pos[day], 4 + k:6 + k] = [g.hit_raw.sum(), len(g)]
    def cov(x):
        x = x.sum(axis=0)
        return np.array([x[0] / max(x[1], 1), x[2] / max(x[3], 1), x[4] / max(x[5], 1), x[6] / max(x[7], 1)])
    point = cov(tab); lo, hi = _boot(tab, cov)
    out = {"days": int(len(days)), "forecasts": int(len(d)),
           "coverage_spike": float(point[0]), "coverage_spike_ci95": [float(lo[0]), float(hi[0])],
           "coverage_calm": float(point[1]), "coverage_calm_ci95": [float(lo[1]), float(hi[1])],
           "raw_coverage_spike": float(point[2]), "raw_coverage_calm": float(point[3]),
           "coverage_all": float(d.hit.mean()), "raw_coverage_all": float(d.hit_raw.mean())}
    out["F1_each_state_ci_contains_80"] = bool(lo[0] <= 0.8 <= hi[0] and lo[1] <= 0.8 <= hi[1])
    out["gap"] = float(point[0] - point[1]); out["raw_gap"] = float(point[2] - point[3])
    out["F2_state_gap_not_larger_than_raw"] = bool(abs(out["gap"]) <= abs(out["raw_gap"]))
    d4 = d[d.horizon == 4]; blocks = d4.groupby(d4.hour // 4).hit.mean(); rawblocks = d4.groupby(d4.hour // 4).hit_raw.mean()
    out["coverage_4h_by_block"] = [float(v) for v in blocks]; out["raw_coverage_4h_by_block"] = [float(v) for v in rawblocks]
    out["range_4h_blocks"] = float(blocks.max() - blocks.min()); out["F3_range_4h_blocks_le_8pp"] = bool(out["range_4h_blocks"] <= 0.08)
    ratio = float((d.groupby(["asset", "horizon"]).wis.mean() / d.groupby(["asset", "horizon"]).wis_raw.mean()).mean() - 1)
    out["wis_change_vs_raw"] = ratio; out["F4_wis_not_worse_than_0_5pct"] = bool(ratio <= 0.005)
    out["by_cell"] = {f"{a}-{h}h": float(v) for (a, h), v in d.groupby(["asset", "horizon"]).hit.mean().items()}
    out["by_hour_4h"] = [float(v) for v in d4.groupby("hour").hit.mean()]
    out["PASS"] = bool(out["F1_each_state_ci_contains_80"] and out["F2_state_gap_not_larger_than_raw"]
                       and out["F3_range_4h_blocks_le_8pp"] and out["F4_wis_not_worse_than_0_5pct"])
    return out


def snapshot(data, history, at=None, max_age_hours=2, now=None):
    """The corridor at the latest closed hour (or at `at`), as the JSON a page or an API would read."""
    base = {"schema": "liqzo.corridor/1", "model": VERSION, "status": "research", "signal": "NO_TRADE",
            "execution_allowed": False, "source": "OKX USDT swaps, closed 1h candles (atlas-data)",
            "semantics": "range of the hourly close at the target hour; not a path, not a direction"}
    o = int(data.time[-1]) if at is None else int(at)
    if now is not None and now - o > max_age_hours * 3600:
        return {**base, "as_of": iso(o), "refused": "stale data"}
    start = o - 400 * 86400
    df, refused = forecast(data, start, o)
    if not len(df) or df.o.max() != o:
        return {**base, "as_of": iso(o), "refused": dict(refused).get(iso(o), "no forecast at this hour")}
    cal = calibrate(df, history); cur = cal[cal.o == o]
    out = {**base, "as_of": iso(o), "state": cur.state.iloc[0], "assets": {}}
    for a in ASSETS:
        hz = {}
        for h in HORIZONS:
            r = cur[(cur.asset == a) & (cur.horizon == h)].iloc[0]
            c = np.array([r["c" + n[1:]] for n in QC])
            hz[str(h)] = {"target": iso(o + h * 3600), "multiplier": round(float(r.f), 4), "matured_in_cell": int(r.n),
                          "log_return_deciles": [round(float(v), 6) for v in c],
                          "price_80": [round(float(r.price * math.exp(c[0])), 2), round(float(r.price * math.exp(c[8])), 2)],
                          "price_40": [round(float(r.price * math.exp(c[2])), 2), round(float(r.price * math.exp(c[6])), 2)]}
        out["assets"][a] = {"price": float(cur[cur.asset == a].price.iloc[0]), "horizons": hz}
    return out


def selftest():
    q = np.array([[-4, -3, -2, -1, 0, 1, 2, 3, 4.0]]) / 100
    assert abs(stretch(np.array([0.08]), q)[0] - 2.0) < 1e-12 and abs(stretch(np.array([-0.02]), q)[0] - 0.5) < 1e-12
    assert multiplier(np.ones(59)) == 1.0 and multiplier(np.arange(1, 101) / 100) == 0.81
    # a forecast that is twice too narrow is widened to about 80% coverage, using only matured outcomes
    rng = np.random.default_rng(1); n = 600; o = 1_700_000_000 - 1_700_000_000 % 86400 + np.arange(n) * 86400
    y = rng.standard_normal(n) * 0.02; base = np.array([-1.2816, -0.8416, -0.5244, -0.2533, 0, 0.2533, 0.5244, 0.8416, 1.2816]) * 0.01
    df = pd.DataFrame({"o": o, "asset": "BTC", "horizon": 24, "state": "calm", "price": 1.0, "y": y})
    for k, name in enumerate(QC):
        df[name] = base[k]
    df["t"] = df.o + 86400; df["hour"] = 0
    c = calibrate(df); late = c[c.n >= MINIMUM]
    hit = ((late.c10 <= late.y) & (late.y <= late.c90)).mean(); rawhit = ((late.q10 <= late.y) & (late.y <= late.q90)).mean()
    assert rawhit < 0.6 and 0.74 < hit < 0.86, (rawhit, hit)
    assert (c.f[:MINIMUM] == 1).all() and (c.n.to_numpy() <= WINDOW).all()
    # no look-ahead: changing later outcomes leaves earlier multipliers untouched
    df2 = df.copy(); df2.loc[400:, "y"] *= 50
    assert np.array_equal(calibrate(df2).f[:400].to_numpy(), c.f[:400].to_numpy())
    print("selftest ok: stretch, multiplier, widening to", round(float(hit), 3), "from", round(float(rawhit), 3), "and no look-ahead")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["replay", "score", "snapshot", "selftest"])
    ap.add_argument("--okx"); ap.add_argument("--binance"); ap.add_argument("--history"); ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--out"); ap.add_argument("--forecasts"); ap.add_argument("--at")
    a = ap.parse_args()
    if a.cmd == "selftest":
        selftest(); sys.exit(0)
    hist = pd.read_csv(a.history) if a.history else None
    if a.cmd == "score":
        df = pd.read_csv(a.forecasts)
        if a.start: df = df[df.o >= ts(a.start)]
        if a.end: df = df[df.o <= ts(a.end)]
        print(json.dumps(score(df), indent=1)); sys.exit(0)
    data = load(a.okx, "{a}-usdt-swap_1h_*.csv") if a.okx else load(a.binance, "um_{a}usdt_1h_*.csv")
    if a.cmd == "snapshot":
        print(json.dumps(snapshot(data, hist, at=ts(a.at) if a.at else None), indent=1)); sys.exit(0)
    df, refused = forecast(data, ts(a.start), ts(a.end), log=sys.stderr)
    cal = calibrate(df, hist); cal.to_csv(a.out, index=False)
    print(json.dumps({"origins": int(cal.o.nunique()), "rows": int(len(cal)), "refused": len(refused), "first": iso(cal.o.min()), "last": iso(cal.o.max())}))
