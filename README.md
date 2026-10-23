# atlas-data

Public market data for Liqzo and its research, collected by GitHub Actions twice an hour.
Only raw exchange data lives here; no signals, no accounts, no keys.

Kept up to date: the assets Liqzo trades or researches: BTC, ETH, BNB, and since 2026-10-17 SOL, LINK, LTC, ZEC and
gold (PAXG spot history, OKX XAU swap). Files of other assets stay here unchanged (frozen on 2026-10-16) so that
earlier research can be reproduced. The platform's stock and pre-IPO perpetuals are read by the page from OKX directly.

## Files kept up to date

| Path | What | Step | Source |
| --- | --- | --- | --- |
| `data/bitstamp/btcusd_1h_YYYY.csv` | BTC/USD spot, the daily trend signal's primary input, from 2014 | 1 hour | Bitstamp API; history before 2026-10-09 aggregated from Bitstamp 1-minute data |
| `data/coinbase/{btc,eth}-usd_1h_YYYY.csv` | BTC cross-check; ETH spot from 2016-05 | 1 hour | Coinbase Exchange API |
| `data/kraken/{xbt,eth}usd_1h_YYYY.csv` | cross-check | 1 hour | Kraken API |
| `data/binance/{bnb,paxg}usdt_1h_YYYY.csv` | BNB/USDT spot from 2017-11; PAXG/USDT (tokenised gold) from 2019-09 | 1 hour | Binance public data archive and market-data API |
| `data/binance/um_{btc,eth,bnb,sol,link,ltc,zec}usdt_1h_YYYY.csv`, `um_*_funding.csv` | USDⓈ-M perpetual candles with taker buy volume, and funding, from 2019-09 | 1 hour, funding 8 hours | Binance public data archive |
| `data/binance/{btc,eth,bnb}usdt_metrics_1h_YYYY.csv` | open interest, long/short ratios, taker volume, from 2021-12 | 1 hour | Binance public data archive |
| `data/okx/{btc,eth,bnb,sol,link,ltc,zec,xau}-usdt-swap_1h_YYYY.csv`, `*_usdt_swap_funding.csv` | OKX USDT perpetual swaps (the live paper books and the forward simulation) | 1 hour, funding 8 hours | OKX API |
| `data/okx/bnb-usdt_1h_YYYY.csv` | BNB spot cross-check | 1 hour | OKX API |
| `data/okx/btc_open_interest_1h.csv` | BTC contracts open interest and volume | 1 hour | OKX API |
| `data/hyperliquid/{btc,eth,bnb}_perp_{1h,4h}_YYYY.csv` | Hyperliquid perpetual candles with trade counts (execution venue candidate). The API keeps only the newest 5000 candles per interval, so 1h starts about 208 days and 4h about 833 days before 2026-10-16 | 1 hour, 4 hours | Hyperliquid info API |
| `data/hyperliquid/{btc,eth,bnb}_funding.csv` | Hyperliquid funding rate per hour and premium, from the first record | 1 hour | Hyperliquid info API |
| `data/deribit/btc_funding_1h.csv`, `btc_dvol_1h.csv` | BTC-PERPETUAL funding; DVOL implied volatility from 2021-03 | 1 hour | Deribit API |
| `data/sentiment/fear_greed_1d.csv` | Crypto Fear & Greed index | 1 day | alternative.me |
| `data/tradfi/yahoo_{spx,ndx,gold_futures}_1d.csv` | S&P 500, NASDAQ-100, gold futures; collected again since 2026-10-23 for Liqzo's paper simulation PX-2 (frozen 2026-10-16..22) | 1 day | Yahoo Finance chart API |
| `data/status.json` | last run, per-source health, cross-exchange price check | | collector |

## Frozen on 2026-10-16 (kept for reproducibility, no longer collected)

- `data/coinbase/sol-usd_1h_*`, `data/kraken/solusd_1h_*`
- `data/binance/um_*` of XRP, DOGE, TRX, TON, ADA, AVAX, 1000SHIB, BCH, DOT, NEAR; `*_metrics_1h_*` of the twelve
- `data/okx/*-usdt-swap_1h_*` and `*_usdt_swap_funding.csv` of XRP, DOGE, TRX, ADA, AVAX, SHIB, BCH, DOT, NEAR
- `data/tradfi/fred_nasdaq100_1d.csv` (NASDAQ-100 from FRED, a cross-check)

Times are Unix seconds, UTC, at the candle open (funding: the funding time). A candle is stored only after it has
closed, and rows older than 48 hours are never rewritten.

## Health

`data/status.json` lists every source with `ok`, `last_row`, `fails_in_row` and the last error. The
`cross_venue` block compares hourly closes over the last 48 hours: Coinbase and Kraken against Bitstamp for BTC,
Kraken against Coinbase for ETH, OKX against Binance for BNB. The workflow fails, and GitHub e-mails the owner, if
Bitstamp fails three runs in a row.

Data belong to their exchanges and are subject to their terms of use.
