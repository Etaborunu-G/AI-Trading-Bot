# AI Trading Bot - Phase 3

Paper research only. No brokerage integration, credentials, order routing, leverage,
shorts, or derivatives. This is a deterministic moving-average baseline, not an AI
prediction system. Account currency is CAD; starting capital defaults to CAD $500.

## Install and run

Python 3.10+; standard library only. No dependencies or API keys required.

```powershell
python -m unittest discover -s tests -v
python bot.py --csv data/canadian_etf.csv --currency CAD --out results.json
```

Python must be installed and on PATH. In this workspace the installed interpreter was
found through `uv --cache-dir .uv-cache python find`; use its full path if necessary.
Existing Phase 1 date/close files are intentionally rejected: next-open execution
requires complete OHLCV observations.

## Historical data ingestion

Import an ascending daily CSV from a trusted source:

```csv
date,open,high,low,close,volume
2024-01-02,30.00,30.50,29.80,30.25,100000
```

That row illustrates the schema only; it is not supplied market history. Headers are
case-insensitive. Prices must be finite and positive, OHLC ranges consistent, volume
finite and nonnegative, and dates strictly increasing without duplicates. Missing
sessions are not invented or forward-filled. Supply only liquid stocks or ETFs;
the CSV format cannot verify instrument classification, exchange calendars, liquidity,
delistings, or corporate actions. Provide at least 31 rows for the default strategy.

Download and cache Stooq historical daily OHLCV:

```powershell
python bot.py --download spy.us --start 2020-01-01 --end 2024-12-31 --csv data/spy.csv --currency USD --fx-csv data/usdcad.csv --out results.json
```

Downloads are explicit network operations. A valid response is saved unchanged with
source URL, retrieval time, row count, date coverage and SHA-256 in a `.meta.json`
sidecar. Invalid responses do not replace cached CSVs. Downloads are never replaced
with synthetic data. Provider availability, licensing and adjustment conventions
must be checked independently. Stooq's adjustment status is recorded as UNVERIFIED.
See the [provider historical-data page](https://stooq.com/q/d/?s=spy.us).

Download real historical USD/CAD separately (start before the first evaluation day):

```powershell
python bot.py --download-fx --start 2019-12-20 --end 2024-12-31 --fx-csv data/usdcad.csv --currency USD
```

This uses the [Bank of Canada Valet API](https://www.bankofcanada.ca/valet/docs),
series FXUSDCAD, and retains both the original JSON and normalized CSV with hashes.
You can also import your own ascending `date,cad_per_unit` FX CSV. One USD = that
many CAD. USD instruments require FX; there is no implicit constant conversion.
CAD instruments use 1.0 and incur no FX spread. Missing or stale FX raises an error.
Only FX observations strictly before a trading date are used, including for its
closing valuation; the seven-calendar-day maximum age is configurable in Python.
This conservative lag prevents same-day FX publication leakage but introduces
valuation tracking error and does not reproduce intraday executable exchange rates.

## Execution, costs and risk

- Signal: short SMA > long SMA, calculated entirely from closes before the fill date.
  Default windows are 10 and 30 observations. Earliest fill is the open after 30 closes.
- Fills: next observed open plus/minus adverse slippage (default 5 bps each side).
  Commission defaults to CAD $1 each side; USD conversions cost 20 bps each side.
  CLI flags: `--fee`, `--slippage-bps`, `--fx-spread-bps`. These are assumptions,
  not quotes from a broker. Cash earns zero interest. Taxes are excluded.
- Whole shares, nonnegative CAD cash, no borrowing, one instrument per run. Position
  sizing includes fees and execution drag to keep entry exposure <=10% of post-fill
  NAV. Expensive instruments may produce no trades with CAD $500.
- Entry/exit capacity is capped at 1% of the previous session's volume. This is an
  estimate available before execution, not a reconstruction of an opening auction.
  Partial exits retry; severe illiquidity can leave positions exposed. Current-day
  volume/high/low are never used to size or select opening fills.
- Exposure that drifts above 10% is trimmed at the next observed open, subject to
  capacity. Daily bars cannot guarantee a continuous 10% ceiling after price/FX moves.
- A >=1% loss versus the previous close's NAV blocks entries for that session and
  requests liquidation. A close-detected loss exits no earlier than the next open.
  Liquidation remains pending until filled; after it completes the daily halt resets
  on a later session. A >=5% drawdown from observed open/close NAV peaks permanently
  halts the run and requests liquidation. Fees/slippage are included in closing NAV.
- Gaps, execution costs, unavailable liquidity and intraday moves can exceed risk
  thresholds. Daily bars do not support guaranteed stops. No future lows/highs are
  consulted to pretend a stop filled. Final holdings remain marked to market; no
  hypothetical final sale or exit fee is added. Open shares are reported explicitly.

## Benchmarks and evaluation

Every run reports the strategy and a **risk-constrained buy-and-hold** baseline over
identical dates, with identical starting capital, costs, FX, capacity and risk controls.
The baseline enters at the first eligible open, retains exposure while allowed,
trims cap breaches and re-enters after daily halts. It is not a fully invested market
index or conventional unrestricted buy-and-hold return. The 10% limit also applies
to benchmarks. Returns are portfolio CAD returns, including idle cash.

```powershell
python bot.py --csv data/spy.csv --currency USD --fx-csv data/usdcad.csv --evaluate --train-size 252 --test-size 63 --out results-evaluation.json
```

The last 20% of observations is a fixed-default-parameter chronological holdout. It
is excluded from walk-forward selection. Earlier data uses expanding training windows
and successive disjoint test blocks (default 252 initial training observations, 63
per test). Candidate SMA pairs are 5/20, 10/30, 20/60. Selection maximizes training
net CAD return; ties choose the first listed candidate. All candidates share 60
warmup rows, excluded from scored training. Test signals may use prior training
closes, but account cash, positions and risk peaks start fresh in each fold.

The report retains training scores, selected configuration, dates, trade logs, risk
events, unrounded daily NAV, cash, shares, exposure, maximum observed drawdown and
input hashes. Folds are independent CAD $500 experiments: do not compound them or
present them as one continuous strategy. The fixed holdout is a separate experiment,
not the final fold or a fitted deployment model. Repeatedly reviewing and tuning on
holdout results contaminates it; reserve a new period before making further claims.
No Sharpe or annualized statistics are inferred from short samples.

## Architecture and validation

- `market_data.py`: immutable bars, CSV/FX validation, explicit provider downloads,
  cached source data and provenance.
- `bot.py`: immutable validated configuration, single-instrument accounting, risk
  state, constrained benchmark, chronological evaluation and CLI.
- `tests/test_bot.py`: deterministic synthetic fixtures for accounting and causality;
  mocked provider responses test ingestion without depending on network access.

Tests cover next-open prices, prefix invariance under future-price changes, holdout
isolation, missing/stale FX and strictly prior rates, costs, whole-share sizing,
cash and position bounds, risk halt timing, permanent drawdown halts, volume capacity,
invalid data/configuration and provider failure preserving cached data.

## Actual verification and limitations

On November 9, 2026, the initial Phase 3 suite passed 14 tests under Python 3.15.
A real Stooq SPY download for 2020-2024 was attempted and returned HTTP 404 in this
environment. No historical equity dataset or real-market return has been fabricated.
See VERIFICATION.md for the final test and data-access record.

Corporate actions are not processed: no dividend cash flows, split share adjustments,
withholding taxes or delisting payouts. Do not mix adjusted closes with raw opens.
Vendor back-adjusted OHLC can alter historical share affordability and position sizing
and is not a point-in-time executable price history. A verified split-free raw-price
period can be used for price-return research, but omitting dividends still understates
investment returns. Full total-return and corporate-action accounting remain future
work. Data revisions, survivorship and selection bias are not solved by chronological
splits. The system has no scheduler, persistent paper account, ML model, multi-asset
portfolio or dashboard; those need separate designs before continuous automation.

### Alternative equity provider and observed runs

Yahoo Finance is available as an explicit alternative provider:

```powershell
python bot.py --provider yahoo --download F --start 2020-01-01 --end 2024-12-31 --csv data/f.csv --currency USD --fx-csv data/usdcad.csv --evaluate --out results-f.json
```

Yahoo symbols use `SPY`, `F`, or a Canadian exchange suffix such as `XIC.TO`, rather
than Stooq's `spy.us` convention. The downloader accepts only provider-classified
EQUITY/ETF instruments and rejects intervals containing reported splits. Dividend
events are counted in metadata and retained in source JSON, but are not credited
to the account. Missing OHLCV fields fail ingestion. Source JSON, normalized CSV,
metadata and hashes are cached together. Local provider sidecars are checked for
currency and content-hash mismatch before running a simulation. Imported CSVs without
metadata remain the user's responsibility to classify and verify.

Final verification: 19 tests passed. Yahoo and Bank of Canada downloads succeeded.
SPY produced no trades because a whole share exceeds the position budget. Ford's
2024 fixed-configuration holdout ended at CAD $473.53 (-5.29%); the constrained
buy-and-hold ended at CAD $493.93 (-1.21%). These omit dividends and are not total
investment returns or evidence of profitability. The XIC.TO 2020-2024 request was
rejected because Yahoo reported splits; no synthetic substitute was generated.
The full local reports are `results-historical.json` and `results-f.json`.
