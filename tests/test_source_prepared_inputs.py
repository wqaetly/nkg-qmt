import copy
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from qmt import prepared_inputs as adapter
from qmt.config import load_config
from qmt.experiment import ANNUAL_EXPERIMENT, protocol
from qmt.market import Tushare
from qmt.research import DISCLOSURES


class SourcePreparedInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.workspace = self.root / 'data'
        self.end = '20261106'; self.day = '20241231'; self.code = '600123.SH'
        self.a, self.b, self.other = '600971.SH', '600295.SH', '600999.SH'
        self.days = ['20221230', '20231229', self.day, '20251231']
        self.cfg = {**load_config(), 'paper_cash': '100000', 'paper_max_position': '5000', 'paper_max_sector': '20000'}
        self.old = {**ANNUAL_EXPERIMENT, 'version': 'source-old', 'cohort_limit': None,
                    'entry_mode': 'yield_rank', 'entry_yields': ['0.05'], 'dividend_policy': 'approved_annual_cash',
                    'cashflow_policy': 'three_year_coverage', 'capital_policy': 'equity_ratio',
                    'exit_policy': 'known_failure_exit'}
        self.new = {**self.old, 'version': 'source-new'}
        self.source = self.workspace / 'backtests' / self.old['version']
        self.target = self.workspace / 'backtests' / self.new['version']
        self.pinned = {}; self.plan_name = 'probe-plan.json'; self.probe_name = 'probe.json'; self.coverage_name = 'coverage.json'
        self.old_quality = {'eligible': False, 'state': '待数据核实', 'reason': 'annual scope', 'evidence_hash': 'old'}
        self.new_quality = {'eligible': True, 'state': '等待价格', 'reason': 'price', 'evidence_hash': 'new'}
        self.raw = copy.deepcopy(next(p['expected'] for p in DISCLOSURES['cash_basis'] if p['expected']['ts_code'] == self.code))
        self.later = {**self.raw, 'end_date': '20241231', 'ann_date': '20250520', 'imp_ann_date': '20250602',
                      'record_date': '20250610', 'ex_date': '20250611', 'pay_date': '20250611', 'cash_div_tax': 0.2}
        self.raw_rows = [self.raw, self.later]
        self.quote = {'ts_code': self.code, 'trade_date': self.day, 'close': 8.56, 'pe_ttm': 12.537, 'dv_ttm': 8.8323}
        industry = [{'in_date': '20000101', 'out_date': None, 'l1_code': 'coal', 'l1_name': '煤炭'}]
        names = [{'ts_code': self.code, 'name': '兰花科创', 'start_date': '20000101', 'end_date': None, 'ann_date': '20000101'}]
        stock = lambda s: {'symbol': s, 'name': s, 'industry': 'coal 煤炭', 'selection_quote': {'close': 1},
                           'selection_quality': {'eligible': True}}
        rebalances = []
        for i, day in enumerate(self.days):
            codes = [self.a, self.code, self.b] if i < 2 else [self.a, self.b]
            rejected = [{'symbol': self.other, 'reason': 'unrelated'}]
            if i >= 2: rejected.insert(0, {'symbol': self.code, **self.old_quality})
            until = self.days[i + 1] if i + 1 < len(self.days) else None
            rebalances.append({'day': day, 'until': until, 'symbols': codes, 'candidate_count': 4,
                               'evaluated_count': 4, 'rejected': rejected})
            cohort = {'selected_on': day, 'stocks': [stock(s) for s in codes], 'rejected': rejected,
                      'candidate_count': 4, 'evaluated_count': 4, 'target_count': None, 'holding_limit': 20}
            sessions = [d for d in [*self.days, self.end] if d >= day]
            payload = self.make_raw(cohort, codes, sessions, industry, names)
            if i >= 2: payload['amounts'][self.code] = {day: '123456789.123456789'}  # Original coarse screening context.
            self.write_bundle(Path('cohorts') / day, payload, {**self.old, 'version': self.old['version'] + '/cohorts/' + day,
                                                           'selection_day': day})
        cohort = {'selected_on': self.days[0], 'stocks': [stock(s) for s in [self.a, self.code, self.b]],
                  'rebalances': rebalances, 'target_count': None, 'holding_limit': 20}
        parent = self.make_raw(cohort, [self.a, self.code, self.b], [*self.days, self.end], industry, names)
        parent['market']['universe_membership'] = {
            self.code: [{'start': self.days[0], 'end': self.days[1]}, {'start': self.days[1], 'end': self.day}]}
        parent['valuation-history'][self.code].insert(0, {'trade_date': '20210101', 'pe_ttm': 9})
        parent['amounts'][self.code]['20241031'] = '10'
        self.write_bundle(Path('.'), parent, self.old)
        requests = []
        def cache_call(api, params, fields):
            rows = copy.deepcopy(self.raw_rows) if api == 'dividend' else []
            key = hashlib.sha256(json.dumps([api, params, fields], sort_keys=True).encode()).hexdigest()
            name = 'data/backtests/source-cache/' + api + '-' + key + '.json'
            self.write(name, {'api': api, 'params': params, 'fields': fields, 'rows': rows})
            requests.append({'api': api, 'params': params, 'fields': fields, 'path': name,
                             'sha256': self.pinned[name], 'row_count': len(rows)})
            return rows
        client = Tushare(); client.call = cache_call
        self.dossier = client.dossier(self.code, self.end, start_year=2019)
        source_codes = {}
        for name in ('qmt/research.py', 'qmt/automatic.py', 'qmt/experiment.py', 'qmt/disclosures.json'):
            self.write(name, {'registered_fixture': name}); source_codes[name] = self.pinned[name]
        version = self.old['version'] + '/cohorts/' + self.day
        self.plan = {'current_code_sha256': source_codes, 'cases': [{'version': version, 'symbol': self.code,
                     'basic': {'name': '兰花科创'}, 'quote': self.quote}]}
        self.write(self.plan_name, self.plan)
        self.probe = {'status': 'cohort_financial_source_comparison_complete', 'returns_computed': False,
                      'orders_computed': False, 'plan': {'path': self.plan_name, 'sha256': self.pinned[self.plan_name]},
                      'cases': [{'version': version, 'day': self.day, 'symbol': self.code,
                                 'old_registry': {'quality': self.old_quality},
                                 'new_registry': {'quality': self.new_quality, 'summary': {'latest_period': '20240930'}},
                                 'old_cohort_exact_match': True, 'quality_changed': True, 'eligible_changed': True}],
                      'cohorts': [{'version': version, 'original_candidate_order': [self.a, self.code, self.b, self.other]}]}
        self.write(self.probe_name, self.probe)
        self.coverage = {'status': 'source_inputs_covered_no_replay', 'files_sha256': {},
                         'probe': {'plan_path': self.plan_name, 'plan_sha256': self.pinned[self.plan_name],
                                   'result_path': self.probe_name, 'result_sha256': self.pinned[self.probe_name]},
                         'groups': [{'baseline': self.old['version'], 'target_day': self.day, 'until': '20251231',
                                     'symbol': self.code, 'donor_path': self.source.relative_to(self.root).as_posix(),
                                     'target_child_path': (self.source / 'cohorts' / self.day).relative_to(self.root).as_posix(),
                                     'windows': {'dossier_start_year': 2019, 'dossier_as_of': self.end, 'bars_start': self.day,
                                                 'valuation_start': '20220101', 'amounts_start': '20241101'},
                                     'dossier_requests': requests, 'dossier_sha256': self.digest(self.dossier),
                                     'coverage_counts': {'bars': 3, 'valuation': 5, 'amounts': 3, 'names': 1, 'industry_intervals': 1},
                                     'target_candidate_count': 4, 'target_evaluated_count': 4,
                                     'selected_before': 2, 'selected_after': 3, 'rejected_before': 2, 'rejected_after': 1,
                                     'industry_label': 'coal 煤炭', 'parent_union_count_before': 3, 'parent_union_count_after': 3,
                                     'parent_membership_before': [{'start': self.days[0], 'end': self.days[1]},
                                                                  {'start': self.days[1], 'end': self.day}],
                                     'parent_membership_add': {'start': self.day, 'end': '20251231'},
                                     'child_existing_amount_count': 1, 'child_new_dividends': [self.later],
                                     'insertion': {'original_candidate_index': 2, 'selected_index_0based': 1,
                                                   'previous_symbol': self.a, 'next_symbol': self.b},
                                     'cash_event_metadata_paths': [(self.source / x / 'market.json').relative_to(self.root).as_posix()
                                                                  for x in (Path('.'), Path('cohorts/20221230'), Path('cohorts/20231229'))]}]}
        self.write(self.coverage_name, self.coverage)

    @staticmethod
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

    def write(self, name, data):
        path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        self.pinned[Path(name).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()

    def write_bundle(self, suffix, payload, experiment):
        for name, value in {'protocol': protocol(self.cfg, experiment, self.end), **payload}.items():
            self.write((self.source / suffix / (name + '.json')).relative_to(self.root), value)

    def make_raw(self, cohort, codes, sessions, industry, names):
        return {'cohort': cohort, 'market': {'universe_as_of': sessions[0], 'sessions': sessions,
                'bars': {s: {d: {'close': 8.56} for d in sessions} for s in codes},
                'industries': {s: 'coal 煤炭' for s in codes},
                'dividends': {s: copy.deepcopy(self.raw_rows) if s == self.code else [] for s in codes}, 'errors': {}},
                'financials': {s: {'symbol': s, 'as_of': self.end, 'start_year': 2017,
                                  'data': {'dividend': copy.deepcopy(self.raw_rows)}, 'errors': {}} for s in codes},
                'names': {s: names for s in codes},
                'valuation-history': {s: [{'trade_date': d, 'pe_ttm': 10} for d in [*self.days, self.end]] for s in codes},
                'amounts': {s: {d: '123456789.123456789' for d in sessions} for s in codes},
                'industry-history': {'stocks': {s: industry for s in codes}, 'peers': {self.code: industry}}}

    def prepare(self, **changes):
        args = {'workspace': self.workspace, 'end': self.end, 'progress': lambda *a, **k: None,
                'cfg': self.cfg, 'experiment': self.new, 'baseline': self.old['version'], 'root': self.root,
                'pinned': self.pinned, 'cohort_probe': self.probe_name, 'coverage': self.coverage_name}
        with patch('qmt.market.request_json', side_effect=AssertionError('Network must remain unused')):
            return adapter.prepare_source_corrected(**{**args, **changes})

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def read_output(self, suffix, name):
        return json.loads((self.target / suffix / (name + '.json')).read_bytes())

    def assert_refused_without_writes(self, **changes):
        before = self.snapshot()
        with self.assertRaises((RuntimeError, ValueError, FileNotFoundError)):
            self.prepare(**changes)
        self.assertEqual(self.snapshot(), before)

    def test_only_verified_member_and_metadata_change_without_old_payment(self):
        before = self.snapshot(); result = self.prepare()
        child = self.read_output(Path('cohorts') / self.day, 'cohort')
        self.assertEqual([s['symbol'] for s in child['stocks']], [self.a, self.code, self.b])
        self.assertEqual(child['stocks'][1]['selection_quality'], self.new_quality)
        self.assertEqual(child['rejected'], [{'symbol': self.other, 'reason': 'unrelated'}])
        self.assertEqual((child['candidate_count'], child['evaluated_count']), (4, 4))
        parent = self.read_output(Path('.'), 'cohort')
        old_parent = json.loads(before[(self.source / 'cohort.json').relative_to(self.root).as_posix()])
        self.assertEqual(parent['stocks'], old_parent['stocks'])
        market = self.read_output(Path('.'), 'market')
        self.assertEqual(market['universe_membership'][self.code][-1], {'start': self.day, 'end': '20251231'})
        self.assertEqual(market['bars'], json.loads(before[(self.source / 'market.json').relative_to(self.root).as_posix()])['bars'])
        self.assertEqual(market['dividends'][self.code][0]['event_kind'], 'cash_basis')
        self.assertEqual(market['dividends'][self.code][0]['correction_originals'], [self.raw])
        child_market = self.read_output(Path('cohorts') / self.day, 'market')
        self.assertEqual(child_market['dividends'][self.code], [self.later])
        self.assertEqual(list(child_market['bars'][self.code]), [self.day, '20251231', self.end])
        self.assertEqual(self.read_output(Path('cohorts') / self.day, 'financials')[self.code], self.dossier)
        self.assertNotIn('20210101', [r['trade_date'] for r in self.read_output(Path('cohorts') / self.day, 'valuation-history')[self.code]])
        self.assertNotIn('20241031', self.read_output(Path('cohorts') / self.day, 'amounts')[self.code])
        for name in ('financials', 'names', 'valuation-history', 'amounts', 'industry-history'):
            self.assertEqual((self.target / (name + '.json')).read_bytes(), (self.source / (name + '.json')).read_bytes())
        for name, content in before.items(): self.assertEqual((self.root / name).read_bytes(), content)
        self.assertIsInstance(result[7][self.code][self.day], Decimal)
        self.assertFalse((self.target / 'yield-rank-result.json').exists())
        self.assert_refused_without_writes()

    def test_policy_or_account_change_is_rejected_before_registration(self):
        for changes in ({'cfg': {**self.cfg, 'paper_cash': '200000'}},
                        {'experiment': {**self.new, 'exit_policy': 'review_freeze'}},
                        {'experiment': {**self.new, 'selection_day': '20221229'}}):
            with self.subTest(changes=changes): self.assert_refused_without_writes(**changes)

    def test_changed_source_or_missing_cache_is_rejected_without_output(self):
        path = self.root / self.coverage['groups'][0]['dossier_requests'][0]['path']
        original = path.read_bytes(); path.unlink()
        self.assert_refused_without_writes(); path.write_bytes(original + b' ')
        self.assert_refused_without_writes(); path.write_bytes(original)
        (self.root / 'qmt/research.py').write_bytes(b'changed code')
        self.assert_refused_without_writes()

    def test_registered_cache_still_requires_complete_request_fields(self):
        request = self.coverage['groups'][0]['dossier_requests'][0]
        value = json.loads((self.root / request['path']).read_bytes()); del value['rows'][0]['cash_div_tax']
        self.write(request['path'], value); request['sha256'] = self.pinned[request['path']]
        self.write(self.coverage_name, self.coverage)
        self.assert_refused_without_writes()

    def test_full_financial_page_is_rejected_without_synthesizing_fallback(self):
        request = next(r for r in self.coverage['groups'][0]['dossier_requests'] if r['api'] == 'income')
        value = json.loads((self.root / request['path']).read_bytes())
        row = {k: None for k in request['fields'].split(',')}
        row.update(ts_code=self.code, ann_date='20240101')
        value['rows'] = [row] * 100
        self.write(request['path'], value)
        request.update(sha256=self.pinned[request['path']], row_count=100)
        self.write(self.coverage_name, self.coverage)
        self.assert_refused_without_writes()

    def test_old_entitlement_cannot_be_added_to_the_new_child(self):
        self.coverage['groups'][0]['child_new_dividends'].insert(0, self.raw)
        self.write(self.coverage_name, self.coverage)
        self.assert_refused_without_writes()

    def test_changed_candidate_order_or_wrong_company_is_rejected(self):
        self.probe['cohorts'][0]['original_candidate_order'] = [self.b, self.code, self.a, self.other]
        self.write(self.probe_name, self.probe)
        self.coverage['probe']['result_sha256'] = self.pinned[self.probe_name]
        self.write(self.coverage_name, self.coverage); self.assert_refused_without_writes()
        self.probe['cohorts'][0]['original_candidate_order'] = [self.a, self.code, self.b, self.other]
        self.probe['cases'][0]['symbol'] = self.other
        self.write(self.probe_name, self.probe); self.coverage['probe']['result_sha256'] = self.pinned[self.probe_name]
        self.write(self.coverage_name, self.coverage); self.assert_refused_without_writes()

    def test_missing_donor_bar_and_existing_destination_never_create_partial_outputs(self):
        path = (self.source / 'market.json').relative_to(self.root)
        market = json.loads((self.root / path).read_bytes()); del market['bars'][self.code][self.day]
        self.write(path, market); self.assert_refused_without_writes()
        self.target.mkdir(); (self.target / 'yield-rank-result.json').write_bytes(b'prior result')
        self.assert_refused_without_writes()


if __name__ == '__main__':
    unittest.main()
