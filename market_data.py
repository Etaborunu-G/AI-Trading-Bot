"""Validated daily OHLCV and point-in-time CAD exchange rates; stdlib only."""
import csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Bar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def validate(bars):
    if not bars:
        raise ValueError('No OHLCV observations')
    for i, b in enumerate(bars):
        if any(not math.isfinite(x) or x <= 0 for x in (b.open, b.high, b.low, b.close)):
            raise ValueError('OHLC prices must be positive and finite')
        if not math.isfinite(b.volume) or b.volume < 0:
            raise ValueError('Volume must be finite and nonnegative')
        if b.low > min(b.open, b.close) or b.high < max(b.open, b.close) or b.low > b.high:
            raise ValueError('Inconsistent OHLC range')
        if i and bars[i-1].date >= b.date:
            raise ValueError('Dates must be strictly increasing without duplicates')
    return bars


def parse_prices(text):
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    if not reader.fieldnames:
        raise ValueError('Missing CSV header')
    reader.fieldnames = [s.strip().lower() for s in reader.fieldnames]
    if not {'date', 'open', 'high', 'low', 'close', 'volume'} <= set(reader.fieldnames):
        raise ValueError('CSV requires date,open,high,low,close,volume')
    return validate([Bar(date.fromisoformat(r['date']), *(float(r[k]) for k in
                     ('open', 'high', 'low', 'close', 'volume'))) for r in reader])


def load_prices(path):
    return parse_prices(Path(path).read_text(encoding='utf-8-sig'))


def load_fx(path):
    """Rows are CAD per one quote-currency unit, observed at that day's close."""
    with open(path, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    rates = [(date.fromisoformat(r['date']), float(r['cad_per_unit'])) for r in rows]
    if not rates or any(not math.isfinite(v) or v <= 0 for _, v in rates):
        raise ValueError('FX rates must be positive and finite')
    if any(rates[i][0] >= rates[i+1][0] for i in range(len(rates)-1)):
        raise ValueError('FX dates must be strictly increasing')
    return rates


def download_boc_fx(start, end, destination):
    """Bank of Canada daily indicative USD/CAD; publication-day closes, never intraday FX."""
    if start > end:
        raise ValueError('Invalid FX date range')
    url = 'https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?' + urlencode(
        dict(start_date=start.isoformat(), end_date=end.isoformat()))
    with urlopen(url, timeout=30) as response:
        raw = response.read(20_000_001)
    if len(raw) > 20_000_000:
        raise ValueError('FX response exceeds size limit')
    payload = json.loads(raw)
    rates = [(date.fromisoformat(r['d']), float(r['FXUSDCAD']['v']))
             for r in payload['observations'] if r.get('FXUSDCAD', {}).get('v')]
    if not rates or any(not start <= d <= end or not math.isfinite(v) or v <= 0 for d, v in rates):
        raise ValueError('Invalid or empty FX response')
    if any(rates[i][0] >= rates[i+1][0] for i in range(len(rates)-1)):
        raise ValueError('FX dates must be strictly increasing')
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['date', 'cad_per_unit'])
        writer.writerows(rates)
    # Keep the original provider response alongside normalized data for audit.
    path.with_suffix(path.suffix+'.source.json').write_bytes(raw)
    metadata = dict(provider='Bank of Canada', series='FXUSDCAD', url=url, rows=len(rates),
                    retrieved_utc=datetime.now(timezone.utc).isoformat(),
                    source_sha256=hashlib.sha256(raw).hexdigest(),
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    path.with_suffix(path.suffix+'.meta.json').write_text(json.dumps(metadata, indent=2))
    return metadata


def download_stooq(symbol, start, end, destination):
    """Explicit network operation; never substitutes generated data on failure."""
    if not re.fullmatch(r'[a-zA-Z0-9.^_-]+', symbol) or start > end:
        raise ValueError('Invalid symbol or date range')
    url = 'https://stooq.com/q/d/l/?' + urlencode(dict(s=symbol.lower(), i='d',
                 d1=start.strftime('%Y%m%d'), d2=end.strftime('%Y%m%d')))
    with urlopen(url, timeout=30) as response:
        raw = response.read(20_000_001)
    if len(raw) > 20_000_000:
        raise ValueError('Provider response exceeds size limit')
    bars = parse_prices(raw.decode('utf-8-sig'))
    if any(b.date < start or b.date > end for b in bars):
        raise ValueError('Provider returned dates outside requested range')
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    metadata = dict(provider='Stooq', symbol=symbol, url=url, quote_currency='user must specify',
                    retrieved_utc=datetime.now(timezone.utc).isoformat(),
                    sha256=hashlib.sha256(raw).hexdigest(), rows=len(bars),
                    adjustment_status='UNVERIFIED: check splits/dividends with provider',
                    start=bars[0].date.isoformat(), end=bars[-1].date.isoformat())
    path.with_suffix(path.suffix + '.meta.json').write_text(json.dumps(metadata, indent=2))
    return metadata


def download_yahoo(symbol, start, end, destination):
    """Alternative public chart endpoint; vendor adjustment history is not point-in-time."""
    from datetime import timedelta
    from urllib.parse import quote
    if not re.fullmatch(r'[a-zA-Z0-9.^_-]+', symbol) or start > end:
        raise ValueError('Invalid symbol or date range')
    begin = int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp())
    finish = int(datetime.combine(end+timedelta(days=1), datetime.min.time(), timezone.utc).timestamp())
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + quote(symbol) + '?' + urlencode(
        dict(period1=begin, period2=finish, interval='1d', events='div,splits'))
    with urlopen(Request(url, headers={'User-Agent': 'phase3-paper-research/1.0'}), timeout=30) as response:
        raw = response.read(20_000_001)
    if len(raw) > 20_000_000:
        raise ValueError('Provider response exceeds size limit')
    result = json.loads(raw)['chart']['result'][0]
    if result['meta'].get('instrumentType') not in ('ETF', 'EQUITY'):
        raise ValueError('Only provider-classified equities and ETFs are supported')
    if result.get('events', {}).get('splits'):
        raise ValueError('Split events in requested interval: corporate-action accounting not supported')
    quotes = result['indicators']['quote'][0]
    offset = result['meta']['gmtoffset']
    bars = []
    for i, stamp in enumerate(result['timestamp']):
        day = datetime.fromtimestamp(stamp+offset, timezone.utc).date()
        values = [quotes[k][i] for k in ('open', 'high', 'low', 'close', 'volume')]
        if not start <= day <= end or any(v is None for v in values):
            raise ValueError('Missing OHLCV or out-of-range provider observation')
        bars.append(Bar(day, *map(float, values)))
    validate(bars)
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['date', 'open', 'high', 'low', 'close', 'volume'])
        writer.writerows((b.date, b.open, b.high, b.low, b.close, b.volume) for b in bars)
    path.with_suffix(path.suffix+'.source.json').write_bytes(raw)
    metadata = dict(provider='Yahoo Finance chart', symbol=symbol, url=url,
                    instrument_type=result['meta']['instrumentType'],
                    quote_currency=result['meta']['currency'], rows=len(bars),
                    retrieved_utc=datetime.now(timezone.utc).isoformat(),
                    source_sha256=hashlib.sha256(raw).hexdigest(),
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    adjustment_status='Vendor OHLC; not dividend-adjusted; retroactive split adjustments possible',
                    dividend_events=len(result.get('events', {}).get('dividends', {})),
                    start=bars[0].date.isoformat(), end=bars[-1].date.isoformat())
    path.with_suffix(path.suffix+'.meta.json').write_text(json.dumps(metadata, indent=2))
    return metadata
