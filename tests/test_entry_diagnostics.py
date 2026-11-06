import copy
import unittest

from qmt.experiment import entry_diagnostics


STAGES = ['当期候选', '财务通过', '估值通过', '流动性通过', '基础候选通过', '全部买入条件通过']


def assessment(**changes):
    row = {'active': True, 'state': '满足价格条件', 'coarse_pass': True,
           'dimensions': {'dividend': {'state': '满足价格条件'},
                          'valuation': {'state': '通过'}, 'liquidity': {'state': '通过'}}}
    row.update(changes)
    return row


class EntryDiagnosticsTests(unittest.TestCase):
    def test_active_intersection_excludes_retired_and_keeps_empty_months(self):
        legacy = assessment()
        del legacy['active']
        waiting = assessment(state='等待价格')
        waiting['dimensions']['dividend']['state'] = '等待价格'
        liquidity_failed = assessment(state='等待筛选条件')
        liquidity_failed['dimensions']['liquidity']['state'] = '未通过'
        valuation_failed = assessment(state='等待筛选条件')
        valuation_failed['dimensions']['valuation']['state'] = '未通过'
        valuation_failed['dimensions']['liquidity']['state'] = '未通过'
        financial_failed = assessment(state='待数据核实')
        financial_failed['dimensions']['dividend']['state'] = '待数据核实'
        active = [assessment(), legacy, waiting, assessment(coarse_pass=False),
                  liquidity_failed, valuation_failed, financial_failed, assessment(dimensions={})]
        audits = [{'day': '20230131', 'assessments': active[:4]},
                  {'day': '20230228', 'assessments': active[4:] +
                   [assessment(active=False, state='退出研究池') for _ in range(780)]}]
        signals = {'20230131': {'candidates': [{'symbol': 'A'}, {'symbol': 'B'}]},
                   '20230228': {'candidates': []}, '20230331': {'candidates': []}}
        original = copy.deepcopy((audits, signals))

        result = entry_diagnostics(audits, signals)

        self.assertEqual(result['entry_funnel'],
                         [dict(stage=stage, count=count) for stage, count in zip(STAGES, [8, 6, 5, 4, 3, 2])])
        self.assertEqual(result['candidate_months'], {'months': 3, 'empty': 2, 'total_candidates': 2})
        self.assertEqual((audits, signals), original)

    def test_missing_or_unknown_dimensions_never_pass(self):
        cases = []
        missing = assessment()
        del missing['dimensions']
        cases.append(('missing dimensions', missing, [1, 0, 0, 0, 0, 0]))
        cases.append(('empty dimensions', assessment(dimensions={}), [1, 0, 0, 0, 0, 0]))
        for dimension, expected in (
                ('dividend', [1, 0, 0, 0, 0, 0]),
                ('valuation', [1, 1, 0, 0, 0, 0]),
                ('liquidity', [1, 1, 1, 0, 0, 0])):
            missing = assessment()
            del missing['dimensions'][dimension]
            unknown = assessment()
            unknown['dimensions'][dimension]['state'] = '未知'
            cases.extend([(f'missing {dimension}', missing, expected),
                          (f'unknown {dimension}', unknown, expected)])
        no_coarse = assessment()
        del no_coarse['coarse_pass']
        cases.append(('missing coarse gate', no_coarse, [1, 1, 1, 1, 0, 0]))
        cases.append(('unknown final state', assessment(state='未知'), [1, 1, 1, 1, 1, 0]))
        for label, row, expected in cases:
            with self.subTest(label=label):
                result = entry_diagnostics([{'assessments': [row]}], {'20230131': {'candidates': []}})
                self.assertEqual([stage['count'] for stage in result['entry_funnel']], expected)

    def test_empty_inputs_have_zero_funnel_and_no_months(self):
        self.assertEqual(entry_diagnostics([], {}), {
            'entry_funnel': [{'stage': stage, 'count': 0} for stage in STAGES],
            'candidate_months': {'months': 0, 'empty': 0, 'total_candidates': 0},
        })


if __name__ == '__main__':
    unittest.main()
