import copy
import hashlib
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

from qmt.config import AppError
from qmt.__main__ import main
from qmt.forecast_diagnostic import METHODS, forecast, observed_label, run_diagnostic, yearly_metrics
from qmt.selection import load_rules
from test_automatic import fixture


def event(period='20231231', **changes):
    return {'ts_code': '600001.SH', 'end_date': period, 'ann_date': '20240401',
            'imp_ann_date': '20240501', 'div_proc': '实施', 'record_date': '20240510',
            'ex_date': '20240511', 'pay_date': '20240512', 'cash_div_tax': '.6', 'stk_div': 0, **changes}


class ForecastDiagnosticTests(unittest.TestCase):
    def test_predictions_exclude_future_disclosures_and_retain_known_year(self):
        row, dossier = fixture(day='20261231')
        stock = {'symbol': row['symbol'], 'name': row['name'], 'industry': row['industry'],
                 'selection_quote': {'trade_date': '20261231', 'close': '8', 'total_share': '.1'},
                 'selection_quality': {'evidence_hash': row['evidence_hash']}}
        before = copy.deepcopy(dossier)
        result = forecast(stock, dossier, '20261231', load_rules())
        self.assertEqual(result['known_fiscal_year'], 2025)
        self.assertEqual(result['target_year'], 2027)
        self.assertEqual(Decimal(result['estimates_dps']['conservative_minimum']), Decimal('.54'))
        self.assertEqual(Decimal(result['estimates_dps']['last_annual']), Decimal('.6'))
        self.assertEqual(dossier, before)
        dossier['data']['income'].append({**dossier['data']['income'][-1], 'ann_date': '20270102', 'n_income_attr_p': 9999999})
        dossier['data']['dividend'].append(event('20261231', ann_date='20270401', imp_ann_date='20270501', cash_div_tax=999))
        self.assertEqual(forecast(stock, dossier, '20261231', load_rules()), result)
        stock['selection_quality']['evidence_hash'] = 'wrong'
        with self.assertRaises(AppError):
            forecast(stock, dossier, '20261231', load_rules())

    def test_missing_zero_proposal_and_unfinished_year_never_become_zero_labels(self):
        for raw in ([], [event(cash_div_tax=0, div_proc='预案')], [event('20230630')]):
            label = observed_label(raw, '20221230', 2023, '20261106')
            self.assertEqual(label['state'], 'pending_zero_or_annual_confirmation')
            self.assertIsNone(label['observed_cash_dps'])
        label = observed_label([event('20261231')], '20251231', 2026, '20261106')
        self.assertEqual(label['state'], 'pending_year_incomplete')
        self.assertIsNone(label['observed_cash_dps'])

    def test_proxy_sums_separate_fiscal_payments_without_claiming_ordinary_verification(self):
        raw = [event(), event('20230630', record_date='20230901', ex_date='20230904', pay_date='20230904',
                             ann_date='20230801', imp_ann_date='20230825', cash_div_tax='.2')]
        before = copy.deepcopy(raw)
        label = observed_label(raw, '20221230', 2023, '20261106')
        self.assertEqual(label['state'], 'proxy_total_cash')
        self.assertEqual(Decimal(label['observed_cash_dps']), Decimal('.8'))
        self.assertFalse(label['ordinary_verified'])
        self.assertEqual(raw, before)
        raw.append(event('20241231', cash_div_tax=999, ann_date='20250401', imp_ann_date='20250501'))
        self.assertEqual(observed_label(raw, '20221230', 2023, '20261106'), label)

    def test_future_implementation_and_unpaid_cash_do_not_score(self):
        self.assertEqual(observed_label([event(imp_ann_date='20261201')], '20221230', 2023, '20261106')['state'],
                         'pending_zero_or_annual_confirmation')
        label = observed_label([event(pay_date='20261110')], '20221230', 2023, '20261106')
        self.assertEqual(label['state'], 'pending_payment')
        self.assertIsNone(label['observed_cash_dps'])

    def test_previous_fiscal_year_stock_action_and_same_payment_action_block_proxy(self):
        for action in (event('20221231', ann_date='20230401', imp_ann_date='20230501',
                             record_date='20230510', ex_date='20230511', pay_date='20230512', stk_div='.3'),
                       event(stk_div='.3')):
            raw = [event(), action] if action['end_date'] != '20231231' else [action]
            label = observed_label(raw, '20221230', 2023, '20261106')
            self.assertEqual(label['state'], 'pending_stock_actions')
            self.assertIsNone(label['observed_cash_dps'])
        after = event('20241231', ann_date='20250401', imp_ann_date='20250501',
                      record_date='20250510', ex_date='20250511', pay_date='20250512', stk_div='.3')
        self.assertEqual(observed_label([event(), after], '20221230', 2023, '20261106')['state'], 'proxy_total_cash')

    def test_conflicting_or_invalid_cash_records_remain_pending(self):
        for raw in ([event(), event(cash_div_tax='.7')], [event(cash_div_tax=-1)], [event(pay_date=None)], [event(None)]):
            label = observed_label(raw, '20221230', 2023, '20261106')
            self.assertEqual(label['state'], 'pending_data_conflict')
            self.assertIsNone(label['observed_cash_dps'])

    def test_verified_zero_requires_matching_source_and_later_confirmation_date(self):
        raw = [event(cash_div_tax=0, div_proc='预案')]
        proof = {'symbol': '600001.SH', 'target_year': 2023, 'cash_dps': '0', 'available_on': '20260428',
                 'source': 'https://example.com/annual.pdf', 'source_sha256': 'a' * 64, 'page': 2,
                 'target_rows_sha256': hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest(), 'note': '测试原文'}
        with patch('qmt.forecast_diagnostic.VERIFIED_ZERO', [proof]):
            before = observed_label(raw, '20221230', 2023, '20260427', symbol='600001.SH')
            self.assertEqual(before['state'], 'pending_zero_or_annual_confirmation')
            after = observed_label(raw, '20221230', 2023, '20260428', symbol='600001.SH')
            self.assertEqual(after['state'], 'verified_zero_cash')
            self.assertEqual(after['observed_cash_dps'], '0')
            self.assertTrue(after['ordinary_verified'])
            self.assertEqual(after['available_on'], '20260428')
            changed = observed_label([event()], '20221230', 2023, '20260428', symbol='600001.SH')
            self.assertEqual(changed['state'], 'pending_data_conflict')
            self.assertEqual(observed_label(raw, '20221230', 2023, '20260428', symbol='different')['state'],
                             'pending_zero_or_annual_confirmation')
        for changes in ({'source': 'javascript:alert(1)'}, {'source_sha256': 'wrong'}, {'page': 0}, {'cash_dps': '1'}):
            with patch('qmt.forecast_diagnostic.VERIFIED_ZERO', [{**proof, **changes}]):
                self.assertEqual(observed_label(raw, '20221230', 2023, '20260428', symbol='600001.SH')['state'], 'pending_data_conflict')

    def test_metrics_use_paired_samples_signed_errors_and_missing_is_not_zero(self):
        estimates = {method: '.6' for method in METHODS}
        rows = [{'origin': '20221230', 'price': '10', 'estimates_dps': estimates,
                 'label': {'state': 'proxy_total_cash', 'observed_cash_dps': actual}} for actual in ('.5', '.9')]
        rows.append({'origin': '20221230', 'price': '1', 'estimates_dps': {k: '999' for k in METHODS},
                     'label': {'state': 'pending_zero_or_annual_confirmation', 'observed_cash_dps': None}})
        output = yearly_metrics(rows)
        for method in METHODS:
            self.assertEqual(output[0]['metrics'][method], {'count': 2, 'bias_yield_bp': '-100.00',
                'mae_yield_bp': '200.00', 'over': 1, 'under': 1, 'exact': 0})
            self.assertIsNone(output[-1]['metrics'][method]['mae_yield_bp'])
        self.assertEqual(output[0]['candidates'], 3)
        self.assertEqual(output[0]['proxy_scored'], 2)

    def test_changed_engine_and_outside_source_do_not_overwrite_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            dest = workspace / 'forecast-diagnostic.json'
            dest.write_text('preserve', encoding='utf8')
            for version in ('', '../elsewhere', 'nested/version'):
                with self.assertRaises(AppError):
                    run_diagnostic(workspace, version)
            source = workspace / 'backtests' / 'test'
            source.mkdir(parents=True)
            (source / 'protocol.json').write_text(json.dumps({'version': 'test', 'pool_refresh': 'year_end', 'cohort_limit': None,
                'earnings_basis': 'conservative_minimum', 'account_config': {}, 'rules': {}, 'engine_sha256': 'old'}), encoding='utf8')
            with patch('qmt.forecast_diagnostic.protocol', return_value={'engine_sha256': 'new'}), self.assertRaises(AppError):
                run_diagnostic(workspace, 'test')
            self.assertEqual(dest.read_text(encoding='utf8'), 'preserve')
            for wrong in ([], None, {'version': 'different'}):
                (source / 'protocol.json').write_text(json.dumps(wrong), encoding='utf8')
                with self.assertRaises(AppError):
                    run_diagnostic(workspace, 'test')
                self.assertEqual(dest.read_text(encoding='utf8'), 'preserve')

    def test_cli_diagnostic_never_opens_a_ledger(self):
        with tempfile.TemporaryDirectory() as temp, patch('qmt.__main__.Ledger', side_effect=AssertionError('ledger opened')):
            with patch('qmt.forecast_diagnostic.run_diagnostic', return_value={'coverage': {'predictions': 0}}) as run:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(['--workspace', temp, 'forecast-check']), 0)
                run.assert_called_once_with(Path(temp).resolve(), 'historical-dividend-v28-annual-all-rank')
            self.assertFalse((Path(temp) / 'ledger.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
