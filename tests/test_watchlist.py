import copy
import importlib.util
import json
import unittest
from pathlib import Path

script = Path(__file__).resolve().parents[1] / 'skills/watchlist-review/scripts/prepare_watchlist.py'
spec = importlib.util.spec_from_file_location('prepare_watchlist', script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def snapshot():
    return {
        'schema_version': 1, 'captured_at': '2026-01-05T15:20:00+08:00',
        'accounts': [{'id': 'private-account', 'summaries': [{'currency': 'CNY', 'market_value': '1000', 'cash_available': '200'}]}],
        'entries': [{'kind': 'holding', 'account_id': 'private-account', 'symbol': '000001.SZ',
                     'name': '示例证券', 'market': 'CN', 'currency': 'CNY', 'asset_type': 'stock',
                     'identity_status': 'verified', 'position': {'quantity': '100', 'snapshot_price': '10',
                     'market_value': '1000', 'cost_price': '11', 'reported_pnl': '-100', 'quote_at': None}}]
    }


class WatchlistTests(unittest.TestCase):
    def test_public_packet_whitelists_only_security_identity(self):
        data = snapshot()
        data['notes'] = 'secret-root'
        data['accounts'][0]['login'] = 'secret-account'
        data['entries'][0]['thesis'] = 'secret-note'
        data['entries'][0]['position']['api_key'] = 'secret-key'
        public, checks = module.prepare(data)
        self.assertEqual(set(public), {'securities'})
        self.assertEqual(set(public['securities'][0]), {'symbol', 'name', 'market', 'currency', 'asset_type'})
        self.assertNotIn('secret', json.dumps(public))
        self.assertNotIn('private-account', json.dumps(public))
        self.assertEqual(checks['status'], 'prepared_only')

    def test_currency_groups_are_not_added_together(self):
        data = snapshot()
        second = copy.deepcopy(data['entries'][0])
        second.update(symbol='00001.HK', market='HK', currency='HKD')
        data['entries'].append(second)
        _, checks = module.prepare(data)
        self.assertEqual({g['currency'] for g in checks['groups']}, {'CNY', 'HKD'})
        self.assertEqual([g['visible_market_value'] for g in checks['groups']], ['1000', '1000'])
        self.assertTrue(any('无已确认可用现金' in w for w in checks['warnings']))

    def test_buying_power_is_not_cash_and_differences_are_preserved(self):
        data = snapshot()
        data['accounts'][0]['summaries'][0] = {'currency': 'CNY', 'market_value': '1001', 'max_buying_power': '99999'}
        _, checks = module.prepare(data)
        self.assertEqual(checks['groups'][0]['difference_from_reported_market_value'], '-1')
        self.assertTrue(any('无已确认可用现金' in w for w in checks['warnings']))
        self.assertEqual(data['entries'][0]['position']['reported_pnl'], '-100')

    def test_same_security_across_accounts_researched_once(self):
        data = snapshot()
        data['accounts'].append({'id': 'second'})
        second = copy.deepcopy(data['entries'][0])
        second['account_id'] = 'second'
        data['entries'].append(second)
        public, checks = module.prepare(data)
        self.assertEqual(len(public['securities']), 1)
        self.assertEqual(checks['holding_count'], 2)
        self.assertEqual(len(checks['groups']), 2)

    def test_duplicates_and_identity_conflicts_fail(self):
        data = snapshot()
        data['entries'].append(copy.deepcopy(data['entries'][0]))
        with self.assertRaises(ValueError):
            module.prepare(data)
        data['entries'][1].update(kind='watch', name='不同证券')
        data['entries'][1].pop('position')
        data['entries'][1].pop('account_id')
        with self.assertRaises(ValueError):
            module.prepare(data)

    def test_unverified_identity_stays_private_and_visible_as_gap(self):
        data = snapshot()
        data['entries'][0].update(identity_status='unverified', symbol=None)
        public, checks = module.prepare(data)
        self.assertEqual(public['securities'], [])
        self.assertEqual(checks['holding_count'], 1)
        self.assertTrue(any('身份待确认' in w for w in checks['warnings']))

    def test_watch_without_position_and_missing_market_value(self):
        data = snapshot()
        watch = copy.deepcopy(data['entries'][0])
        watch.update(kind='watch', symbol='000002.SZ')
        watch.pop('account_id')
        watch.pop('position')
        data['entries'].append(watch)
        data['entries'][0]['position']['market_value'] = None
        _, checks = module.prepare(data)
        self.assertEqual(checks['watch_count'], 1)
        self.assertIsNone(checks['groups'][0]['visible_market_value'])
        self.assertEqual(checks['groups'][0]['weights'], [])

    def test_invalid_numbers_accounts_and_time_fail(self):
        for bad in ('NaN', 'Infinity', '-1', '0', True):
            data = snapshot()
            data['entries'][0]['position']['quantity'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                module.prepare(data)
        data = snapshot()
        data['captured_at'] = '2026-01-05T15:20:00'
        with self.assertRaises(ValueError):
            module.prepare(data)
        data = snapshot()
        data['entries'][0]['account_id'] = 'missing'
        with self.assertRaises(ValueError):
            module.prepare(data)

    def test_private_cost_may_be_negative_but_available_cannot_exceed_quantity(self):
        data = snapshot()
        data['entries'][0]['position']['cost_price'] = '-1'
        module.prepare(data)
        data['entries'][0]['position']['available_quantity'] = '101'
        with self.assertRaises(ValueError):
            module.prepare(data)


if __name__ == '__main__':
    unittest.main()
