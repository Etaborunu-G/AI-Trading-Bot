"""Synthetic fixtures test mechanics, not investment performance."""
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from io import BytesIO
from unittest.mock import patch
from bot import Config, backtest, comparison, evaluate
from market_data import Bar, parse_prices, download_stooq, load_prices


def bars(n=100, price=10):
    return [Bar(date(2020,1,1)+timedelta(days=i), price, price, price, price, 10000) for i in range(n)]


class BotTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(short_window=2, long_window=3, fee_per_order=0, slippage_bps=0)

    def test_flat(self):
        r = backtest(bars(), self.cfg)
        self.assertEqual(r['final_equity'], 500)
        self.assertEqual(r['trades'], [])

    def test_next_open(self):
        data = bars()
        data[1] = replace(data[1], close=11, high=11)
        data[2] = replace(data[2], close=12, high=12)
        data[3] = replace(data[3], open=13, high=99, close=99)
        r = backtest(data, self.cfg)
        self.assertEqual(r['trades'][0]['date'], data[3].date.isoformat())
        self.assertEqual(r['trades'][0]['price'], 13)

    def test_prefix_invariance(self):
        data = bars()
        a = backtest(data[:50], self.cfg, benchmark=True)
        data[50:] = [replace(b, open=100, high=100, low=100, close=100) for b in data[50:]]
        b = backtest(data, self.cfg, benchmark=True)
        self.assertEqual(a['equity_curve'], b['equity_curve'][:47])
        self.assertEqual(a['trades'], [t for t in b['trades'] if t['date'] <= data[49].date.isoformat()])

    def test_cap_costs_cash(self):
        r = backtest(bars(), replace(self.cfg, fee_per_order=1, slippage_bps=10), benchmark=True)
        self.assertEqual(r['open_shares'], 4)
        self.assertAlmostEqual(r['final_equity'], 498.96)
        self.assertLessEqual(r['equity_curve'][0]['exposure_fraction'], .1)
        self.assertTrue(all(x['cash'] >= 0 for x in r['equity_curve']))

    def test_unaffordable(self):
        self.assertEqual(backtest(bars(price=100), self.cfg, benchmark=True)['trades'], [])

    def test_fx_prior_only_and_stale(self):
        data = bars(6)
        fx = [(data[0].date, 1.3), (data[3].date, 2.)]
        cfg = replace(self.cfg, quote_currency='USD', fx_spread_bps=0)
        r = backtest(data, cfg, fx, benchmark=True)
        self.assertEqual(r['trades'][0]['fx'], 1.3)
        self.assertEqual(r['equity_curve'][1]['fx'], 2.)
        with self.assertRaises(ValueError): backtest(data, cfg)
        with self.assertRaises(ValueError): backtest(bars(30), cfg, fx)

    def test_close_halt_next_open(self):
        data = bars(7)
        data[3] = replace(data[3], close=8, low=8)
        r = backtest(data, self.cfg, benchmark=True)
        self.assertEqual(r['trades'][1]['date'], data[4].date.isoformat())
        self.assertEqual(r['trades'][1]['reason'], 'daily_loss')

    def test_gap_drawdown_permanent_halt(self):
        data = bars(10)
        data[4:] = [replace(b, open=1, high=1, low=1, close=1) for b in data[4:]]
        r = backtest(data, self.cfg, benchmark=True)
        self.assertTrue(r['halted'])
        self.assertEqual(len([t for t in r['trades'] if t['side']=='BUY']), 1)
        self.assertGreater(r['max_drawdown_pct'], 5)

    def test_prior_volume(self):
        data = bars(7)
        data[2] = replace(data[2], volume=0)
        r = backtest(data, self.cfg, benchmark=True)
        self.assertEqual(r['trades'][0]['date'], data[4].date.isoformat())

    def test_bad_config(self):
        for kw in [dict(max_position_fraction=.11), dict(daily_loss_limit=.02), dict(max_drawdown=.06), dict(fee_per_order=-1), dict(initial_cash=float('nan')), dict(short_window=30), dict(quote_currency='EUR')]:
            with self.assertRaises(ValueError): Config(**kw)

    def test_benchmark_same_dates(self):
        r = comparison(bars(), self.cfg, None, 3, 100)
        self.assertEqual(r['strategy']['start'], r['buy_hold']['start'])
        self.assertEqual(r['buy_hold']['starting_cash'], 500)

    def test_walk_forward_holdout_isolation(self):
        data = bars()
        a = evaluate(data, self.cfg, train_size=20, test_size=10, candidates=[(2,3),(3,5)])
        data[80:] = [replace(b, close=20, high=20) for b in data[80:]]
        b = evaluate(data, self.cfg, train_size=20, test_size=10, candidates=[(2,3),(3,5)])
        self.assertEqual(a['walk_forward'], b['walk_forward'])
        for f in a['walk_forward']:
            self.assertLess(f['train_end'], f['results']['strategy']['start'])
            self.assertLess(f['results']['strategy']['end'], a['holdout']['strategy']['start'])

    def test_csv_validation(self):
        h = 'date,open,high,low,close,volume\n'
        g = '2020-01-01,10,11,9,10,100\n'
        self.assertEqual(len(parse_prices(h+g)), 1)
        for text in [h+g+g,h+'2020-01-01,10,9,8,10,1',h+'2020-01-01,nan,11,9,10,1','date,close\n2020-01-01,10',h+'2020-01-01,10,11,9,10,-1']:
            with self.assertRaises(ValueError): parse_prices(text)

    def test_download_provenance_failure_preserves_cache(self):
        raw = b'Date,Open,High,Low,Close,Volume\n2020-01-01,10,11,9,10,100\n'
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'prices.csv'
            with patch('market_data.urlopen', return_value=BytesIO(raw)):
                meta = download_stooq('test.us',date(2020,1,1),date(2020,1,2),target)
            self.assertEqual(meta['rows'], 1)
            self.assertEqual(len(meta['sha256']), 64)
            self.assertEqual(load_prices(target)[0].close, 10)
            with patch('market_data.urlopen', return_value=BytesIO(b'No data')):
                with self.assertRaises(ValueError): download_stooq('test.us',date(2020,1,1),date(2020,1,2),target)
            self.assertEqual(target.read_bytes(), raw)

class AdditionalTests(unittest.TestCase):
    def test_boc_download_normalizes_and_keeps_source(self):
        from market_data import download_boc_fx, load_fx
        raw = b'{"observations":[{"d":"2020-01-01","FXUSDCAD":{"v":"1.3"}},{"d":"2020-01-02"}]}'
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'fx.csv'
            with patch('market_data.urlopen', return_value=BytesIO(raw)):
                meta = download_boc_fx(date(2020,1,1),date(2020,1,2),p)
            self.assertEqual(meta['rows'],1)
            self.assertEqual(load_fx(p),[(date(2020,1,1),1.3)])
            self.assertEqual(p.with_suffix('.csv.source.json').read_bytes(),raw)

    def test_position_drift_trim(self):
        data = bars(7)
        data[4:] = [replace(b, open=20, high=20, low=20, close=20) for b in data[4:]]
        cfg = Config(short_window=2,long_window=3,fee_per_order=1,slippage_bps=5)
        r = backtest(data,cfg,benchmark=True)
        self.assertEqual(r['trades'][1]['reason'],'position_trim')
        self.assertLessEqual(r['equity_curve'][1]['exposure_fraction'], .10)

    def test_partial_daily_exit_remains_pending(self):
        data = bars(10)
        data[3] = replace(data[3],close=8,low=8,volume=100)
        data[4] = replace(data[4],volume=100)
        data[5] = replace(data[5],volume=100)
        data[6] = replace(data[6],volume=100)
        cfg = Config(short_window=2,long_window=3,fee_per_order=0,slippage_bps=0)
        r = backtest(data,cfg,benchmark=True)
        exits = [t for t in r['trades'] if t['side']=='SELL']
        self.assertEqual([t['reason'] for t in exits[:4]], ['daily_loss']*4)
        self.assertEqual([t['shares'] for t in exits[:4]], [1]*4)

    def test_training_selection_ignores_next_test(self):
        cfg = Config(short_window=2,long_window=3,fee_per_order=0,slippage_bps=0)
        data = bars()
        a = evaluate(data,cfg,train_size=20,test_size=10,candidates=[(2,3),(3,5)])
        data[25:35] = [replace(b,close=20,high=20) for b in data[25:35]]
        b = evaluate(data,cfg,train_size=20,test_size=10,candidates=[(2,3),(3,5)])
        self.assertEqual(a['walk_forward'][0]['selected'],b['walk_forward'][0]['selected'])
        self.assertEqual(a['walk_forward'][0]['training_scores'],b['walk_forward'][0]['training_scores'])

    def test_yahoo_provenance_and_split_rejection(self):
        import json
        from market_data import download_yahoo
        item = dict(meta=dict(gmtoffset=0,currency='USD',instrumentType='EQUITY'), timestamp=[1577836800],
                    indicators={'quote':[dict(open=[10],high=[11],low=[9],close=[10],volume=[100])]},
                    events={'dividends':{'one':{'amount':.1}}})
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'prices.csv'
            raw = json.dumps({'chart':{'result':[item]}}).encode()
            with patch('market_data.urlopen',return_value=BytesIO(raw)):
                meta = download_yahoo('TEST',date(2020,1,1),date(2020,1,2),target)
            self.assertEqual(meta['dividend_events'],1)
            self.assertEqual(load_prices(target)[0].close,10)
            original = target.read_bytes()
            item['events']['splits'] = {'one':{'numerator':2,'denominator':1}}
            raw = json.dumps({'chart':{'result':[item]}}).encode()
            with patch('market_data.urlopen',return_value=BytesIO(raw)):
                with self.assertRaises(ValueError):
                    download_yahoo('TEST',date(2020,1,1),date(2020,1,2),target)
            self.assertEqual(target.read_bytes(),original)


if __name__ == '__main__':
    unittest.main()
