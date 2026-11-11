#!/usr/bin/env python3
"""Liqzo market model, the hourly snapshot for the page.  RESEARCH ONLY · NO_TRADE · no network, no keys.

One JSON per closed hour: the corridor of `corridor_c4_sh_v1` (4 and 24 hours ahead, BTC and ETH) and twenty scenario
paths for the chart. The frozen files of the forward check (forward_v1/corridor.py, its engine/ and history/) are
imported and never changed: every run compares them with forward_v1/LOCK.json and refuses if they differ. The
corridor written here is the one `corridor.py snapshot` gives (`selftest --full` proves it on a real hour).
What this file adds:

  cache      raw forecasts older than 50 hours are kept between runs (atlas-data never rewrites a row older than 48
             hours), so an hour costs about fifty engine fits instead of 9,600; the newer hours are recomputed;
  scenarios  twenty real past 24-hour paths the engine weighs most, one from each 5% slice of the 24h outcome, each
             from a different episode, scaled to today's volatility and by the corridor's own width multiplier
             (linear in the hour between 4 and 24: an assumption, checked on seen data, see README.md);
  centre     the path that stays closest to the hourly median over all 24 hours (not only at the end), so the
             brightest line on the chart shows no jump the model does not promise;
  record     how many matured forecasts since the forward check began closed inside their 80% range;
  log        one line per forecast, appended when it is published: the range was public before its hour closed.

  python model_live.py snapshot --okx ../data/okx --history forward_v1/history/c4_binance_stretch_2025.csv.gz \
                                --cache CACHE.csv.gz --out latest.json [--log log] [--at 2026-11-11T12:00] [--max-age-hours 2]
  python model_live.py selftest [--okx ../data/okx --history forward_v1/history/c4_binance_stretch_2025.csv.gz [--full]]
"""
import argparse, hashlib, json, math, sys, tempfile, time
from pathlib import Path
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
FROZEN = HERE / "forward_v1"
sys.path.insert(0, str(FROZEN))
import corridor as C                                             # noqa: E402  (frozen)

SCHEMA = "liqzo.model/1"
RECORD_SINCE = C.ts("2026-11-12T00:00")                          # the forward check's first origin (forward_v1/LOCK.json)
SPAN = 400 * 86400                                               # the same look-back as corridor.snapshot
FRESH = 50 * 3600                                                # hours newer than this are never taken from the cache
SLICES, NEIGHBOURS, APART = 20, 200, 24
RAW = ["o", "asset", "horizon", "state", "price", *C.QC]         # what the cache keeps; outcomes come from the data
LOG = ["as_of", "asset", "horizon", "target", "price", "lo80", "hi80", "lo40", "hi40", "multiplier", "matured_in_cell", "state"]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def lock():
    """Are the frozen files the ones of forward_v1/LOCK.json? The snapshot says so, and refuses if they are not."""
    want = json.loads((FROZEN / "LOCK.json").read_text())["hashes"]
    have = {k: sha(FROZEN / k) for k in ("corridor.py", "engine/incumbent_engine.py", "engine/market.py", "history/c4_binance_stretch_2025.csv.gz")}
    return {"matches_forward_v1": all(want[k] == v for k, v in have.items()), "corridor_py": have["corridor.py"][:16]}


def outcomes(data, df):
    """The outcome of every row by corridor.forecast's own rule and arithmetic: known once every hourly return up to
    the target hour is known."""
    pos = {int(t): k for k, t in enumerate(data.time)}
    ai = {a: k for k, a in enumerate(C.ASSETS)}
    n, y = len(data.time), np.full(len(df), np.nan)
    for j, (o, a, h) in enumerate(zip(df.o.to_numpy(), df.asset.to_numpy(), df.horizon.to_numpy())):
        i, a, h = pos[int(o)], ai[a], int(h)
        if i + h < n and np.isfinite(data.returns[i + 1:i + h + 1, a]).all():
            y[j] = float(np.log(data.close[i + h, a] / data.close[i, a]))
    return y


def raw_forecasts(data, o, cache=None):
    """What corridor.forecast(data, o - 400 days, o) returns, with the hours older than FRESH taken from the cache."""
    start = o - SPAN
    old = None
    if cache is not None and Path(cache).exists():
        old = pd.read_csv(cache, float_precision="round_trip")
        if list(old.columns) == RAW:
            old = old[(old.o >= start) & (old.o <= o - FRESH)]
        if list(old.columns) != RAW or not len(old):
            old = None
    have = set(int(t) for t in old.o.unique()) if old is not None else set()
    want = data.time[(data.time >= start) & (data.time <= o)]
    todo = np.array([t for t in want if int(t) not in have], dtype=np.int64)
    parts, refused = ([old] if old is not None else []), []
    if len(todo):
        for run in np.split(todo, np.flatnonzero(np.diff(todo) != 3600) + 1):      # each unbroken run of hours, through the frozen function
            df, ref = C.forecast(data, int(run[0]), int(run[-1]))
            refused += ref
            if len(df):
                parts.append(df[RAW])
    if not parts:
        return pd.DataFrame(columns=[*RAW, "y", "t", "hour"]), refused
    df = pd.concat(parts, ignore_index=True).sort_values(["o", "asset", "horizon"], kind="stable").reset_index(drop=True)
    df["o"] = df.o.astype("int64"); df["horizon"] = df.horizon.astype("int64")
    df.insert(5, "y", outcomes(data, df))                                          # the column order of corridor.forecast
    df["t"] = df.o + df.horizon * 3600
    df["hour"] = (df.o % 86400) // 3600
    return df, refused


def save_cache(df, cache):
    Path(cache).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(cache) + ".tmp.gz")
    df[RAW].to_csv(tmp, index=False, float_format="%.17g", compression="gzip")     # 17 digits: a float64 comes back bit for bit
    tmp.replace(cache)


def scenarios(data, fit, ai, price, f4, f24, gate24):
    """Twenty scenario paths (hourly closes as prices) and the index of the centre one."""
    w, sig = fit["weights"], fit["sigma"][ai]
    P = fit["paths"][:, :, ai] * sig                                               # cumulative log returns, hours 1..24
    Q = np.array([C.weighted_quantile(P[:, k], w)[[0, 4, 8]] for k in range(24)])
    med, half = Q[:, 1], (Q[:, 2] - Q[:, 0]) / 2
    H = np.arange(1, 25)
    fh = np.where(H <= 4, f4, f4 + (f24 - f4) * (H - 4) / 20)
    order = np.argsort(P[:, 23], kind="stable")
    mid = np.empty(len(w)); mid[order] = np.cumsum(w[order]) - w[order] / 2        # where a path's 24h outcome sits, 0..1
    rank = np.empty(len(w), int); rank[np.argsort(-w, kind="stable")] = np.arange(1, len(w) + 1)
    stray = np.mean(np.abs(P - med) / half, axis=1)                                # distance from the hourly median, in half-widths
    starts = fit["starts"]
    # the centre: among the engine's nearest neighbours whose 24h outcome is in the middle tenth, the calmest path
    middle = (mid >= 0.45) & (mid < 0.55)
    cand = np.flatnonzero(middle & (rank <= NEIGHBOURS))
    if not len(cand):
        cand = np.flatnonzero(middle) if middle.any() else np.arange(len(w))
    cj = int(cand[np.argmin(stray[cand])])
    centre = 9 if mid[cj] < 0.5 else 10
    pick, taken = {centre: cj}, [int(starts[cj])]
    pools = {k: np.flatnonzero((mid >= k / SLICES + 0.0125) & (mid < (k + 1) / SLICES - 0.0125)) for k in range(SLICES) if k != centre}
    pools = {k: v for k, v in pools.items() if len(v)}
    for k in sorted(pools, key=lambda k: -w[pools[k]].max()):                      # the best matches choose first
        ix = pools[k][np.argsort(-w[pools[k]], kind="stable")]
        j = next((int(j) for j in ix if all(abs(int(starts[j]) - t) >= APART for t in taken)), None)
        if j is not None:
            taken.append(int(starts[j])); pick[k] = j
    T = med + fh * (P - med)                                                       # the same width multiplier the corridor uses
    out = []
    for k in sorted(pick):
        j = pick[k]
        c = [round(float(price * math.exp(x)), 2) for x in T[j]]
        d = (centre - k) / centre if k < centre else (k - centre) / (SLICES - 1 - centre)
        out.append({"slice": k, "date": C.iso(data.time[int(starts[j])])[:10], "d": round(float(d), 4),
                    "out": not (gate24[0] <= c[23] <= gate24[1]), "c": c})
    return out, [s["slice"] for s in out].index(centre)


def record(cal, asset):
    """Matured forecasts since the forward check began: how many closed inside their 80% range (corridor.score's rule)."""
    r = cal[(cal.asset == asset) & (cal.o >= RECORD_SINCE) & np.isfinite(cal.y)]
    out = {}
    for h in C.HORIZONS:
        g = r[r.horizon == h]
        out[f"h{h}"] = {"n": int(len(g)), "inside": int(((g.c10 <= g.y) & (g.y <= g.c90)).sum())}
    return out


def snapshot(data, history, cache=None, at=None, max_age_hours=2, now=None):
    """(the JSON for the page, the raw forecasts to cache or None)"""
    base = {"schema": SCHEMA, "model": C.VERSION, "status": "research", "signal": "NO_TRADE", "execution_allowed": False,
            "source": "OKX USDT swaps, closed 1h candles (atlas-data)",
            "semantics": "ranges of the hourly close 4 and 24 hours ahead; the paths are scenarios, not a forecast and not a direction",
            "record_since": C.iso(RECORD_SINCE), "lock": lock()}
    o = int(data.time[-1]) if at is None else int(at)
    base["as_of"] = C.iso(o)
    if not base["lock"]["matches_forward_v1"]:
        return {**base, "refused": "the frozen files differ from forward_v1/LOCK.json"}, None
    if now is not None and now - o > max_age_hours * 3600:
        return {**base, "refused": "stale data"}, None
    df, refused = raw_forecasts(data, o, cache)
    if not len(df) or df.o.max() != o:
        return {**base, "refused": dict(refused).get(C.iso(o), "no forecast at this hour")}, (df if len(df) else None)
    cal = C.calibrate(df, history)
    cur = cal[cal.o == o]
    fit = C.ScenarioEngine(data).fit(int(np.flatnonzero(data.time == o)[0]))
    out = {**base, "state": cur.state.iloc[0], "assets": {}}
    for ai, a in enumerate(C.ASSETS):
        hz, fm = {}, {}
        for h in C.HORIZONS:
            r = cur[(cur.asset == a) & (cur.horizon == h)].iloc[0]
            c = np.array([r["c" + n[1:]] for n in C.QC]); p = float(r.price)
            fm[h] = float(r.f)
            hz[f"h{h}"] = {"target": C.iso(o + h * 3600), "multiplier": round(float(r.f), 4), "matured_in_cell": int(r.n),
                           "lo80": round(float(p * math.exp(c[0])), 2), "hi80": round(float(p * math.exp(c[8])), 2),
                           "lo40": round(float(p * math.exp(c[2])), 2), "hi40": round(float(p * math.exp(c[6])), 2)}
        price = float(cur[cur.asset == a].price.iloc[0])
        sc, centre = scenarios(data, fit, ai, price, fm[4], fm[24], (hz["h24"]["lo80"], hz["h24"]["hi80"]))
        out["assets"][a] = {"price": price, **hz, "record": record(cal, a), "centre": centre, "scenarios": sc}
    return out, df


def append_log(snap, directory):
    """One line per forecast in DIR/YYYY-MM.csv, once per hour: what was published, before its target hour closed."""
    if "assets" not in snap:
        return False
    f = Path(directory) / (snap["as_of"][:7] + ".csv")
    f.parent.mkdir(parents=True, exist_ok=True)
    text = f.read_text() if f.exists() else ",".join(LOG) + "\n"
    if ("\n" + snap["as_of"] + ",") in text:
        return False
    for a, A in snap["assets"].items():
        for h in C.HORIZONS:
            z = A[f"h{h}"]
            text += ",".join(str(v) for v in [snap["as_of"], a, h, z["target"], A["price"], z["lo80"], z["hi80"], z["lo40"], z["hi40"], z["multiplier"], z["matured_in_cell"], snap["state"]]) + "\n"
    f.write_text(text)
    return True


def selftest(okx=None, history=None, full=False):
    # 1. the cache round trip keeps every float
    x = pd.DataFrame({"o": [1, 2], "asset": ["BTC", "ETH"], "horizon": [4, 24], "state": ["calm", "spike"], "price": [0.1 + 0.2, 1e5 / 3],
                      **{q: [math.pi * (k + 1), -math.e / (k + 1)] for k, q in enumerate(C.QC)}})
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "c.csv.gz"; save_cache(x, p); y = pd.read_csv(p, float_precision="round_trip")
        assert list(y.columns) == RAW and all((x[c].to_numpy() == y[c].to_numpy()).all() for c in RAW)
        # 2. the log takes an hour once
        s = {"as_of": "2026-11-12T00:00:00+00:00", "state": "calm", "assets": {a: {"price": 1.0, **{f"h{h}": {"target": "t", "lo80": 1, "hi80": 2, "lo40": 1, "hi40": 2, "multiplier": 1.0, "matured_in_cell": 0} for h in C.HORIZONS}} for a in C.ASSETS}}
        assert append_log(s, d) and not append_log(s, d) and len((Path(d) / "2026-11.csv").read_text().splitlines()) == 5
        assert not append_log({"as_of": "x", "refused": "stale data"}, d)
    print("cache round trip: exact; the log takes an hour once")
    assert lock()["matches_forward_v1"], "frozen files differ from forward_v1/LOCK.json"
    print("frozen files: match forward_v1/LOCK.json")
    if not okx:
        print("selftest OK (pass --okx DIR --history FILE for the checks on real hours)"); return
    data = C.load(okx, "{a}-usdt-swap_1h_*.csv"); hist = pd.read_csv(history)
    o = int(data.time[-1])
    with tempfile.TemporaryDirectory() as d:
        cache = Path(d) / "cache.csv.gz"
        # 3. the rows are corridor.forecast's own, with or without the cache
        t0 = time.time(); frozen, _ = C.forecast(data, o - SPAN, o); t_frozen = time.time() - t0
        t0 = time.time(); first, _ = raw_forecasts(data, o, None); t_full = time.time() - t0
        save_cache(first[first.o <= o - 30 * 3600], cache)                         # a cache as an earlier run left it
        t0 = time.time(); again, _ = raw_forecasts(data, o, cache); t_inc = time.time() - t0
        for name, b in (("without a cache", first), ("with a cache", again)):
            assert list(b.columns) == list(frozen.columns) and len(b) == len(frozen), name
            for c in frozen.columns:
                u, v = frozen[c].to_numpy(), b[c].to_numpy()
                assert ((u == v) | ((u != u) & (v != v))).all() if u.dtype.kind == "f" else (u == v).all(), (name, c)
        print(f"raw forecasts: {len(frozen)} rows identical to corridor.forecast bit for bit, with and without the cache "
              f"(frozen {t_frozen:.0f} s, without a cache {t_full:.0f} s, with a cache {t_inc:.1f} s)")
        save_cache(first, cache)
        t0 = time.time(); snap, _ = snapshot(data, hist, cache); t_snap = time.time() - t0
    assert "assets" in snap, snap
    for a_, A in snap["assets"].items():
        sc = A["scenarios"]
        assert len(sc) == SLICES and len({s["date"] for s in sc}) >= SLICES - 2 and all(len(s["c"]) == 24 for s in sc)
        ends = [s["c"][23] for s in sc]
        assert ends == sorted(ends), "scenarios are ordered by their 24h ending"
        assert sc[A["centre"]]["d"] == 0 and sc[0]["d"] == 1 and sc[-1]["d"] == 1
        assert 13 <= sum(not s["out"] for s in sc) <= 19, [s["out"] for s in sc]
    size = len(json.dumps(snap, separators=(",", ":")))
    print(f"snapshot at {snap['as_of']}: 20 scenarios per asset, centre marked, {size} bytes, {t_snap:.1f} s with a warm cache")
    if full:                                                                       # the slow proof: the frozen snapshot itself
        fr = C.snapshot(data, hist)
        assert fr["as_of"] == snap["as_of"] and fr["state"] == snap["state"]
        for a_ in C.ASSETS:
            assert fr["assets"][a_]["price"] == snap["assets"][a_]["price"]
            for h in C.HORIZONS:
                u, v = fr["assets"][a_]["horizons"][str(h)], snap["assets"][a_][f"h{h}"]
                assert u["price_80"] == [v["lo80"], v["hi80"]] and u["price_40"] == [v["lo40"], v["hi40"]] and u["target"] == v["target"] \
                    and u["multiplier"] == v["multiplier"] and u["matured_in_cell"] == v["matured_in_cell"], (a_, h, u, v)
        print("corridor: identical to corridor.py snapshot (prices, targets, multipliers, cell sizes)")
    print("selftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["snapshot", "selftest"])
    ap.add_argument("--okx"); ap.add_argument("--history"); ap.add_argument("--cache"); ap.add_argument("--out"); ap.add_argument("--log"); ap.add_argument("--at")
    ap.add_argument("--max-age-hours", type=float, default=2); ap.add_argument("--full", action="store_true")
    a = ap.parse_args()
    if a.cmd == "selftest":
        selftest(a.okx, a.history, a.full)
    else:
        data = C.load(a.okx, "{a}-usdt-swap_1h_*.csv")
        snap, df = snapshot(data, pd.read_csv(a.history), a.cache, C.ts(a.at) if a.at else None, a.max_age_hours, None if a.at else time.time())
        if df is not None and a.cache:
            save_cache(df, a.cache)
        if "refused" in snap:                                                      # the last good snapshot stays where it is
            print(f"as_of {snap['as_of']}: refused ({snap['refused']}); nothing written")
            sys.exit(0)
        text = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
        if a.out:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True); Path(a.out).write_text(text + "\n")
        logged = append_log(snap, a.log) if a.log else False
        print(f"as_of {snap['as_of']}: {len(text)} bytes, state {snap['state']}" + (", logged" if logged else ""))
