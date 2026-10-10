"""Atlas data collector, run hourly by GitHub Actions.

Fetches public market data, keeps only closed candles, merges it into CSV files under data/
and writes data/status.json. Standard library only. Every source is independent: one failing
source never stops the others, and its error is recorded in the status file.

Rules that keep the history trustworthy:
  * a candle is stored only after it has closed;
  * rows older than 48 hours are never overwritten (no silent history edits);
  * hourly price files are split by year, so a commit only touches the current year.
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
NOW = int(time.time())
UA = {"User-Agent": "atlas-data-collector/1.0 (+https://github.com/richardrank13-spec/atlas-data)"}
FREEZE = 48 * 3600  # rows older than this are immutable


def get(url, timeout=30, raw=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return body if raw else json.loads(body.decode("utf-8"))


def iso(ts):
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).isoformat().replace("+00:00", "Z")


def path(*p):
    full = os.path.join(ROOT, *p)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    return full


def read_rows(fn, key):
    if not os.path.exists(fn):
        return {}
    with open(fn, newline="") as f:
        return {int(r[key]): r for r in csv.DictReader(f)}


def write_rows(fn, rows, cols, key):
    tmp = fn + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for k in sorted(rows):
            w.writerow({c: rows[k].get(c, "") for c in cols})
    os.replace(tmp, fn)


def merge(fn, new, cols, key):
    """Add new rows; overwrite only rows younger than FREEZE. Returns (added, overwritten)."""
    have = read_rows(fn, key)
    added = changed = 0
    for r in new:
        k = int(r[key])
        r = {c: str(r[c]) for c in cols}
        if k not in have:
            have[k] = r
            added += 1
        elif NOW - k < FREEZE and have[k] != r:
            have[k] = r
            changed += 1
    write_rows(fn, have, cols, key)
    return added, changed


def merge_yearly(prefix, new, cols, key="open_time"):
    by_year = {}
    for r in new:
        by_year.setdefault(dt.datetime.fromtimestamp(int(r[key]), dt.timezone.utc).year, []).append(r)
    tot = [0, 0]
    for y, rows in by_year.items():
        a, c = merge(path(f"{prefix}_{y}.csv"), rows, cols, key)
        tot[0] += a
        tot[1] += c
    return tot


def last_key(prefix, key="open_time"):
    d = os.path.dirname(path(prefix + "_x"))
    base = os.path.basename(prefix)
    files = sorted(f for f in os.listdir(d) if f.startswith(base + "_") and f.endswith(".csv"))
    if not files:
        return None
    rows = read_rows(os.path.join(d, files[-1]), key)
    return max(rows) if rows else None


OHLC = ["open_time", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------- price candles (1h)
def bitstamp():
    out = []
    last = last_key("bitstamp/btcusd_1h")
    start = (last + 3600) if last else NOW - 1000 * 3600
    for _ in range(6):  # pages of 1000 hours; normally one page
        js = get(f"https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=3600&limit=1000&start={start}")
        rows = js["data"]["ohlc"]
        if not rows:
            break
        for c in rows:
            t = int(c["timestamp"])
            if t + 3600 <= NOW - 60:
                out.append({"open_time": t, "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "volume": c["volume"]})
        nxt = int(rows[-1]["timestamp"]) + 3600
        if nxt <= start or nxt + 3600 > NOW:
            break
        start = nxt
    a, c = merge_yearly("bitstamp/btcusd_1h", out, OHLC)
    return {"rows": len(out), "added": a, "overwritten": c, "last_row": iso(max(r["open_time"] for r in out)) if out else None}


def coinbase(product):
    js = get(f"https://api.exchange.coinbase.com/products/{product}/candles?granularity=3600")
    out = [{"open_time": int(t), "open": o, "high": h, "low": lo, "close": c, "volume": v}
           for t, lo, h, o, c, v in js if int(t) + 3600 <= NOW - 60]
    a, c = merge_yearly(f"coinbase/{product.lower()}_1h", out, OHLC)
    return {"rows": len(out), "added": a, "overwritten": c, "last_row": iso(max(r["open_time"] for r in out)) if out else None}


def kraken():
    js = get("https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=60")
    if js.get("error"):
        raise RuntimeError(";".join(js["error"]))
    res = js["result"]
    series = next(v for k, v in res.items() if k != "last")
    out = [{"open_time": int(r[0]), "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[6]}
           for r in series if int(r[0]) + 3600 <= NOW - 60]
    a, c = merge_yearly("kraken/xbtusd_1h", out, OHLC)
    return {"rows": len(out), "added": a, "overwritten": c, "last_row": iso(max(r["open_time"] for r in out)) if out else None}


# ---------------------------------------------------------------- derivatives and sentiment
def deribit_funding():
    end = NOW * 1000
    start = end - 72 * 3600 * 1000
    js = get(f"https://www.deribit.com/api/v2/public/get_funding_rate_history?instrument_name=BTC-PERPETUAL&start_timestamp={start}&end_timestamp={end}")
    out = [{"t": int(r["timestamp"]) // 1000, "interest_1h": r["interest_1h"], "interest_8h": r["interest_8h"], "index_price": r["index_price"]} for r in js["result"]]
    a, c = merge(path("deribit/btc_funding_1h.csv"), out, ["t", "interest_1h", "interest_8h", "index_price"], "t")
    return {"rows": len(out), "added": a, "last_row": iso(max(r["t"] for r in out)) if out else None}


DVOL_START = 1616544000  # 2021-03-24, first DVOL data


def deribit_dvol():
    fn = path("deribit/btc_dvol_1h.csv")
    have = read_rows(fn, "t")
    cols = ["t", "open", "high", "low", "close"]
    out = []
    # newest window
    end = NOW * 1000
    js = get(f"https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC&resolution=3600&start_timestamp={end - 72 * 3600 * 1000}&end_timestamp={end}")
    out += [{"t": int(r[0]) // 1000, "open": r[1], "high": r[2], "low": r[3], "close": r[4]} for r in js["result"]["data"]]
    # backfill a few pages of older history per run, until the first DVOL day
    oldest = min(have) if have else None
    pages = 0
    while oldest and oldest > DVOL_START and pages < 40:
        js = get(f"https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC&resolution=3600&start_timestamp={(oldest - 1000 * 3600) * 1000}&end_timestamp={(oldest - 3600) * 1000}")
        page = [{"t": int(r[0]) // 1000, "open": r[1], "high": r[2], "low": r[3], "close": r[4]} for r in js["result"]["data"]]
        if not page:
            break
        out += page
        oldest = min(r["t"] for r in page)
        pages += 1
    out = [r for r in out if r["t"] + 3600 <= NOW - 60]
    a, c = merge(fn, out, cols, "t")
    allrows = read_rows(fn, "t")
    return {"rows": len(out), "added": a, "first_row": iso(min(allrows)) if allrows else None, "last_row": iso(max(allrows)) if allrows else None}


def okx_funding():
    js = get("https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=100")
    if js.get("code") != "0":
        raise RuntimeError(js.get("msg") or js.get("code"))
    out = [{"t": int(r["fundingTime"]) // 1000, "funding_rate": r["fundingRate"], "realized_rate": r.get("realizedRate", "")} for r in js["data"]]
    a, c = merge(path("okx/btc_usdt_swap_funding.csv"), out, ["t", "funding_rate", "realized_rate"], "t")
    return {"rows": len(out), "added": a, "last_row": iso(max(r["t"] for r in out)) if out else None}


def okx_open_interest():
    js = get("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy=BTC&period=1H")
    if js.get("code") != "0":
        raise RuntimeError(js.get("msg") or js.get("code"))
    out = [{"t": int(r[0]) // 1000, "oi_usd": r[1], "volume_usd": r[2]} for r in js["data"] if int(r[0]) // 1000 + 3600 <= NOW - 60]
    a, c = merge(path("okx/btc_open_interest_1h.csv"), out, ["t", "oi_usd", "volume_usd"], "t")
    return {"rows": len(out), "added": a, "last_row": iso(max(r["t"] for r in out)) if out else None}


BIN_COLS = ["t", "sum_open_interest", "sum_open_interest_value", "count_toptrader_long_short_ratio",
            "sum_toptrader_long_short_ratio", "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]
BIN_START = dt.date(2021, 12, 1)


def _binance_day(day):
    url = f"https://data.binance.vision/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-{day.isoformat()}.zip"
    try:
        blob = get(url, raw=True)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    z = zipfile.ZipFile(io.BytesIO(blob))
    text = z.read(z.namelist()[0]).decode("utf-8")
    hourly = {}
    for r in csv.DictReader(io.StringIO(text)):
        ts = int(dt.datetime.fromisoformat(r["create_time"]).replace(tzinfo=dt.timezone.utc).timestamp())
        h = ts - ts % 3600
        hourly[h] = r  # the last 5-minute snapshot within the hour
    return [dict({"t": h}, **{c: hourly[h][c] for c in BIN_COLS[1:]}) for h in sorted(hourly)]


def binance_metrics():
    """Futures positioning snapshots from Binance public data (daily files), kept hourly."""
    have_days = set()
    for f in os.listdir(os.path.dirname(path("binance/x"))):
        if f.startswith("btcusdt_metrics_1h_") and f.endswith(".csv"):
            for k in read_rows(os.path.join(os.path.dirname(path("binance/x")), f), "t"):
                have_days.add(dt.datetime.fromtimestamp(k, dt.timezone.utc).date())
    today = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).date()
    want = [today - dt.timedelta(days=i) for i in (1, 2, 3)]
    # backfill up to 20 older days per run
    oldest = min(have_days) if have_days else today
    d = oldest - dt.timedelta(days=1)
    while len(want) < 23 and d >= BIN_START:
        if d not in have_days:
            want.append(d)
        d -= dt.timedelta(days=1)
    out, missing = [], []
    want = sorted(set(want), reverse=True)
    for day in want:
        if day in have_days and day < today - dt.timedelta(days=2):
            continue
        rows = _binance_day(day)
        if rows is None:
            missing.append(day.isoformat())
            continue
        out += rows
    for r in out:
        r["open_time"] = r["t"]
    a, c = merge_yearly("binance/btcusdt_metrics_1h", out, BIN_COLS, key="t")
    days = sorted(have_days | {dt.datetime.fromtimestamp(r["t"], dt.timezone.utc).date() for r in out})
    return {"rows": len(out), "added": a, "first_day": days[0].isoformat() if days else None, "last_day": days[-1].isoformat() if days else None, "not_published": missing[:3]}


def fear_greed():
    js = get("https://api.alternative.me/fng/?limit=0&format=json")
    out = [{"t": int(r["timestamp"]), "value": r["value"], "classification": r["value_classification"]} for r in js["data"]]
    a, c = merge(path("sentiment/fear_greed_1d.csv"), out, ["t", "value", "classification"], "t")
    return {"rows": len(out), "added": a, "last_row": iso(max(r["t"] for r in out)) if out else None}


# ---------------------------------------------------------------- cross-venue check
def cross_venue(hours=48):
    def closes(prefix):
        y = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).year
        rows = {}
        for yy in (y - 1, y):
            fn = path(f"{prefix}_{yy}.csv")
            rows.update(read_rows(fn, "open_time"))
        return {k: float(v["close"]) for k, v in rows.items() if k >= NOW - hours * 3600}
    bs = closes("bitstamp/btcusd_1h")
    res = {"hours": hours, "bitstamp_rows": len(bs)}
    for name, prefix in (("coinbase", "coinbase/btc-usd_1h"), ("kraken", "kraken/xbtusd_1h")):
        other = closes(prefix)
        devs = sorted(abs(other[k] / bs[k] - 1) * 1e4 for k in bs if k in other and bs[k] > 0)
        res[name] = {"overlap": len(devs), "median_bps": round(devs[len(devs) // 2], 2) if devs else None, "max_bps": round(devs[-1], 2) if devs else None}
    return res


SOURCES = [
    ("bitstamp_btcusd_1h", bitstamp),
    ("coinbase_btcusd_1h", lambda: coinbase("BTC-USD")),
    ("coinbase_ethusd_1h", lambda: coinbase("ETH-USD")),
    ("coinbase_solusd_1h", lambda: coinbase("SOL-USD")),
    ("kraken_xbtusd_1h", kraken),
    ("deribit_funding_1h", deribit_funding),
    ("deribit_dvol_1h", deribit_dvol),
    ("okx_funding_8h", okx_funding),
    ("okx_open_interest_1h", okx_open_interest),
    ("binance_metrics_1h", binance_metrics),
    ("fear_greed_1d", fear_greed),
]


def main():
    sfn = path("status.json")
    prev = json.load(open(sfn)) if os.path.exists(sfn) else {"sources": {}}
    status = {"updated": iso(NOW), "sources": {}}
    for name, fn in SOURCES:
        p = prev.get("sources", {}).get(name, {})
        t0 = time.time()
        try:
            info = fn()
            status["sources"][name] = dict(info, ok=True, error=None, fails_in_row=0, last_ok=iso(NOW), sec=round(time.time() - t0, 1))
        except Exception as e:  # noqa: BLE001 - one source must never stop the others
            status["sources"][name] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300], "fails_in_row": int(p.get("fails_in_row", 0)) + 1,
                                       "last_ok": p.get("last_ok"), "last_row": p.get("last_row"), "sec": round(time.time() - t0, 1)}
        print(name, json.dumps(status["sources"][name])[:300], flush=True)
    try:
        status["cross_venue"] = cross_venue()
    except Exception as e:  # noqa: BLE001
        status["cross_venue"] = {"error": str(e)[:200]}
    with open(sfn, "w") as f:
        json.dump(status, f, indent=1, sort_keys=True)
    bad = [k for k, v in status["sources"].items() if not v["ok"]]
    print("failed:", bad or "none")
    # the run fails (and GitHub e-mails the owner) only if the primary price source keeps failing
    if status["sources"]["bitstamp_btcusd_1h"]["fails_in_row"] >= 3:
        sys.exit(1)


if __name__ == "__main__":
    main()
