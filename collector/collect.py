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


def first_key(prefix, key="open_time"):
    d = os.path.dirname(path(prefix + "_x"))
    base = os.path.basename(prefix)
    files = sorted(f for f in os.listdir(d) if f.startswith(base + "_") and f.endswith(".csv") and os.path.getsize(os.path.join(d, f)) > 0)
    for f in files:
        rows = read_rows(os.path.join(d, f), key)
        if rows:
            return min(rows)
    return None


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


# listing dates (with a margin) below which no Coinbase candles exist
CB_FLOOR = {"BTC-USD": None, "ETH-USD": 1462060800}  # 2016-05-01
CB_PAGES = 400  # backfill budget per product per run (300 hours per page)


def coinbase(product):
    js = get(f"https://api.exchange.coinbase.com/products/{product}/candles?granularity=3600")
    out = [{"open_time": int(t), "open": o, "high": h, "low": lo, "close": c, "volume": v}
           for t, lo, h, o, c, v in js if int(t) + 3600 <= NOW - 60]
    prefix = f"coinbase/{product.lower()}_1h"
    info = {}
    floor = CB_FLOOR.get(product)
    if floor:
        # extend the history backwards from the oldest stored hour until the listing date
        oldest = first_key(prefix) or (min(r["open_time"] for r in out) if out else NOW - NOW % 3600)
        pages = empty = 0
        while oldest > floor and pages < CB_PAGES and empty < 6:
            end = oldest - 3600
            start = end - 299 * 3600
            q = f"start={iso(start)}&end={iso(end)}"
            page = get(f"https://api.exchange.coinbase.com/products/{product}/candles?granularity=3600&{q}")
            rows = [{"open_time": int(t), "open": o, "high": h, "low": lo, "close": c, "volume": v}
                    for t, lo, h, o, c, v in page if start <= int(t) <= end]
            empty = 0 if rows else empty + 1
            out += rows
            oldest = start
            pages += 1
            time.sleep(0.15)
        info = {"backfill_pages": pages, "history_done": bool(oldest <= floor or empty >= 6)}
    a, c = merge_yearly(prefix, out, OHLC)
    return dict({"rows": len(out), "added": a, "overwritten": c, "first_row": iso(first_key(prefix)) if first_key(prefix) else None,
                 "last_row": iso(max(r["open_time"] for r in out)) if out else None}, **info)


def kraken(pair="XBTUSD", prefix="kraken/xbtusd_1h"):
    js = get(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=60")
    if js.get("error"):
        raise RuntimeError(";".join(js["error"]))
    res = js["result"]
    series = next(v for k, v in res.items() if k != "last")
    out = [{"open_time": int(r[0]), "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[6]}
           for r in series if int(r[0]) + 3600 <= NOW - 60]
    a, c = merge_yearly(prefix, out, OHLC)
    return {"rows": len(out), "added": a, "overwritten": c, "last_row": iso(max(r["open_time"] for r in out)) if out else None}


# ---------------------------------------------------------------- Binance spot klines (USDT)
def _sec(x):
    """Binance archive times: milliseconds, or microseconds for spot files from 2025 on."""
    x = int(x)
    return x // 1_000_000 if x > 10 ** 14 else x // 1000


def _binance_kline_zip(url):
    try:
        blob = get(url, raw=True, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    z = zipfile.ZipFile(io.BytesIO(blob))
    out = []
    for r in csv.reader(io.StringIO(z.read(z.namelist()[0]).decode("utf-8"))):
        if not r or not r[0].strip().isdigit():
            continue  # header line, if any
        out.append({"open_time": _sec(r[0]), "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]})
    return out


def binance_spot(symbol, start_month):
    """History from the Binance public data archive (all monthly files once, then daily files); the newest
    hours from the market-data API. Only closed candles are stored."""
    base = "https://data.binance.vision/data/spot"
    prefix = f"binance/{symbol.lower()}_1h"
    last = last_key(prefix)
    today = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).date()
    out, files, missing = [], 0, []
    if last is None:
        m = start_month
        while (m.year, m.month) < (today.year, today.month):
            rows = _binance_kline_zip(f"{base}/monthly/klines/{symbol}/1h/{symbol}-1h-{m.year}-{m.month:02d}.zip")
            files += 1
            if rows is None:
                missing.append(f"{m.year}-{m.month:02d}")
            else:
                out += rows
            m = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        d = today.replace(day=1)
    else:
        d = dt.datetime.fromtimestamp(last, dt.timezone.utc).date() - dt.timedelta(days=1)
    while d < today and files < 200:
        rows = _binance_kline_zip(f"{base}/daily/klines/{symbol}/1h/{symbol}-1h-{d.isoformat()}.zip")
        files += 1
        if rows is None:
            missing.append(d.isoformat())
        else:
            out += rows
        d += dt.timedelta(days=1)
    try:
        js = get(f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=1h&limit=72")
        out += [{"open_time": int(r[0]) // 1000, "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]} for r in js]
        live = "data-api.binance.vision"
    except Exception as e:  # noqa: BLE001
        live = f"unreachable: {type(e).__name__}: {e}"[:120]
    out = [r for r in out if r["open_time"] % 3600 == 0 and r["open_time"] + 3600 <= NOW - 60]
    a, c = merge_yearly(prefix, out, OHLC)
    fk, lk = first_key(prefix), last_key(prefix)
    return {"rows": len(out), "added": a, "overwritten": c, "files": files, "not_published": missing[-3:], "missing_n": len(missing),
            "live": live, "first_row": iso(fk) if fk else None, "last_row": iso(lk) if lk else None}


# ---------------------------------------------------------------- OKX
def okx_candles(inst, prefix, backfill_to=None, calls=40):
    """Closed 1h candles. With backfill_to (a date), older history is paged in from the history endpoint, at most
    `calls` pages of 100 hours per run, until the stored history reaches that date."""
    js = get(f"https://www.okx.com/api/v5/market/candles?instId={inst}&bar=1H&limit=300")
    if js.get("code") != "0":
        raise RuntimeError(js.get("msg") or js.get("code"))
    row = lambda r: {"open_time": int(r[0]) // 1000, "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
    out = [row(r) for r in js["data"] if r[8] == "1" and int(r[0]) // 1000 + 3600 <= NOW - 60]
    back = 0
    if backfill_to is not None:
        goal = int(dt.datetime(backfill_to.year, backfill_to.month, backfill_to.day, tzinfo=dt.timezone.utc).timestamp())
        first = first_key(prefix)
        oldest = min([first] + [r["open_time"] for r in out]) if first else min((r["open_time"] for r in out), default=None)
        while oldest is not None and oldest > goal and back < calls:
            js = get(f"https://www.okx.com/api/v5/market/history-candles?instId={inst}&bar=1H&limit=100&after={oldest * 1000}")
            back += 1
            if js.get("code") != "0" or not js.get("data"):
                break
            rows = [row(r) for r in js["data"] if r[8] == "1"]
            out += rows
            oldest = min(r["open_time"] for r in rows) if rows else None
            time.sleep(0.15)
    a, c = merge_yearly(prefix, out, OHLC)
    fk = first_key(prefix)
    return {"rows": len(out), "added": a, "overwritten": c, "history_pages": back, "first_row": iso(fk) if fk else None,
            "last_row": iso(max(r["open_time"] for r in out)) if out else None}


# OKX swaps besides BTC and ETH, for the live paper journal and the forward simulation. Since 2026-10-16 the platform
# covers BTC, ETH and BNB only; the files of the twelve other wave-2 coins stay in data/okx, frozen on that day.
OKX_ALTS = ["BNB"]


def okx_alt_swaps():
    """1h candles (history back to 2026-03-01, paged in over a few runs) and funding of the OKX USDT swaps in OKX_ALTS.
    One coin failing never stops the others; the report says which failed."""
    rep, bad = {}, {}
    for c in OKX_ALTS:
        inst = f"{c}-USDT-SWAP"
        try:
            r = okx_candles(inst, f"okx/{c.lower()}-usdt-swap_1h", backfill_to=dt.date(2026, 3, 1), calls=40)
            f = okx_funding(inst, f"okx/{c.lower()}_usdt_swap_funding.csv")
            rep[c] = {"first_row": r.get("first_row"), "last_row": r.get("last_row"), "history_pages": r.get("history_pages"), "funding_last": f.get("last_row")}
        except Exception as e:  # noqa: BLE001
            bad[c] = f"{type(e).__name__}: {e}"[:120]
    lasts = [v["last_row"] for v in rep.values() if v.get("last_row")]
    if not rep:
        raise RuntimeError(f"all OKX alt swaps failed: {bad}")
    return {"coins": rep, "failed": bad, "last_row": min(lasts) if lasts else None}


# ---------------------------------------------------------------- Binance USD-M perpetuals (history for the trade systems)
UM_COLS = ["open_time", "open", "high", "low", "close", "volume", "taker_buy_volume"]
FUND_COLS = ["t", "rate", "interval_h"]


def _um_klines_zip(url):
    try:
        blob = get(url, raw=True, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    z = zipfile.ZipFile(io.BytesIO(blob))
    out = []
    for r in csv.reader(io.StringIO(z.read(z.namelist()[0]).decode("utf-8"))):
        if not r or not r[0].strip().isdigit():
            continue  # header line
        out.append({"open_time": _sec(r[0]), "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5],
                    "taker_buy_volume": r[9] if len(r) > 9 else ""})
    return out


def binance_um(symbol, start_month):
    """Perpetual 1h klines from the Binance public archive: every monthly file once, then daily files."""
    base = "https://data.binance.vision/data/futures/um"
    prefix = f"binance/um_{symbol.lower()}_1h"
    last = last_key(prefix)
    today = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).date()
    out, files, missing = [], 0, []
    if last is None:
        m = start_month
        while (m.year, m.month) < (today.year, today.month):
            rows = _um_klines_zip(f"{base}/monthly/klines/{symbol}/1h/{symbol}-1h-{m.year}-{m.month:02d}.zip")
            files += 1
            if rows is None:
                missing.append(f"{m.year}-{m.month:02d}")
            else:
                out += rows
            m = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        d = today.replace(day=1)
    else:
        d = dt.datetime.fromtimestamp(last, dt.timezone.utc).date() - dt.timedelta(days=1)
    while d < today and files < 200:
        rows = _um_klines_zip(f"{base}/daily/klines/{symbol}/1h/{symbol}-1h-{d.isoformat()}.zip")
        files += 1
        if rows is None:
            missing.append(d.isoformat())
        else:
            out += rows
        d += dt.timedelta(days=1)
    out = [r for r in out if r["open_time"] % 3600 == 0 and r["open_time"] + 3600 <= NOW - 60]
    a, c = merge_yearly(prefix, out, UM_COLS)
    fk, lk = first_key(prefix), last_key(prefix)
    return {"rows": len(out), "added": a, "files": files, "not_published": missing[-3:], "missing_n": len(missing),
            "first_row": iso(fk) if fk else None, "last_row": iso(lk) if lk else None}


def binance_um_funding(symbol, start_month):
    """Funding rate history (monthly archive files; the current month arrives after it ends)."""
    base = "https://data.binance.vision/data/futures/um/monthly/fundingRate"
    fn = path(f"binance/um_{symbol.lower()}_funding.csv")
    have = read_rows(fn, "t")
    months = {dt.datetime.fromtimestamp(k, dt.timezone.utc).strftime("%Y-%m") for k in have}
    today = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).date()
    out, files, missing = [], 0, []
    m = start_month
    newest = None
    while (m.year, m.month) < (today.year, today.month):
        key = f"{m.year}-{m.month:02d}"
        newest = key
        if key not in months:
            try:
                blob = get(f"{base}/{symbol}/{symbol}-fundingRate-{key}.zip", raw=True, timeout=60)
                z = zipfile.ZipFile(io.BytesIO(blob))
                for r in csv.reader(io.StringIO(z.read(z.namelist()[0]).decode("utf-8"))):
                    if not r or not r[0].strip().isdigit():
                        continue
                    out.append({"t": _sec(r[0]), "rate": r[2] if len(r) > 2 else r[-1], "interval_h": r[1] if len(r) > 2 else ""})
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                missing.append(key)
            files += 1
        m = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    a, c = merge(fn, out, FUND_COLS, "t")
    allr = read_rows(fn, "t")
    return {"rows": len(out), "added": a, "files": files, "missing_n": len(missing), "missing": missing[-3:], "newest_month": newest,
            "first_row": iso(min(allr)) if allr else None, "last_row": iso(max(allr)) if allr else None}


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


def okx_funding(inst="BTC-USDT-SWAP", fn="okx/btc_usdt_swap_funding.csv"):
    js = get(f"https://www.okx.com/api/v5/public/funding-rate-history?instId={inst}&limit=100")
    if js.get("code") != "0":
        raise RuntimeError(js.get("msg") or js.get("code"))
    out = [{"t": int(r["fundingTime"]) // 1000, "funding_rate": r["fundingRate"], "realized_rate": r.get("realizedRate", "")} for r in js["data"]]
    a, c = merge(path(fn), out, ["t", "funding_rate", "realized_rate"], "t")
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


def _binance_day(day, symbol="BTCUSDT"):
    url = f"https://data.binance.vision/data/futures/um/daily/metrics/{symbol}/{symbol}-metrics-{day.isoformat()}.zip"
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


def binance_metrics(symbol="BTCUSDT", start=None, per_run=60):
    """Futures positioning snapshots from Binance public data (daily files), kept hourly.
    start: the oldest day to backfill (default BIN_START); per_run: older days fetched per run."""
    start = start or BIN_START
    s = symbol.lower()
    have_days = set()
    for f in os.listdir(os.path.dirname(path("binance/x"))):
        if f.startswith(f"{s}_metrics_1h_") and f.endswith(".csv"):
            for k in read_rows(os.path.join(os.path.dirname(path("binance/x")), f), "t"):
                have_days.add(dt.datetime.fromtimestamp(k, dt.timezone.utc).date())
    today = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).date()
    want = [today - dt.timedelta(days=i) for i in (1, 2, 3)]
    # backfill up to per_run older days per run
    oldest = min(have_days) if have_days else today
    d = oldest - dt.timedelta(days=1)
    while len(want) < per_run + 3 and d >= start:
        if d not in have_days:
            want.append(d)
        d -= dt.timedelta(days=1)
    out, missing = [], []
    want = sorted(set(want), reverse=True)
    for day in want:
        if day in have_days and day < today - dt.timedelta(days=2):
            continue
        rows = _binance_day(day, symbol)
        if rows is None:
            missing.append(day.isoformat())
            continue
        out += rows
    for r in out:
        r["open_time"] = r["t"]
    a, c = merge_yearly(f"binance/{s}_metrics_1h", out, BIN_COLS, key="t")
    days = sorted(have_days | {dt.datetime.fromtimestamp(r["t"], dt.timezone.utc).date() for r in out})
    return {"rows": len(out), "added": a, "first_day": days[0].isoformat() if days else None, "last_day": days[-1].isoformat() if days else None, "not_published": missing[:3]}


# positioning of BNB for crowd research (BTC and ETH have their own sources). The other twelve coins collected here
# until 2026-10-16 keep their files, frozen on that day. The backfill walks back from today.
METRIC_ALTS = ["BNBUSDT"]
METRIC_ALTS_START = BIN_START  # 2021-12-01, the start of the Binance metrics archive


def binance_metrics_alts():
    out = {}
    for sym in METRIC_ALTS:
        try:
            r = binance_metrics(sym, start=METRIC_ALTS_START, per_run=120)
            out[sym] = {"added": r["added"], "first_day": r["first_day"], "last_day": r["last_day"]}
        except Exception as e:  # noqa: BLE001 - one coin must not stop the others
            out[sym] = {"error": f"{type(e).__name__}: {e}"[:120]}
    bad = [k for k, v in out.items() if "error" in v]
    if len(bad) == len(METRIC_ALTS):
        raise RuntimeError("all coins failed: " + out[bad[0]]["error"])
    return {"coins": out, "failed": bad, "last_row": max((v.get("last_day") or "" for v in out.values()), default=None)}


# ---------------------------------------------------------------- wave 2: more perpetuals
# atlas-alerts research/2026-10-10_wave2_prereg.md fixed a 14-coin list before any of its data was downloaded. Since
# 2026-10-16 only BNB is kept up to date (wave 4 and the platform cover BTC, ETH, BNB); the other files are frozen.
UNIVERSE = ["BNBUSDT"]
UNIVERSE_START = dt.date(2019, 9, 1)
UNIVERSE_BUDGET = 1200  # archive files per run, shared by all coins; the first backfill spreads over a few runs


def _months(a, b):
    """First days of the months from a's month up to, not including, b's month."""
    m = a.replace(day=1)
    while (m.year, m.month) < (b.year, b.month):
        yield m
        m = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def _rows_per_month(fns, key):
    n = {}
    for fn in fns:
        for k in read_rows(fn, key):
            t = dt.datetime.fromtimestamp(k, dt.timezone.utc)
            n[(t.year, t.month)] = n.get((t.year, t.month), 0) + 1
    return n


def binance_um_universe():
    """1h klines and funding of the wave-2 coins from the Binance public archive. Missing months come from monthly files
    (a month counts as present when at most two days of its hours are absent); once a coin's months are complete, the
    current month comes from daily files. Months the archive does not have (before a listing) are remembered and not
    asked again, except last month, whose file may not be published yet: its days are taken instead."""
    base = "https://data.binance.vision/data/futures/um"
    mfn = path("binance/um_universe_missing.json")
    missing = json.load(open(mfn)) if os.path.exists(mfn) else {}
    today = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).date()
    first_this = today.replace(day=1)
    first_prev = (first_this - dt.timedelta(days=1)).replace(day=1)
    budget = UNIVERSE_BUDGET
    report = {}
    ddir = os.path.dirname(path("binance/x"))
    for sym in UNIVERSE:
        s = sym.lower()
        prefix = f"binance/um_{s}_1h"
        kmiss, fmiss = set(missing.get(sym, [])), set(missing.get(sym + "_funding", []))
        kfiles = [os.path.join(ddir, f) for f in os.listdir(ddir) if f.startswith(f"um_{s}_1h_") and f.endswith(".csv")]
        have = _rows_per_month(kfiles, "open_time")
        out, days, pending, files = [], [], 0, 0
        for m in _months(UNIVERSE_START, first_this):
            key = f"{m.year}-{m.month:02d}"
            nxt = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
            if have.get((m.year, m.month), 0) >= ((nxt - m).days - 2) * 24 or key in kmiss:
                continue
            if budget <= 0:
                pending += 1
                continue
            rows = _um_klines_zip(f"{base}/monthly/klines/{sym}/1h/{sym}-1h-{key}.zip")
            budget -= 1
            files += 1
            if rows is not None:
                out += rows
            elif m < first_prev:
                kmiss.add(key)
            else:
                d = m
                while d < first_this:
                    days.append(d)
                    d += dt.timedelta(days=1)
        if pending == 0:
            last = last_key(prefix)
            d = first_this
            if last is not None:
                d = max(d, dt.datetime.fromtimestamp(last, dt.timezone.utc).date())
            while d < today:
                days.append(d)
                d += dt.timedelta(days=1)
        for d in days:
            if budget <= 0:
                pending += 1
                break
            rows = _um_klines_zip(f"{base}/daily/klines/{sym}/1h/{sym}-1h-{d.isoformat()}.zip")
            budget -= 1
            files += 1
            if rows is not None:
                out += rows
        out = [r for r in out if r["open_time"] % 3600 == 0 and r["open_time"] + 3600 <= NOW - 60]
        added = merge_yearly(prefix, out, UM_COLS)[0] if out else 0
        ffn = path(f"binance/um_{s}_funding.csv")
        fhave = set(_rows_per_month([ffn], "t"))
        fout = []
        for m in _months(UNIVERSE_START, first_this):
            key = f"{m.year}-{m.month:02d}"
            if (m.year, m.month) in fhave or key in fmiss:
                continue
            if budget <= 0:
                pending += 1
                continue
            budget -= 1
            files += 1
            try:
                blob = get(f"{base}/monthly/fundingRate/{sym}/{sym}-fundingRate-{key}.zip", raw=True, timeout=60)
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                if m < first_prev:
                    fmiss.add(key)
                continue
            z = zipfile.ZipFile(io.BytesIO(blob))
            for r in csv.reader(io.StringIO(z.read(z.namelist()[0]).decode("utf-8"))):
                if not r or not r[0].strip().isdigit():
                    continue
                fout.append({"t": _sec(r[0]), "rate": r[2] if len(r) > 2 else r[-1], "interval_h": r[1] if len(r) > 2 else ""})
        fadded = merge(ffn, fout, FUND_COLS, "t")[0] if (fout or os.path.exists(ffn)) else 0
        missing[sym], missing[sym + "_funding"] = sorted(kmiss), sorted(fmiss)
        fk, lk = first_key(prefix), last_key(prefix)
        report[sym] = {"added": added, "funding_added": fadded, "files": files, "pending": pending,
                       "first_row": iso(fk) if fk else None, "last_row": iso(lk) if lk else None}
    with open(mfn, "w") as f:
        json.dump(missing, f, indent=1, sort_keys=True)
    lasts = [v["last_row"] for v in report.values() if v["last_row"]]
    return {"coins": report, "pending": sum(v["pending"] for v in report.values()), "budget_left": budget,
            "last_row": min(lasts) if lasts else None}


def fear_greed():
    js = get("https://api.alternative.me/fng/?limit=0&format=json")
    out = [{"t": int(r["timestamp"]), "value": r["value"], "classification": r["value_classification"]} for r in js["data"]]
    a, c = merge(path("sentiment/fear_greed_1d.csv"), out, ["t", "value", "classification"], "t")
    return {"rows": len(out), "added": a, "last_row": iso(max(r["t"] for r in out)) if out else None}


# ---------------------------------------------------------------- Hyperliquid (execution venue candidate)
# Hyperliquid's API keeps only the newest 5000 candles of each interval (about 208 days of hours, 833 days of 4-hour
# bars), so this archive is what keeps the older ones. Requests are POSTs to one endpoint and are rate-limited by
# weight per IP, so every run pages in at most HL_PAGES pages per series and the first backfill spreads over runs.
HL_INFO = "https://api.hyperliquid.xyz/info"
HL_COINS = ["BTC", "ETH", "BNB"]
HL_PAGES = 5
HL_FUNDING_START = dt.date(2023, 1, 1)  # before the venue's first listings; the API answers from its first record
HL_COLS = ["open_time", "open", "high", "low", "close", "volume", "trades"]


def hl_post(body, timeout=30):
    req = urllib.request.Request(HL_INFO, data=json.dumps(body).encode("utf-8"),
                                 headers=dict(UA, **{"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _hl_series(fetch, start, key, step, pages):
    """Pages forward from `start` (seconds) while a page brings rows past the last one. Stops at the current period,
    after `pages` requests, or at the first rate-limit answer (the next run goes on). Returns (rows, requests, limited)."""
    out, used, t = [], 0, start
    while used < pages and t + step <= NOW:
        try:
            rows = fetch(t)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                return out, used + 1, True
            raise
        used += 1
        rows = [r for r in rows if r[key] >= t]
        if not rows:
            break
        out += rows
        t = max(r[key] for r in rows) + 1
        time.sleep(1.0)
    return out, used, False


def hl_candles():
    """Closed 1h and 4h candles of the Hyperliquid BTC, ETH and BNB perpetuals. One series failing never stops the
    others; the report names it."""
    rep, bad = {}, {}
    for coin in HL_COINS:
        for interval, step in (("1h", 3600), ("4h", 14400)):
            prefix = f"hyperliquid/{coin.lower()}_perp_{interval}"

            def fetch(t, coin=coin, interval=interval):
                js = hl_post({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval,
                                                                "startTime": t * 1000, "endTime": NOW * 1000}})
                return [{"open_time": int(r["t"]) // 1000, "open": r["o"], "high": r["h"], "low": r["l"], "close": r["c"],
                         "volume": r["v"], "trades": r.get("n", "")} for r in js]
            try:
                last = last_key(prefix)
                start = last + step if last is not None else NOW - 5000 * step
                rows, used, limited = _hl_series(fetch, start, "open_time", step, HL_PAGES)
                rows = [r for r in rows if r["open_time"] % step == 0 and r["open_time"] + step <= NOW - 60]
                added = merge_yearly(prefix, rows, HL_COLS)[0] if rows else 0
                fk, lk = first_key(prefix), last_key(prefix)
                rep[f"{coin}_{interval}"] = {"added": added, "pages": used, "rate_limited": limited,
                                            "first_row": iso(fk) if fk else None, "last_row": iso(lk) if lk else None}
            except Exception as e:  # noqa: BLE001 - one series must not stop the others
                bad[f"{coin}_{interval}"] = f"{type(e).__name__}: {e}"[:120]
    if not rep:
        raise RuntimeError(f"all Hyperliquid candle series failed: {bad}")
    lasts = [v["last_row"] for k, v in rep.items() if k.endswith("_1h") and v["last_row"]]
    return {"series": rep, "failed": bad, "last_row": min(lasts) if lasts else None}


def hl_funding():
    """Hourly funding of the Hyperliquid BTC, ETH and BNB perpetuals (rate per hour, premium), from the first record."""
    rep, bad = {}, {}
    for coin in HL_COINS:
        fn = path(f"hyperliquid/{coin.lower()}_funding.csv")

        def fetch(t, coin=coin):
            js = hl_post({"type": "fundingHistory", "coin": coin, "startTime": t * 1000, "endTime": NOW * 1000})
            return [{"t": int(r["time"]) // 1000, "rate": r["fundingRate"], "premium": r.get("premium", "")} for r in js]
        try:
            have = read_rows(fn, "t")
            start = max(have) + 1 if have else int(dt.datetime(HL_FUNDING_START.year, HL_FUNDING_START.month,
                                                                  HL_FUNDING_START.day, tzinfo=dt.timezone.utc).timestamp())
            rows, used, limited = _hl_series(fetch, start, "t", 3600, HL_PAGES)
            added = merge(fn, rows, ["t", "rate", "premium"], "t")[0] if rows else 0
            allr = read_rows(fn, "t")
            rep[coin] = {"added": added, "pages": used, "rate_limited": limited,
                         "first_row": iso(min(allr)) if allr else None, "last_row": iso(max(allr)) if allr else None}
        except Exception as e:  # noqa: BLE001
            bad[coin] = f"{type(e).__name__}: {e}"[:120]
    if not rep:
        raise RuntimeError(f"all Hyperliquid funding series failed: {bad}")
    lasts = [v["last_row"] for v in rep.values() if v["last_row"]]
    return {"coins": rep, "failed": bad, "last_row": min(lasts) if lasts else None}


# ---------------------------------------------------------------- cross-venue check
CROSS = {  # asset: (primary prefix, [(name, prefix), ...])
    "btc": ("bitstamp/btcusd_1h", [("coinbase", "coinbase/btc-usd_1h"), ("kraken", "kraken/xbtusd_1h")]),
    "eth": ("coinbase/eth-usd_1h", [("kraken", "kraken/ethusd_1h")]),
    "bnb": ("binance/bnbusdt_1h", [("okx", "okx/bnb-usdt_1h")]),
}


def cross_venue(hours=48):
    def closes(prefix):
        y = dt.datetime.fromtimestamp(NOW, dt.timezone.utc).year
        rows = {}
        for yy in (y - 1, y):
            fn = path(f"{prefix}_{yy}.csv")
            rows.update(read_rows(fn, "open_time"))
        return {k: float(v["close"]) for k, v in rows.items() if k >= NOW - hours * 3600}

    def compare(primary, others):
        bs = closes(primary)
        res = {"primary_rows": len(bs)}
        for name, prefix in others:
            other = closes(prefix)
            devs = sorted(abs(other[k] / bs[k] - 1) * 1e4 for k in bs if k in other and bs[k] > 0)
            res[name] = {"overlap": len(devs), "median_bps": round(devs[len(devs) // 2], 2) if devs else None, "max_bps": round(devs[-1], 2) if devs else None}
        return res
    res = {"hours": hours}
    btc = compare(*CROSS["btc"])
    res["bitstamp_rows"] = btc.pop("primary_rows")
    res.update(btc)  # BTC keys stay where the engine reads them
    res["assets"] = {a: compare(*CROSS[a]) for a in ("eth", "bnb")}
    return res


SOURCES = [
    ("bitstamp_btcusd_1h", bitstamp),
    ("coinbase_btcusd_1h", lambda: coinbase("BTC-USD")),
    ("coinbase_ethusd_1h", lambda: coinbase("ETH-USD")),
    ("kraken_xbtusd_1h", kraken),
    ("kraken_ethusd_1h", lambda: kraken("ETHUSD", "kraken/ethusd_1h")),
    ("binance_bnbusdt_1h", lambda: binance_spot("BNBUSDT", dt.date(2017, 11, 1))),
    ("okx_bnbusdt_1h", lambda: okx_candles("BNB-USDT", "okx/bnb-usdt_1h")),
    ("binance_um_btcusdt_1h", lambda: binance_um("BTCUSDT", dt.date(2019, 9, 1))),
    ("binance_um_ethusdt_1h", lambda: binance_um("ETHUSDT", dt.date(2019, 11, 1))),
    ("binance_um_btcusdt_funding", lambda: binance_um_funding("BTCUSDT", dt.date(2019, 9, 1))),
    ("binance_um_ethusdt_funding", lambda: binance_um_funding("ETHUSDT", dt.date(2019, 11, 1))),
    ("okx_btc_swap_1h", lambda: okx_candles("BTC-USDT-SWAP", "okx/btc-usdt-swap_1h", backfill_to=dt.date(2025, 1, 1))),
    ("okx_eth_swap_1h", lambda: okx_candles("ETH-USDT-SWAP", "okx/eth-usdt-swap_1h", backfill_to=dt.date(2025, 1, 1))),
    ("okx_alt_swaps_1h", okx_alt_swaps),
    ("deribit_funding_1h", deribit_funding),
    ("deribit_dvol_1h", deribit_dvol),
    ("okx_funding_8h", okx_funding),
    ("okx_eth_funding_8h", lambda: okx_funding("ETH-USDT-SWAP", "okx/eth_usdt_swap_funding.csv")),
    ("okx_open_interest_1h", okx_open_interest),
    ("binance_metrics_1h", lambda: binance_metrics("BTCUSDT", per_run=120)),
    ("binance_metrics_eth_1h", lambda: binance_metrics("ETHUSDT", per_run=120)),
    ("binance_metrics_alts_1h", binance_metrics_alts),
    ("fear_greed_1d", fear_greed),
    ("hyperliquid_candles", hl_candles),
    ("hyperliquid_funding", hl_funding),
    ("binance_um_universe_1h", binance_um_universe),  # last: its first backfill is the slowest
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
