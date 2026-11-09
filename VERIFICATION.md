# Phase 3 verification record

Executed November 9, 2026 in this workspace. Python 3.15, standard library only.

## Automated checks

`python -m unittest discover -s tests -v`: 19 tests passed (final rerun below).
Synthetic unit-test fixtures verify mechanics only; none are presented as historical
market returns. Network responses are mocked in unit tests. Actual provider calls
were separately exercised through the CLI.

## Actual provider calls

- Stooq SPY daily OHLCV, 2020-01-01 through 2024-12-31: HTTP 404; no data cached.
- Bank of Canada FXUSDCAD, 2019-12-20 through 2024-12-31: 1,252 observations cached
  in data/usdcad.csv, original JSON and metadata alongside it.
- Yahoo Finance SPY, 2020-01-01 through 2024-12-31: 1,258 daily observations,
  actual coverage 2020-01-02 through 2024-12-31; 20 dividend events retained in source.
- Yahoo Finance XIC.TO, same requested dates: rejected because provider reported
  split events. No XIC result was produced.
- Yahoo Finance F, same dates: 1,258 observations with the same coverage; 14 dividend
  events retained in source. No split events reported in the requested interval.

Cached normalized price SHA-256:

- SPY: af2b5a61769f9447e7cd4636fd86559111c8909b774b2e6911a58d266fc31345
- F: c75d38511781f4805efd5205b01a1e8b66882afda7703d419954430ad7b05de0
- USD/CAD: abe52e50f5a03b7987b6183ab0fb5dea11f62bcf814ba337acaa32f9fa4e6427

All source URLs, raw-response hashes and UTC retrieval times are in data/*.meta.json.
Market-data snapshots and full reports are included in the repository for exact
reproduction. Rerunning downloads may produce different data if the provider revises
history. Python caches and temporary tooling files remain excluded. No broker or
live-order service was contacted.

## Actual out-of-sample results

Final 20% holdout: 2024-01-02 through 2024-12-31. Initial CAD $500 per account.
Default SMA 10/30, CAD $1 commission per fill, 5 bps slippage and 20 bps FX spread.
No dividends credited; these are incomplete price-return research results.

| Instrument / model | Final CAD NAV | Return | Max observed drawdown | Fills | Ending shares |
| --- | ---: | ---: | ---: | ---: | ---: |
| SPY strategy | 500.00 | 0.00% | 0.00% | 0 | 0 |
| SPY constrained hold | 500.00 | 0.00% | 0.00% | 0 | 0 |
| F strategy | 473.53 | -5.29% | 5.29% | 12 | 0 |
| F constrained hold | 493.93 | -1.21% | 3.18% | 4 | 3 |

SPY is unaffordable under a CAD $50 initial position budget and whole-share sizing.
Ford's strategy reached its permanent drawdown halt; losses exceeded the trigger
because a threshold cannot guarantee the next execution price or avoid exit costs.
The constrained hold retains shares, valued without a final sale or exit commission.
It is a risk-matched baseline, not unrestricted fully invested buy-and-hold.

Each report also contains 12 independent walk-forward test folds before the holdout.
Their individual scores, selected windows, full trades and NAV curves are retained;
no combined return is claimed. This is an implementation check, not a strategy
recommendation. Instrument choices tested data access and affordability and were
not a pre-registered universe. Corporate actions, dividend accounting, revisions,
liquidity approximation, survivorship bias and intraday risk remain limitations.

## Reproduce from cached data

```powershell
python -m unittest discover -s tests -v
python bot.py --csv data/spy.csv --currency USD --fx-csv data/usdcad.csv --evaluate --out results-historical.json
python bot.py --csv data/f.csv --currency USD --fx-csv data/usdcad.csv --evaluate --out results-f.json
```

The Python engine is single-instrument and paper-only. Continuous automation,
persistent simulated accounts, a React dashboard and AI predictions remain future
work. No live-trading path exists.
