import copy
import hashlib
import itertools
import json
import unittest

from qmt.automatic import assess, load_rules
from qmt.config import AppError, load_config
from qmt.research import evaluate_review, statements, summarize
from test_automatic import fixture


def statement(**changes):
    return {'ts_code': '600001.SH', 'end_date': '20260930',
            'ann_date': '20261030', 'report_type': '1',
            'total_share': 1000, 'total_assets': 10000, **changes}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def summarize_again(dossier, original):
    row = summarize(dossier, {'name': original['name'], 'industry': original['industry']},
                    {'trade_date': dossier['as_of'], 'close': original['price'],
                     'total_share': '.1', 'dv_ttm': 7.5})
    row['selection'] = copy.deepcopy(original['selection'])
    return row


class StatementVersionTests(unittest.TestCase):
    def test_latest_disclosure_resolves_old_conflicts_in_every_order(self):
        rows = [statement(total_assets=10000), statement(total_assets=11000),
                statement(ann_date='20261102', total_assets=12000)]
        original = copy.deepcopy(rows)
        for permutation in itertools.permutations(rows):
            with self.subTest(order=permutation):
                self.assertEqual(statements(permutation, '20261102')['20260930'], rows[-1])
                with self.assertRaisesRegex(AppError, 'total_assets'):
                    statements(permutation, '20261101')
        self.assertEqual(rows, original)

    def test_same_day_restatement_wins_before_conflict_checks(self):
        rows = [statement(total_assets=10000), statement(total_assets=11000),
                statement(report_type='4', total_assets=12000)]
        for permutation in itertools.permutations(rows):
            self.assertEqual(statements(permutation, '20261105')['20260930'], rows[-1])
        conflicting = rows + [statement(report_type='4', total_assets=13000)]
        for permutation in itertools.permutations(conflicting):
            with self.assertRaisesRegex(AppError, 'total_assets'):
                statements(permutation, '20261105')

    def test_announcement_date_precedes_report_type_and_future_rows_stay_hidden(self):
        old = statement(report_type='4', total_assets=11000)
        new = statement(ann_date='20261102', total_assets=12000)
        future = statement(report_type='4', ann_date='20261103', f_ann_date='20261110', total_assets=13000)
        for permutation in itertools.permutations([old, new, future]):
            self.assertEqual(statements(permutation, '20261101')['20260930'], old)
            self.assertEqual(statements(permutation, '20261105')['20260930'], new)
            self.assertEqual(statements(permutation, '20261110')['20260930'], future)

    def test_chosen_version_does_not_fill_nulls_from_discarded_versions(self):
        rows = [statement(total_share=1000), statement(report_type='4', total_share=None)]
        for permutation in itertools.permutations(rows):
            self.assertIsNone(statements(permutation, '20261105')['20260930']['total_share'])
        later = statement(ann_date='20261102', total_share=None)
        for permutation in itertools.permutations([rows[0], later]):
            self.assertIsNone(statements(permutation, '20261105')['20260930']['total_share'])

    def test_null_and_missing_fields_merge_deterministically(self):
        missing = statement()
        del missing['total_share']
        rows = [missing, statement(total_share=None, money_cap=None), statement(total_share=1000)]
        expected = {**statement(), 'money_cap': None}
        for permutation in itertools.permutations(rows):
            result = statements(permutation, '20261105')['20260930']
            self.assertEqual(result, expected)
            self.assertEqual(fingerprint(result), fingerprint(expected))

    def test_equal_numeric_types_have_one_hash_in_every_order(self):
        rows = [statement(total_share=1000), statement(total_share=1000.0)]
        results = [statements(order, '20261105') for order in itertools.permutations(rows)]
        self.assertEqual(len({fingerprint(result) for result in results}), 1)
        self.assertEqual(results[0]['20260930']['total_share'], 1000)
        # A second economic value must remain unknown, with order-stable audit evidence.
        audited = []
        for order in itertools.permutations(rows + [statement(total_share=1100)]):
            warnings = []
            result = statements(order, '20261105', quarterly_share_conflicts=warnings)
            self.assertIsNone(result['20260930']['total_share'])
            self.assertEqual(len(warnings[0]['originals']), 3)
            audited.append(fingerprint({'result': result, 'warnings': warnings}))
        self.assertEqual(len(set(audited)), 1)

    def test_security_and_effective_announcement_conflicts_remain_strict(self):
        base = statement(f_ann_date='20261101')
        for extra in ({**base, 'ts_code': '600002.SH'}, {**base, 'ann_date': '20261029'}):
            for permutation in itertools.permutations([base, extra]):
                with self.assertRaises(AppError):
                    statements(permutation, '20261105', quarterly_share_conflicts=[])


class QuarterlyShareConflictTests(unittest.TestCase):
    def test_explicit_policy_keeps_unknown_shares_and_all_original_evidence(self):
        rows = [statement(), statement(total_share=1100), statement(total_share=None)]
        original = copy.deepcopy(rows)
        expected_warning = None
        for permutation in itertools.permutations(rows):
            with self.assertRaisesRegex(AppError, 'total_share'):
                statements(permutation, '20261105')
            warnings = []
            result = statements(permutation, '20261105', quarterly_share_conflicts=warnings)
            self.assertIsNone(result['20260930']['total_share'])
            self.assertEqual(result['20260930']['total_assets'], 10000)
            self.assertEqual(len(warnings), 1)
            self.assertEqual({k: warnings[0][k] for k in ('api', 'period', 'ann_date', 'field', 'values')},
                             {'api': 'balancesheet', 'period': '20260930', 'ann_date': '20261030',
                              'field': 'total_share', 'values': [1000, 1100]})
            self.assertCountEqual(warnings[0]['originals'], original)
            if expected_warning is None:
                expected_warning = warnings
            self.assertEqual(fingerprint(warnings), fingerprint(expected_warning))
        self.assertEqual(rows, original)

    def test_annual_shares_and_every_other_field_still_block(self):
        for end_date in ('20251231', '20241231'):
            with self.assertRaisesRegex(AppError, 'total_share'):
                statements([statement(end_date=end_date), statement(end_date=end_date, total_share=1100)],
                           '20261105', quarterly_share_conflicts=[])
        for field in ('total_assets', 'total_liab', 'money_cap', 'st_borr', 'non_cur_liab_due_1y', 'lt_borr'):
            with self.subTest(field=field), self.assertRaisesRegex(AppError, field):
                statements([statement(**{field: 100}), statement(**{field: 101})],
                           '20261105', quarterly_share_conflicts=[])

    def test_future_conflict_does_not_warn_and_known_revision_removes_current_warning(self):
        original = statement()
        future = statement(ann_date='20261110', total_share=1100)
        warnings = []
        self.assertEqual(statements([original, future], '20261105', quarterly_share_conflicts=warnings),
                         {'20260930': original})
        self.assertEqual(warnings, [])
        rows = [original, statement(total_share=1200), future]
        for permutation in itertools.permutations(rows):
            warnings = []
            self.assertEqual(statements(permutation, '20261110', quarterly_share_conflicts=warnings),
                             {'20260930': future})
            self.assertEqual(warnings, [])

    def test_summary_and_assessment_preserve_results_with_audited_unused_conflict(self):
        original_row, dossier = fixture()
        baseline = assess(original_row, dossier, load_rules())
        dossier['data']['balancesheet'].append({**dossier['data']['balancesheet'][-1], 'total_share': 1100})
        untouched = copy.deepcopy(dossier)
        row = summarize_again(dossier, original_row)
        self.assertTrue(row['gate'])
        self.assertIsNone(row['balance']['total_share'])
        self.assertEqual(row['share_count'], original_row['share_count'])
        self.assertEqual(len(row['data_warnings']), 1)
        self.assertNotEqual(row['evidence_hash'], original_row['evidence_hash'])
        assessment = assess(row, dossier, load_rules())
        self.assertEqual({k: v for k, v in assessment.items() if k != 'evidence_hash'},
                         {k: v for k, v in baseline.items() if k != 'evidence_hash'})
        self.assertEqual(dossier, untouched)
        dossier['data']['balancesheet'].reverse()
        reordered = summarize_again(dossier, original_row)
        self.assertEqual(row['evidence_hash'], reordered['evidence_hash'])
        self.assertEqual(row['data_warnings'], reordered['data_warnings'])
        review = {'approved': True, 'symbol': row['symbol'], 'status': '可进一步评估',
                  'reviewed_on': row['as_of'], 'evidence_hash': original_row['evidence_hash']}
        with self.assertRaisesRegex(AppError, '财报/分红/股数已变化'):
            evaluate_review(row, review, load_config())

    def test_changed_conflict_changes_hash_but_future_duplicate_does_not(self):
        original_row, dossier = fixture()
        conflict = {**dossier['data']['balancesheet'][-1], 'total_share': 1100}
        dossier['data']['balancesheet'].append(conflict)
        first = summarize_again(dossier, original_row)
        dossier['data']['balancesheet'].append({**conflict, 'ann_date': '20261110', 'total_share': 1200})
        self.assertEqual(summarize_again(dossier, original_row)['evidence_hash'], first['evidence_hash'])
        conflict['total_share'] = 1150
        changed = summarize_again(dossier, original_row)
        self.assertNotEqual(changed['evidence_hash'], first['evidence_hash'])
        self.assertIsNone(changed['balance']['total_share'])

    def test_required_balance_conflicts_block_summary_and_assessment(self):
        for period, field in (('20251231', 'total_share'), ('20260930', 'total_assets'),
                              ('20260930', 'st_borr')):
            original_row, dossier = fixture()
            source = next(r for r in dossier['data']['balancesheet'] if r['end_date'] == period)
            dossier['data']['balancesheet'].append({**source, field: source[field] + 1})
            row = summarize_again(dossier, original_row)
            with self.subTest(period=period, field=field):
                self.assertFalse(row['gate'])
                self.assertTrue(any(field in issue for issue in row['issues']))
                self.assertEqual(assess(row, dossier, load_rules())['state'], '待数据核实')
                # Even a stale pre-conflict summary cannot bypass the assessment's revalidation.
                self.assertEqual(assess(original_row, dossier, load_rules())['state'], '待数据核实')

    def test_missing_annual_shares_are_never_replaced_by_zero_or_quarterly_shares(self):
        original_row, dossier = fixture()
        annual = next(r for r in dossier['data']['balancesheet'] if r['end_date'] == '20251231')
        annual['total_share'] = None
        dossier['data']['balancesheet'].append({**dossier['data']['balancesheet'][-1], 'total_share': 1100})
        row = summarize_again(dossier, original_row)
        self.assertIsNone(row['balance']['total_share'])
        self.assertEqual(assess(row, dossier, load_rules())['state'], '待数据核实')


if __name__ == '__main__':
    unittest.main()
