# Liqzo market model: the hourly snapshot

**Research only. Not a signal, not advice, not a forecast of direction.** `signal: NO_TRADE`, `execution_allowed: false`.

Once an hour, after the collector has stored the hour that just closed, the workflow `model` writes:

| Path | What |
| --- | --- |
| `model/latest.json` | the corridor for BTC and ETH 4 and 24 hours ahead, and twenty scenario paths for the chart on liqzo.app |
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

## How to verify

Python 3.12, `pip install -r model/requirements.txt`.

```
python model/model_live.py selftest
python model/model_live.py selftest --okx data/okx --history model/forward_v1/history/c4_binance_stretch_2025.csv.gz --full
```

The second command recomputes 400 days of hourly forecasts three ways (about five minutes) and checks that the cached
path gives the frozen code's own numbers bit for bit and that the corridor equals `corridor.py snapshot`.
Every run refuses to write if a frozen file differs from `forward_v1/LOCK.json`.

## Snapshot format (`liqzo.model/1`)

```
as_of            the closed hour the snapshot was made at (UTC)
state            "spike" (BTC's 24-hour volatility is not below its 7-day one) or "calm"
record_since     the first hour of the forward check
assets.BTC|ETH
  price          the close at as_of
  h4, h24        target, lo80, hi80, lo40, hi40, multiplier, matured_in_cell
  record         h4 and h24: n, inside
  centre         index of the centre path in scenarios
  scenarios[]    slice 0–19, date of the past episode, d (0 centre … 1 edge), out (ends outside the 24h range), c[24] hourly closes
```

A snapshot is not replaced when the model refuses (stale candles, a missing hour): the last good one stays, and its
`as_of` says how old it is. The workflow fails, and GitHub e-mails the owner, when a closed hour is waiting and the
snapshot is more than three hours old. liqzo.app shows no model once the snapshot is older than three hours.

Data belong to their exchanges and are subject to their terms of use.
