import copy
import io
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from qmt.__main__ import main, parser
from qmt.config import AppError, atomic_json, load_config
from qmt.experiment import register_experiment, run_experiment
from test_broad_experiment import DAY, END, prepare, signals
from test_capital_policy_experiment import scaled


def context(experiment):
    return {**experiment,
            'version': experiment['version'].replace('historical-dividend-v33', 'historical-dividend-v35', 1)+'-pe-context',
            'valuation_policy': 'historical_context'}


class ValuationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prepared, self.client, self.codes, experiment = prepare(Path(self.temp.name), True, expensive_prefix=False)
        self.experiment = scaled(experiment)
        self.symbol = self.codes[2]

    def evaluate(self, prepared, policy):
        snapshots, audits = signals(prepared, '0.05', policy)
        row = next(a for a in audits[0]['assessments'] if a['symbol'] == self.symbol)
        return snapshots[DAY], row

    def test_only_known_high_percentile_changes_and_default_is_identical(self):
        original = signals(self.prepared, '0.05', self.experiment)
        explicit = signals(self.prepared, '0.05', {**self.experiment, 'valuation_policy': 'historical_gate'})
        self.assertEqual(original, explicit)
        for history in self.prepared[6].values():
            for quote in history:
                if quote['trade_date'] < DAY:
                    quote['pe_ttm'] = '5'
        old, old_row = self.evaluate(self.prepared, self.experiment)
        new, new_row = self.evaluate(self.prepared, context(self.experiment))
        self.assertEqual(old['candidates'], [])
        self.assertEqual(len(new['candidates']), 23)
        self.assertEqual(old_row['dimensions']['valuation']['state'], '未通过')
        self.assertEqual(new_row['dimensions']['valuation']['state'], '通过')
        self.assertEqual(Decimal(new_row['dimensions']['valuation']['percentile']), 1)
        self.assertEqual(new_row['dimensions']['valuation']['policy'], 'historical_context')
        for field in ('evidence_hash', 'estimated_yield', 'cash_decision', 'fcf_coverage', 'coarse_pass'):
            self.assertEqual(old_row[field], new_row[field])
        self.assertEqual(old_row['dimensions']['dividend'], new_row['dimensions']['dividend'])
        self.assertEqual(old_row['dimensions']['liquidity'], new_row['dimensions']['liquidity'])
        self.assertEqual(old['freeze'], new['freeze'])

    def test_data_boundaries_and_other_valuation_gates_remain(self):
        for case, expected in (('249_samples', '待核实'), ('250_samples', '通过'),
                               ('4_peers', '待核实'), ('5_peers', '通过'),
                               ('nonpositive_pe', '待核实'), ('absolute_pe', '未通过'),
                               ('industry_pe', '未通过'), ('normalized_pe', '未通过')):
            with self.subTest(case=case):
                prepared = copy.deepcopy(self.prepared)
                history = prepared[6][self.symbol]
                current = next(q for q in history if q['trade_date'] == DAY)
                if case.endswith('_samples'):
                    count = int(case.split('_')[0])
                    prepared[6][self.symbol] = [q for q in history if q['trade_date'] < DAY][:count]+[current]
                elif case.endswith('_peers'):
                    count = int(case.split('_')[0])
                    for code in self.codes[count:]:
                        prepared[1].prices.return_value[code]['pe_ttm'] = None
                elif case == 'nonpositive_pe':
                    current['pe_ttm'] = '0'
                elif case == 'absolute_pe':
                    for quote in prepared[1].prices.return_value.values():
                        quote['pe_ttm'] = '30'
                    current['pe_ttm'] = '26'
                elif case == 'industry_pe':
                    current['pe_ttm'] = '11'
                else:
                    current['close'] = '30'
                snapshot, row = self.evaluate(prepared, context(self.experiment))
                valuation = row['dimensions']['valuation']
                self.assertEqual(valuation['state'], expected)
                self.assertEqual(self.symbol in [c['symbol'] for c in snapshot['candidates']], expected == '通过')
                if case == 'normalized_pe':
                    self.assertGreater(Decimal(valuation['normalized_pe']), 25)
                if case == 'absolute_pe':
                    self.assertLessEqual(26, Decimal(valuation['industry_median_pe']))

    def test_half_percentile_and_future_quotes_do_not_change_history(self):
        for quote in self.prepared[6][self.symbol]:
            if quote['trade_date'] < DAY:
                quote['pe_ttm'] = '10'
        self.prepared[6][self.symbol].append({'ts_code': self.symbol, 'trade_date': END, 'pe_ttm': '1'})
        old, old_row = self.evaluate(self.prepared, self.experiment)
        new, new_row = self.evaluate(self.prepared, context(self.experiment))
        self.assertEqual(old['candidates'], new['candidates'])
        for row in (old_row, new_row):
            self.assertEqual(Decimal(row['dimensions']['valuation']['percentile']), Decimal('.5'))
            self.assertEqual(row['dimensions']['valuation']['state'], '通过')

    def test_invalid_policy_stops_before_registration_or_fetch(self):
        for change in ({'valuation_policy': None}, {'valuation_policy': []}, {'valuation_policy': 'unknown'},
                       {'capital_policy': 'fixed_amount'}, {'earnings_basis': 'ttm'}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp, \
                 patch('qmt.experiment.prepare_annual_experiment') as fetch:
                root = Path(temp)
                for function in (register_experiment, run_experiment):
                    with self.assertRaises(AppError):
                        function(root, END, load_config(), experiment={**context(self.experiment), **change})
                fetch.assert_not_called()
                self.assertFalse((root/'backtests').exists())

    def test_cli_is_opt_in_and_creates_separate_v35_protocol(self):
        with tempfile.TemporaryDirectory() as temp, patch('qmt.__main__.render'), \
             patch('qmt.experiment.run_experiment') as run, \
             patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
            root = Path(temp)
            atomic_json(root/'screen.json', {'as_of': END})
            prefix = ['--workspace', str(root), 'backtest']
            self.assertEqual(main(prefix+['--pe-context']), 1)
            run.assert_not_called()
            flags = ['--annual-pool', '--all-candidates', '--yield-rank', '--approved-annual-cash',
                     '--three-year-cashflow', '--equity-limits']
            self.assertEqual(main(prefix+flags+['--earnings-basis', 'ttm', '--pe-context']), 1)
            run.assert_not_called()
            for extra in ([], ['--long-history', '--position-freeze']):
                self.assertEqual(main(prefix+flags+extra), 0)
                old = run.call_args.kwargs['experiment']
                self.assertTrue(old['version'].startswith('historical-dividend-v33-'))
                self.assertEqual(main(prefix+flags+extra+['--pe-context']), 0)
                self.assertEqual(run.call_args.kwargs['experiment'], context(old))
        self.assertFalse(hasattr(parser().parse_args(['daily']), 'pe_context'))


if __name__ == '__main__':
    unittest.main()
