# atlas-data

Public market data for the Atlas BTC trend-signal engine, collected by GitHub Actions twice an hour.
Only raw exchange data lives here; no signals.

## Files

| Path | What | Step | Source |
| --- | --- | --- | --- |
| `data/bitstamp/btcusd_1h_YYYY.csv` | BTC/USD candles, the engine's primary input | 1 hour | Bitstamp API; history before 2026-10-09 aggregated from Bitstamp 1-minute data |
| `data/coinbase/btc-usd_1h_YYYY.csv` | BTC cross-check | 1 hour | Coinbase Exchange API |
| `data/coinbase/{eth,sol}-usd_1h_YYYY.csv` | ETH and SOL, full history (ETH from 2016-05, SOL from 2021-06) | 1 hour | Coinbase Exchange API |
| `data/kraken/{xbt,eth,sol}usd_1h_YYYY.csv` | cross-check (last 30 days kept growing) | 1 hour | Kraken API |
| `data/binance/{bnb,paxg}usdt_1h_YYYY.csv` | BNB/USDT and PAXG/USDT (tokenised gold), full history | 1 hour | Binance public data archive and market-data API |
| `data/okx/bnb-usdt_1h_YYYY.csv` | BNB cross-check | 1 hour | OKX API |
| `data/tradfi/yahoo_{spx,ndx,gold_futures}_1d.csv`, `fred_nasdaq100_1d.csv` | S&P 500, NASDAQ-100, gold futures: candidates for a later test | 1 day | Yahoo Finance chart API; FRED as a cross-check |
| `data/deribit/btc_funding_1h.csv` | BTC-PERPETUAL funding | 1 hour | Deribit API |
| `data/deribit/btc_dvol_1h.csv` | DVOL implied-volatility index, back to 2021-03 | 1 hour | Deribit API |
| `data/okx/btc_usdt_swap_funding.csv` | BTC-USDT-SWAP funding | 8 hours | OKX API |
| `data/okx/btc_open_interest_1h.csv` | BTC contracts open interest and volume | 1 hour | OKX API |
| `data/binance/btcusdt_metrics_1h_YYYY.csv` | USDⓈ-M futures open interest, long/short ratios, taker volume | 1 hour | Binance public data archive |
| `data/sentiment/fear_greed_1d.csv` | Crypto Fear & Greed index | 1 day | alternative.me |
| `data/status.json` | last run, per-source health, cross-exchange price check | | collector |

Times are Unix seconds, UTC, at the candle open. A candle is stored only after it has closed, and rows
older than 48 hours are never rewritten.

## Health

`data/status.json` lists every source with `ok`, `last_row`, `fails_in_row` and the last error. The
`cross_venue` block compares hourly closes of Coinbase and Kraken with Bitstamp over the last 48 hours.
The workflow fails, and GitHub e-mails the owner, if Bitstamp fails three runs in a row.

Data belong to their exchanges and are subject to their terms of use.
