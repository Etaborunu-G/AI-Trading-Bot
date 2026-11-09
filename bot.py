"""Single-instrument, CAD-account, paper-only daily-bar research engine."""
import argparse
from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import date
import json
import math
from pathlib import Path
from statistics import mean

from market_data import download_boc_fx, download_stooq, download_yahoo, load_fx, load_prices, validate


@dataclass(frozen=True)
class Config:
    initial_cash: float = 500.0
    max_position_fraction: float = .10
    daily_loss_limit: float = .01
    max_drawdown: float = .05
    fee_per_order: float = 1.0  # CAD, per side
    slippage_bps: float = 5.0
    fx_spread_bps: float = 20.0
    short_window: int = 10
    long_window: int = 30
    quote_currency: str = 'CAD'
    max_fx_age_days: int = 7
    max_volume_fraction: float = .01

    def __post_init__(self):
        numeric = (self.initial_cash, self.max_position_fraction, self.daily_loss_limit,
                   self.max_drawdown, self.fee_per_order, self.slippage_bps,
                   self.fx_spread_bps, self.max_volume_fraction)
        if any(not math.isfinite(x) for x in numeric):
            raise ValueError('Configuration must be finite')
        if self.initial_cash <= 0 or not 0 < self.max_position_fraction <= .10:
            raise ValueError('Positive cash and position limit <=10% required')
        if not 0 < self.daily_loss_limit <= .01 or not 0 < self.max_drawdown <= .05:
            raise ValueError('Risk limits cannot exceed 1% daily / 5% drawdown')
        if not 0 <= self.fee_per_order or not 0 <= self.slippage_bps < 10000 or not 0 <= self.fx_spread_bps < 10000:
            raise ValueError('Invalid trading costs')
        if not all(isinstance(x, int) for x in (self.short_window, self.long_window, self.max_fx_age_days)):
            raise ValueError('Windows and FX age must be integers')
        if not 0 < self.short_window < self.long_window or self.max_fx_age_days < 1:
            raise ValueError('Invalid windows / FX age')
        if self.quote_currency not in ('CAD', 'USD') or not 0 < self.max_volume_fraction <= .01:
            raise ValueError('Only CAD/USD equities and <=1% prior volume participation supported')


def backtest(data, cfg=None, fx=None, start_index=None, end_index=None, benchmark=False):
    """Prior closes -> current open. FX uses strictly earlier observations even at close."""
    cfg = cfg or Config()
    validate(data)
    start = cfg.long_window if start_index is None else start_index
    end = len(data) if end_index is None else end_index
    if not cfg.long_window <= start < end <= len(data):
        raise ValueError('Insufficient warmup or invalid evaluation interval')
    if cfg.quote_currency != 'CAD' and not fx:
        raise ValueError('USD instruments require historical CAD-per-USD FX')
    if fx:
        if any(not math.isfinite(v) or v <= 0 for _, v in fx) or any(fx[i][0] >= fx[i+1][0] for i in range(len(fx)-1)):
            raise ValueError('Invalid FX series')
    fx_days = [d for d, _ in (fx or [])]
    cash, shares, peak, previous = cfg.initial_cash, 0, cfg.initial_cash, cfg.initial_cash
    dd_halt = False
    pending_halt = False
    liquidation_pending = False
    trades, curve, events = [], [], []
    max_dd = 0.0
    for i in range(start, end):
        b = data[i]
        rate = 1.0
        if cfg.quote_currency != 'CAD':
            j = bisect_left(fx_days, b.date) - 1
            if j < 0 or (b.date - fx_days[j]).days > cfg.max_fx_age_days:
                raise ValueError(f'Missing/stale prior FX for {b.date}')
            rate = fx[j][1]
        spread = cfg.fx_spread_bps / 10000 if cfg.quote_currency != 'CAD' else 0
        opening = cash + shares*b.open*rate
        daily_halt = pending_halt or liquidation_pending or opening <= previous*(1-cfg.daily_loss_limit)
        dd_halt |= opening <= peak*(1-cfg.max_drawdown)
        bullish = benchmark or mean(x.close for x in data[i-cfg.short_window:i]) > mean(x.close for x in data[i-cfg.long_window:i])
        # Capacity uses prior volume, never the as-yet-unknown current daily volume.
        capacity = math.floor(data[i-1].volume*cfg.max_volume_fraction)

        def sell(quantity, reason):
            nonlocal cash, shares
            if quantity <= 0:
                return
            fill = b.open*(1-cfg.slippage_bps/10000)
            proceeds = quantity*fill*rate*(1-spread)-cfg.fee_per_order
            # Never borrow to pay a fee. Unfilled exits remain exposed and retry.
            if cash + proceeds < 0:
                return
            cash += proceeds
            shares -= quantity
            trades.append(dict(date=b.date.isoformat(), side='SELL', shares=quantity,
                               price=fill, fx=rate, fee_cad=cfg.fee_per_order, reason=reason))

        if dd_halt or daily_halt or not bullish:
            sell(min(shares, capacity), 'drawdown' if dd_halt else 'daily_loss' if daily_halt else 'signal')
        elif shares*b.open*rate > opening*cfg.max_position_fraction:
            # Trim a drifting position at the next observed open; no intraday guarantees.
            cost_unit = b.open*rate*(cfg.slippage_bps/10000 + spread)
            numerator = shares*b.open*rate-cfg.max_position_fraction*(opening-cfg.fee_per_order)
            denominator = b.open*rate-cfg.max_position_fraction*cost_unit
            sell(min(shares, capacity, math.ceil(numerator/denominator)), 'position_trim')
        elif not shares:
            fill = b.open*(1+cfg.slippage_bps/10000)
            unit = fill*rate*(1+spread)
            # Include fee and execution drag when computing post-fill 10% NAV cap.
            q_risk = math.floor(cfg.max_position_fraction*(opening-cfg.fee_per_order) /
                                (b.open*rate+cfg.max_position_fraction*(unit-b.open*rate)))
            quantity = max(0, min(capacity, q_risk, math.floor((cash-cfg.fee_per_order)/unit)))
            if quantity:
                cash -= quantity*unit+cfg.fee_per_order
                shares = quantity
                trades.append(dict(date=b.date.isoformat(), side='BUY', shares=quantity,
                                   price=fill, fx=rate, fee_cad=cfg.fee_per_order, reason='entry'))
        nav = cash+shares*b.close*rate
        peak = max(peak, opening)
        max_dd = max(max_dd, 1-opening/peak)
        peak = max(peak, nav)
        max_dd = max(max_dd, 1-nav/peak)
        pending_halt = nav <= previous*(1-cfg.daily_loss_limit)
        liquidation_pending = (daily_halt or pending_halt) and shares > 0
        dd_halt |= nav <= peak*(1-cfg.max_drawdown)
        if daily_halt or pending_halt or dd_halt:
            events.append(dict(date=b.date.isoformat(), daily_loss=daily_halt or pending_halt, drawdown=dd_halt))
        curve.append(dict(date=b.date.isoformat(), equity=nav, cash=cash, shares=shares,
                          exposure_fraction=shares*b.close*rate/nav if nav > 0 else 0, fx=rate))
        previous = nav
    final = curve[-1]['equity']
    return dict(mode='PAPER_ONLY', starting_cash=cfg.initial_cash, final_equity=final,
                return_pct=(final/cfg.initial_cash-1)*100, max_drawdown_pct=max_dd*100,
                halted=dd_halt, open_shares=shares, trades=trades, equity_curve=curve,
                risk_events=events, config=asdict(cfg), start=data[start].date.isoformat(),
                end=data[end-1].date.isoformat(), benchmark=benchmark,
                valuation='Mark to market; no forced final sale or exit fee')


def comparison(data, cfg, fx, start, end):
    return dict(strategy=backtest(data, cfg, fx, start, end),
                buy_hold=backtest(data, cfg, fx, start, end, benchmark=True))


def evaluate(data, cfg=None, fx=None, train_size=252, test_size=63, candidates=None):
    """Expanding training windows; parameters selected only on preceding training data.

    Each test fold is a fresh CAD account. Fold returns are not a continuous portfolio.
    Last 20% is a separate fixed-configuration holdout, not used in walk-forward fitting.
    """
    from dataclasses import replace
    cfg = cfg or Config()
    candidates = candidates or [(5, 20), (10, 30), (20, 60)]
    if train_size < 1 or test_size < 1:
        raise ValueError('Training and test sizes must be positive')
    configs = [replace(cfg, short_window=s, long_window=l) for s, l in candidates]
    warmup = max(c.long_window for c in configs + [cfg])
    split = int(len(data)*.8)
    if split < warmup+train_size or split >= len(data):
        raise ValueError('Insufficient history for training and held-out evaluation')
    folds = []
    cursor = warmup+train_size
    while cursor < split:
        scores = [backtest(data, c, fx, warmup, cursor)['return_pct'] for c in configs]
        selected = configs[max(range(len(scores)), key=lambda j: scores[j])]
        stop = min(cursor+test_size, split)
        folds.append(dict(train_start=data[warmup].date.isoformat(), train_end=data[cursor-1].date.isoformat(),
                          selected=asdict(selected), training_scores=scores,
                          results=comparison(data, selected, fx, cursor, stop)))
        cursor = stop
    if not folds:
        raise ValueError('No walk-forward test observations')
    return dict(mode='PAPER_ONLY', holdout=comparison(data, cfg, fx, split, len(data)),
                walk_forward=folds, candidates=candidates,
                protocol='Final 20% fixed-config holdout; earlier expanding train/test folds; independent accounts')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv')
    parser.add_argument('--download', help='Stooq equity/ETF symbol, e.g. spy.us; data only')
    parser.add_argument('--provider', choices=['stooq', 'yahoo'], default='stooq')
    parser.add_argument('--start', type=date.fromisoformat)
    parser.add_argument('--end', type=date.fromisoformat)
    parser.add_argument('--currency', choices=['CAD', 'USD'], required=True)
    parser.add_argument('--fx-csv')
    parser.add_argument('--download-fx', action='store_true', help='Download Bank of Canada USD/CAD to --fx-csv')
    parser.add_argument('--fee', type=float, default=1.0)
    parser.add_argument('--slippage-bps', type=float, default=5.0)
    parser.add_argument('--fx-spread-bps', type=float, default=20.0)
    parser.add_argument('--evaluate', action='store_true')
    parser.add_argument('--train-size', type=int, default=252)
    parser.add_argument('--test-size', type=int, default=63)
    parser.add_argument('--out', default='results.json')
    args = parser.parse_args()
    try:
        if args.download_fx:
            if not args.start or not args.end or not args.fx_csv:
                raise ValueError('FX download requires --start, --end, --fx-csv')
            download_boc_fx(args.start, args.end, args.fx_csv)
            if not args.csv and not args.download:
                print(f'Historical USD/CAD data saved to {args.fx_csv}')
                return
        if not args.csv:
            raise ValueError('--csv is required (destination when downloading)')
        if args.download:
            if not args.start or not args.end:
                raise ValueError('Download requires --start and --end')
            download = download_stooq if args.provider == 'stooq' else download_yahoo
            metadata = download(args.download, args.start, args.end, args.csv)
            if metadata['quote_currency'] not in ('user must specify', args.currency):
                raise ValueError('Provider currency does not match --currency')
        data = load_prices(args.csv)
        source_metadata = None
        sidecar = Path(args.csv).with_suffix(Path(args.csv).suffix+'.meta.json')
        if sidecar.exists():
            import hashlib
            source_metadata = json.loads(sidecar.read_text(encoding='utf-8'))
            if source_metadata.get('quote_currency', 'user must specify') not in ('user must specify', args.currency):
                raise ValueError('Cached provider currency does not match --currency')
            if source_metadata.get('sha256') != hashlib.sha256(Path(args.csv).read_bytes()).hexdigest():
                raise ValueError('Cached price CSV does not match provenance hash')
        cfg = Config(quote_currency=args.currency, fee_per_order=args.fee,
                     slippage_bps=args.slippage_bps, fx_spread_bps=args.fx_spread_bps)
        fx = load_fx(args.fx_csv) if args.fx_csv else None
        result = evaluate(data, cfg, fx, args.train_size, args.test_size) if args.evaluate else comparison(data, cfg, fx, cfg.long_window, len(data))
        import hashlib
        result['inputs'] = dict(prices_sha256=hashlib.sha256(Path(args.csv).read_bytes()).hexdigest(),
                               fx_sha256=hashlib.sha256(Path(args.fx_csv).read_bytes()).hexdigest() if args.fx_csv else None,
                               price_source=source_metadata)
        result['limitations'] = [
            'Price-return research: dividends, taxes and corporate-action accounting excluded.',
            'Vendor historical data may be revised or retroactively adjusted.',
            'Daily observations cannot guarantee intraday risk caps or opening liquidity.',
            'Buy-and-hold is constrained by the same position and risk limits.',
            'Walk-forward folds are independent accounts, not a compounded track record.'
        ]
        Path(args.out).write_text(json.dumps(result, indent=2, allow_nan=False), encoding='utf-8')
        print(f'Paper research report saved to {args.out}')
    except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
        parser.exit(2, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
