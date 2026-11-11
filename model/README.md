# Liqzo market model: the hourly snapshot

**Research only. Not a signal, not advice, not a forecast of direction.** `signal: NO_TRADE`, `execution_allowed: false`.

Once an hour, after the collector has stored the hour that just closed, the workflow `model` writes:

| Path | What |
| --- | --- |
| `model/latest.json` | the corridor 4 and 24 hours ahead and twenty scenario paths for the chart on liqzo.app: BTC and ETH, and the coins admitted after them (below) |
| `model/log/YYYY-MM.csv` | one line per forecast, appended when it is published: the range was public before its hour closed |

## What the model says

`corridor_c4_sh_v1` gives ranges for the hourly close of the OKX USDT swap 4 and 24 hours ahead: an 80% range
(`lo80`, `hi80`) and a 40% range (`lo40`, `hi40`). It does not know the direction: in its tests the direction of the
median was right 47–54% of the time.

The twenty paths are real 24-hour episodes from the last year that the engine finds most similar to now, one from each
5% slice of the 24-hour outcome, each from a different day, scaled to today's volatility and to the corridor's width.
`centre` is the episode that stays closest to the hourly median all 24 hours. A path is an illustration of how the
range can be walked, not a prediction: on data already seen (06.01–26.10.2026, 14,112 forecasts) the hourly close
stayed inside the scaled 80% band 80.7–82.7% of the time at each single hour, but a whole 24-hour path stayed inside
it only 40% of the time.

## The check it is under

The rules were written down before the first hour (`forward_v1/PREREG.md`, files frozen by `forward_v1/LOCK.json`):
every hour from 2026-11-12 00:00 UTC to 2027-05-30 23:00 UTC, four criteria on how often the close lands inside the
80% range. `record` in the snapshot counts the matured forecasts of that period (`n`) and those that closed inside
(`inside`), by the same rule the final score uses. Until the verdict the model stays research.

## The coins besides BTC and ETH

The check above is about BTC and ETH and proves nothing for another coin. A coin is added only by a rule written down
before its first run (`ext_v1` in Liqzo's research records, 2026-11-11):

1. The frozen engine is given BTC and the coin, the coin in the place of its second asset; only the coin's rows are
   kept. The state is BTC's. The calibrator is the same and sees the coin's own matured forecasts only. Nothing is
   tuned for a coin.
2. Seven criteria on Binance hours (the check's four on the coin alone, 06.01–26.10.2026; each horizon and the year
   before within 76–84%; every hour 1…24 of the scaled band within 76–84%).
3. The same computation on the OKX hours stored here, from 2025-01-01: the "inside the range" marks must agree with
   the Binance run for at least 98.5% of forecasts and the median width within 2%.

| Coin | Marks agreeing with the Binance run (≥ 98.5%) | Median width, 4h / 24h / both (≤ 2%) | Inside the 80% range on OKX hours, 06.01–26.10.2026 |
| --- | --- | --- | --- |
| BNB | 98.89% | +0.50% / −0.04% / +0.32% | 81.4% |
| SOL | 99.14% | −0.51% / −0.92% / −0.94% | 80.9% |
| LINK | 99.19% | +0.52% / +1.37% / +0.52% | 80.3% |
| LTC | 99.01% | −0.09% / +0.10% / −0.20% | 80.3% |

These four passed all of it and are in the snapshot. ZEC failed one of the seven criteria (the 4-hour range across
the six blocks of the day: 8.08 points against a limit of 8) and is not shown; nothing is adjusted for it. All of
this is data already seen, not a forward check.

A coin has its own count, from its first published hour (`since`). A coin whose total share inside the 80% range
leaves 72–88% by 2027-02-19 is taken off; if the check on BTC and ETH ends badly, the model is taken off for every
coin. A coin's hour is left out — and BTC and ETH written all the same — when its candles are missing or late, when
its state would differ, or when its block would not pass the page's own checks.

Gold and the stock, ETF and pre-IPO contracts are outside this: their underlying markets close, which this model
does not know.

## How to verify

Python 3.12, `pip install -r model/requirements.txt`.

```
python model/model_live.py selftest
python model/model_live.py selftest --okx data/okx --history model/forward_v1/history/c4_binance_stretch_2025.csv.gz --full
python model/model_live.py selftest --okx data/okx --history model/forward_v1/history/c4_binance_stretch_2025.csv.gz --coins
```

The second command recomputes 400 days of hourly forecasts three ways (about five minutes) and checks that the cached
path gives the frozen code's own numbers bit for bit and that the corridor equals `corridor.py snapshot`.
Every run refuses to write if a frozen file differs from `forward_v1/LOCK.json`. The third command recomputes every
coin's hours from 2025 (about a quarter of an hour) and checks that a coin's ranges in the snapshot are the frozen
forecast and the frozen calibrator on the coin's rows, with and without the cache, and that BTC and ETH are unchanged
by the coins.

## Snapshot format (`liqzo.model/1`)

```
as_of            the closed hour the snapshot was made at (UTC)
state            "spike" (BTC's 24-hour volatility is not below its 7-day one) or "calm"
record_since     the first hour of the forward check
assets.BTC|ETH and each admitted coin the hour could be computed for
  price          the close at as_of
  h4, h24        target, lo80, hi80, lo40, hi40, multiplier, matured_in_cell
  record         h4 and h24: n, inside
  since          a coin only: the first hour it was published, where its count starts
  centre         index of the centre path in scenarios
  scenarios[]    slice 0–19, date of the past episode, d (0 centre … 1 edge), out (ends outside the 24h range), c[24] hourly closes
```

A snapshot is not replaced when the model refuses (stale candles, a missing hour): the last good one stays, and its
`as_of` says how old it is. The workflow fails, and GitHub e-mails the owner, when a closed hour is waiting and the
snapshot is more than three hours old. liqzo.app shows no model once the snapshot is older than three hours.

Data belong to their exchanges and are subject to their terms of use.
