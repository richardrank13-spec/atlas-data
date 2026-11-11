#!/usr/bin/env python3
"""Liqzo market model, the hourly snapshot for the page.  RESEARCH ONLY · NO_TRADE · no network, no keys.

One JSON per closed hour: the corridor of `corridor_c4_sh_v1` (4 and 24 hours ahead, BTC and ETH) and twenty scenario
paths for the chart. The frozen files of the forward check (forward_v1/corridor.py, its engine/ and history/) are
imported and never changed: every run compares them with forward_v1/LOCK.json and refuses if they differ. The
corridor written here is the one `corridor.py snapshot` gives (`selftest --full` proves it on a real hour).
What this file adds:

  coins      besides BTC and ETH, the coins of EXT, admitted one by one by a rule recorded before its run (Liqzo
             research, market_model/ext_v1; README.md here). The frozen engine is given BTC and the coin, the coin in
             the place of its second asset, and only the coin's rows are kept; BTC's state is the state. Nothing of
             this touches BTC's and ETH's numbers, which are computed first and alone. A coin's calibrator sees the
             coin's own matured forecasts on OKX hours and nothing else, every one since its hours begin: the
             computation of ext_v1's step T1, continued to this hour. A coin whose hours are missing, late or too
             short is left out of that hour's snapshot; BTC and ETH are written all the same;

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
  python model_live.py selftest [--okx ../data/okx --history forward_v1/history/c4_binance_stretch_2025.csv.gz [--full] [--coins]]
"""
import argparse, glob, hashlib, json, math, sys, tempfile, time
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
# The coins besides BTC and ETH. ext_v1's admission check (seven criteria on Binance hours, then step T1 on OKX hours)
# decides this list and nothing else does; ZEC failed one criterion and is not here. The forward check (forward_v1)
# is about BTC and ETH only and these coins are no part of its verdict: each keeps its own count from its first
# published hour.
EXT = ("BNB", "SOL", "LINK", "LTC")
EXT_FROM = C.ts("2025-01-02T00:00")                              # a coin needs OKX hours from here on, as BTC and ETH have
SLOT, SLOT_AT = C.ASSETS[1], 1                                   # the frozen engine's second asset, where a coin stands


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def lock():
    """Are the frozen files the ones of forward_v1/LOCK.json? The snapshot says so, and refuses if they are not."""
    want = json.loads((FROZEN / "LOCK.json").read_text())["hashes"]
    have = {k: sha(FROZEN / k) for k in ("corridor.py", "engine/incumbent_engine.py", "engine/market.py", "history/c4_binance_stretch_2025.csv.gz")}
    return {"matches_forward_v1": all(want[k] == v for k, v in have.items()), "corridor_py": have["corridor.py"][:16]}


def outcomes(data, df, at=None):
    """The outcome of every row by corridor.forecast's own rule and arithmetic: known once every hourly return up to
    the target hour is known. at: the data's column of every row (a coin's rows are all the second asset's)."""
    pos = {int(t): k for k, t in enumerate(data.time)}
    ai = {a: k for k, a in enumerate(C.ASSETS)}
    n, y = len(data.time), np.full(len(df), np.nan)
    for j, (o, a, h) in enumerate(zip(df.o.to_numpy(), df.asset.to_numpy(), df.horizon.to_numpy())):
        i, a, h = pos[int(o)], (ai[a] if at is None else at), int(h)
        if i + h < n and np.isfinite(data.returns[i + 1:i + h + 1, a]).all():
            y[j] = float(np.log(data.close[i + h, a] / data.close[i, a]))
    return y


def raw_forecasts(data, o, cache=None, coin=None, start=None):
    """What corridor.forecast(data, o - 400 days, o) returns, with the hours older than FRESH taken from the cache.
    coin: the data is BTC and this coin in the second asset's place; only its rows are kept, under its own name, and
    from the first hour there is (start: another first hour, for the selftest)."""
    start = start if start is not None else o - SPAN if coin is None else int(data.time[0])
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
            if coin is not None and len(df):
                df = df[df.asset == SLOT].assign(asset=coin)
            if len(df):
                parts.append(df[RAW])
    if not parts:
        return pd.DataFrame(columns=[*RAW, "y", "t", "hour"]), refused
    df = pd.concat(parts, ignore_index=True).sort_values(["o", "asset", "horizon"], kind="stable").reset_index(drop=True)
    df["o"] = df.o.astype("int64"); df["horizon"] = df.horizon.astype("int64")
    df.insert(5, "y", outcomes(data, df, None if coin is None else SLOT_AT))      # the column order of corridor.forecast
    df["t"] = df.o + df.horizon * 3600
    df["hour"] = (df.o % 86400) // 3600
    return df, refused


def save_cache(df, cache):
    Path(cache).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(cache) + ".tmp.gz")
    df[RAW].to_csv(tmp, index=False, float_format="%.17g", compression="gzip")     # 17 digits: a float64 comes back bit for bit
    tmp.replace(cache)


def decimals(price):
    """Decimals of a written price: 2 as for BTC and ETH, more for a coin under 100 so an hour's step is not lost."""
    return 2 if price >= 100 else 3 if price >= 10 else 4


def scenarios(data, fit, ai, price, f4, f24, gate24, nd=2):
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
        c = [round(float(price * math.exp(x)), nd) for x in T[j]]
        d = (centre - k) / centre if k < centre else (k - centre) / (SLICES - 1 - centre)
        out.append({"slice": k, "date": C.iso(data.time[int(starts[j])])[:10], "d": round(float(d), 4),
                    "out": not (gate24[0] <= c[23] <= gate24[1]), "c": c})
    return out, [s["slice"] for s in out].index(centre)


def record(cal, asset, since=RECORD_SINCE):
    """Matured forecasts since the forward check began (a coin: since its first published hour): how many closed inside
    their 80% range (corridor.score's rule)."""
    r = cal[(cal.asset == asset) & (cal.o >= since) & np.isfinite(cal.y)]
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
        out["assets"][a] = block(data, cal, cur, fit, ai, a, o)
    return out, df


def block(data, cal, cur, fit, ai, a, o, since=None):
    """One asset of the snapshot: its two ranges, its count and its scenarios. since: a coin's own first published hour."""
    price = float(cur[cur.asset == a].price.iloc[0])
    nd = 2 if since is None else decimals(price)
    hz, fm = {}, {}
    for h in C.HORIZONS:
        r = cur[(cur.asset == a) & (cur.horizon == h)].iloc[0]
        c = np.array([r["c" + n[1:]] for n in C.QC]); p = float(r.price)
        fm[h] = float(r.f)
        hz[f"h{h}"] = {"target": C.iso(o + h * 3600), "multiplier": round(float(r.f), 4), "matured_in_cell": int(r.n),
                       "lo80": round(float(p * math.exp(c[0])), nd), "hi80": round(float(p * math.exp(c[8])), nd),
                       "lo40": round(float(p * math.exp(c[2])), nd), "hi40": round(float(p * math.exp(c[6])), nd)}
    sc, centre = scenarios(data, fit, ai, price, fm[4], fm[24], (hz["h24"]["lo80"], hz["h24"]["hi80"]), nd)
    rec = record(cal, a) if since is None else record(cal, a, since)
    return {"price": price, **hz, "record": rec, "centre": centre, "scenarios": sc, **({} if since is None else {"since": C.iso(since)})}


# ---------- the coins besides BTC and ETH
def frame(okx, coin):
    """A coin's closed OKX hours as the frozen engine reads them (corridor.load's own two lines), or None."""
    paths = sorted(glob.glob(str(Path(okx) / f"{coin.lower()}-usdt-swap_1h_*.csv")))
    if not paths:
        return None
    f = pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)
    f["taker_buy_volume"] = f["volume"] / 2
    return f[C.COLS]


def pair(okx, coin):
    """(the frozen engine's data for BTC and the coin, None) or (None, why the coin cannot be computed)."""
    f = frame(okx, coin)
    if f is None or not len(f):
        return None, "no OKX hours"
    if int(f.open_time.iloc[0]) > EXT_FROM:
        return None, "OKX hours only from " + C.iso(int(f.open_time.iloc[0]))
    try:
        return C.from_frames({C.ASSETS[0]: frame(okx, C.ASSETS[0]), SLOT: f}), None
    except ValueError as e:
        return None, str(e).replace(SLOT + ":", coin + ":")                        # the engine names the place, not the coin


def first_logged(directory, coin):
    """The hour a coin was first published (the public log's first line of it), or None."""
    if not directory:
        return None
    for f in sorted(Path(directory).glob("*.csv")):
        for line in f.read_text().splitlines()[1:]:
            x = line.split(",", 2)
            if len(x) > 1 and x[1] == coin:
                return C.ts(x[0].replace("Z", ""))
    return None


def coin_cache(cache, coin):
    return None if cache is None else Path(cache).with_name(f"raw_{coin.lower()}.csv.gz")


def unfit(A):
    """Why the page would refuse this block (liqzo.app reads a snapshot whole or not at all: site/functions/api/model.js
    there), or None. A coin's block that would be refused is left out, so BTC and ETH are never lost with it."""
    p = A["price"]
    for h in C.HORIZONS:
        z = A[f"h{h}"]
        if not (0 < z["lo80"] <= z["lo40"] < z["hi40"] <= z["hi80"]) or not (z["lo80"] < p < z["hi80"]):
            return f"its {h}h range does not hold its own price"
    sc = A["scenarios"]
    if not 3 <= len(sc) <= SLICES or sc[A["centre"]]["d"] != 0:
        return "its scenarios have no centre"
    if any(len(x["c"]) != 24 or not all(p / 3 < v < p * 3 for v in x["c"]) for x in sc):
        return "a scenario leaves the page's bounds"
    return None


def add_coin(snap, okx, coin, cache=None, log=None):
    """Adds the coin to a snapshot that already holds BTC and ETH, when the coin can be computed at the same hour and
    under the same state. Returns (why it was left out or None, its raw forecasts to cache or None)."""
    o = C.ts(snap["as_of"].replace("Z", ""))
    data, why = pair(okx, coin)
    if data is None:
        return why, None
    if int(data.time[-1]) < o:
        return "its hours end at " + C.iso(int(data.time[-1])), None
    df, refused = raw_forecasts(data, o, coin_cache(cache, coin), coin)
    if not len(df) or df.o.max() != o:
        return dict(refused).get(C.iso(o), "no forecast at this hour"), (df if len(df) else None)
    cal = C.calibrate(df, None)                                                    # the coin's own matured forecasts only
    cur = cal[cal.o == o]
    if cur.state.iloc[0] != snap["state"]:
        return "its state differs from BTC's own", df
    if int(cur.n.min()) < C.MINIMUM:
        return f"a cell has only {int(cur.n.min())} matured forecasts", df
    fit = C.ScenarioEngine(data).fit(int(np.flatnonzero(data.time == o)[0]))
    since = first_logged(log, coin)
    A = block(data, cal, cur, fit, SLOT_AT, coin, o, o if since is None else since)
    why = unfit(A)
    if why is None:
        snap["assets"][coin] = A
    return why, df


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


def selftest(okx=None, history=None, full=False, coins=False):
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
        # 2a. a coin's count starts at its first line in the log; its cache stands beside the main one; its decimals
        s["assets"]["SOL"] = s["assets"]["BTC"]; s["as_of"] = "2026-11-12T01:00:00Z"; append_log(s, d)
        assert first_logged(d, "SOL") == C.ts("2026-11-12T01:00") and first_logged(d, "LTC") is None and first_logged(None, "SOL") is None
        assert coin_cache(Path(d) / "raw.csv.gz", "SOL") == Path(d) / "raw_sol.csv.gz" and coin_cache(None, "SOL") is None
        assert (decimals(612.3), decimals(99.99), decimals(14.2), decimals(9.99)) == (2, 3, 3, 4)
        assert pair(d, "SOL") == (None, "no OKX hours")
        ok = {"price": 10.0, "centre": 1, "scenarios": [{"d": 1, "c": [10.0] * 24}, {"d": 0, "c": [10.0] * 24}, {"d": 1, "c": [10.0] * 24}],
              **{f"h{h}": {"lo80": 9.0, "lo40": 9.5, "hi40": 10.5, "hi80": 11.0} for h in C.HORIZONS}}
        assert unfit(ok) is None and unfit({**ok, "price": 11.0}) and unfit({**ok, "centre": 0}) and unfit({**ok, "scenarios": ok["scenarios"][:2]}) \
            and unfit({**ok, "h4": {**ok["h4"], "lo40": 8.0}}) and unfit({**ok, "scenarios": [{"d": 0, "c": [40.0] * 24}] * 3, "centre": 0})
    print("cache round trip: exact; the log takes an hour once; a coin's count starts at its first logged hour")
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
        # 3a. a coin's route, with ETH walking it: the same hours, states and deciles as ETH has in the frozen forecast
        two, why = pair(okx, SLOT)
        assert why is None and (two.time == data.time).all()
        route, _ = raw_forecasts(two, o, None, SLOT, start=o - SPAN)
        own = frozen[frozen.asset == SLOT].reset_index(drop=True)
        assert len(route) == len(own) and all(((u == v) | ((u != u) & (v != v))).all() if u.dtype.kind == "f" else (u == v).all()
                                              for u, v in ((own[c].to_numpy(), route[c].to_numpy()) for c in frozen.columns))
        print(f"a coin's route: {len(route)} rows of {SLOT} by it are the frozen forecast's own, bit for bit")
        if coins:                                                                  # the slow proof for every coin there are hours for
            before = json.dumps({k: snap["assets"][k] for k in C.ASSETS}, sort_keys=True)
            for coin in EXT:
                ccache = coin_cache(cache, coin)
                t0 = time.time(); why, cdf = add_coin(snap, okx, coin, cache, d); t_cold = time.time() - t0
                if why:
                    print(f"  {coin}: left out ({why})"); continue
                save_cache(cdf, ccache)
                # the same numbers by ext_v1's own lines (ext_check.py replay): frozen forecast, the coin's rows, frozen calibrator
                pd_, _ = pair(okx, coin)
                ref, _ = C.forecast(pd_, int(pd_.time[0]), o)
                ref = C.calibrate(ref[ref.asset == SLOT].assign(asset=coin), None)
                now_ = ref[ref.o == o]
                A = snap["assets"][coin]; nd = decimals(A["price"])
                for h in C.HORIZONS:
                    r = now_[now_.horizon == h].iloc[0]; z = A[f"h{h}"]
                    assert z["lo80"] == round(float(r.price * math.exp(r.c10)), nd) and z["hi80"] == round(float(r.price * math.exp(r.c90)), nd) \
                        and z["multiplier"] == round(float(r.f), 4) and z["matured_in_cell"] == int(r.n), (coin, h)
                keep = json.dumps(A, sort_keys=True); snap["assets"].pop(coin)
                t0 = time.time(); why2, _ = add_coin(snap, okx, coin, cache, d); t_warm = time.time() - t0
                assert why2 is None and json.dumps(snap["assets"][coin], sort_keys=True) == keep, coin
                done = ref[np.isfinite(ref.y) & (ref.n >= C.MINIMUM)]
                cov = float(((done.c10 <= done.y) & (done.y <= done.c90)).mean())
                print(f"  {coin}: {len(ref)} rows from {C.iso(ref.o.min())}; the snapshot's ranges are the frozen calibrator's own on them, "
                      f"with and without the cache ({t_cold:.0f} s, then {t_warm:.1f} s); in the 80% range so far {100 * cov:.1f}% of {len(done)}")
            assert json.dumps({k: snap["assets"][k] for k in C.ASSETS}, sort_keys=True) == before, "a coin changed BTC or ETH"
            print("  BTC and ETH: unchanged by the coins")
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
    ap.add_argument("--max-age-hours", type=float, default=2); ap.add_argument("--full", action="store_true"); ap.add_argument("--coins", action="store_true")
    a = ap.parse_args()
    if a.cmd == "selftest":
        selftest(a.okx, a.history, a.full, a.coins)
    else:
        data = C.load(a.okx, "{a}-usdt-swap_1h_*.csv")
        snap, df = snapshot(data, pd.read_csv(a.history), a.cache, C.ts(a.at) if a.at else None, a.max_age_hours, None if a.at else time.time())
        if df is not None and a.cache:
            save_cache(df, a.cache)
        if "refused" in snap:                                                      # the last good snapshot stays where it is
            print(f"as_of {snap['as_of']}: refused ({snap['refused']}); nothing written")
            sys.exit(0)
        left = {}
        for coin in EXT:                                                           # after BTC and ETH, and never in their way
            try:
                why, cdf = add_coin(snap, a.okx, coin, a.cache, a.log)
                if cdf is not None and a.cache:
                    save_cache(cdf, coin_cache(a.cache, coin))
            except Exception as e:
                snap["assets"].pop(coin, None)
                why = f"{type(e).__name__}: {e}"
            if why:
                left[coin] = why
        text = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
        if a.out:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True); Path(a.out).write_text(text + "\n")
        logged = append_log(snap, a.log) if a.log else False
        print(f"as_of {snap['as_of']}: {len(text)} bytes, state {snap['state']}, {', '.join(snap['assets'])}" + (", logged" if logged else ""))
        for coin, why in left.items():
            print(f"  {coin} left out: {why}")
