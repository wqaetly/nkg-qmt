import copy
import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from qmt.backtest import _inputs, suspension_values
from qmt.config import AppError, load_config
from qmt.ledger import Ledger
from qmt.research import DISCLOSURES, full_day_suspension


# Sessions and raw closes copied from the sealed v33 long market, not generated weekdays.
CASES = [
    {'code': '000726.SZ', 'anchor': '20150709', 'resume': '20150714',
     'close': '13.09', 'resume_close': '14.4', 'resume_public': '20150714',
     'days': ['20150710', '20150713'],
     'sources': [('20150710', '1201268099', 'f203c0eec018ceaab976cd44653dc9ca0b38c0d8ca3e45453d693cee049351f8')]},
    {'code': '000858.SZ', 'anchor': '20150729', 'resume': '20151102',
     'close': '25.73', 'resume_close': '24.2', 'resume_public': '20151031',
     'days': '''20150730 20150731 20150803 20150804 20150805 20150806 20150807
         20150810 20150811 20150812 20150813 20150814 20150817 20150818 20150819
         20150820 20150821 20150824 20150825 20150826 20150827 20150828 20150831
         20150901 20150902 20150907 20150908 20150909 20150910 20150911 20150914
         20150915 20150916 20150917 20150918 20150921 20150922 20150923 20150924
         20150925 20150928 20150929 20150930 20151008 20151009 20151012 20151013
         20151014 20151015 20151016 20151019 20151020 20151021 20151022 20151023
         20151026 20151027 20151028 20151029 20151030'''.split(),
     'sources': [
         ('20150730', '1201364487', '8e03640e12a1a70ecaa7ff846ffdcf7cbe927f1c8fe96c4f5d3112af05ca1b93'),
         ('20150806', '1201397615', 'fba3d64e0691836e6ffc6fc9eef49092056eaf4d78fb6df0ad3341593361a6f2'),
         ('20150813', '1201432957', '6435cf3542c008dc7a773698b4383f692f817222f9a6bb80cef3325baa834b37'),
         ('20150821', '1201467048', '5e61894b0e57a39dd9816a7829fc62231d6af99a5904bde92a6b862d979f06a4'),
         ('20150828', '1201508435', 'c09473c59f5d87e98e23c75f338e8cb2bdd94bcf390b2df37e41860cb3bdd133'),
         ('20150909', '1201567270', '8368b73bdbc1cc9a90a3ba1575ac5bbf3e5f93e1f8ec9541142b5106115daf14'),
         ('20150917', '1201603160', '6f04b569e068507a26da8c1a9e3336bd42ce55cbde53790082512cebee5ef490'),
         ('20150925', '1201635361', '8f2828e49aa8df60248edb1ecd04fa3a748146d386c01ce1282b85645859bca0'),
         ('20151012', '1201673995', '4cc5d8af1cc0281de7468c4d8477b51d54f29230d52b44f63c5a3540af0de473'),
         ('20151020', '1201708894', '289671d8f3174484cab18dcf651dc7cc2150de4462f0995d528d8f92606d0dac'),
         ('20151028', '1201732204', '04ca5ac6130d9be861d1b538ad5e51ec1446a18ff514e89cc400b7896250e94e')]}]


def context(case):
    return ([case['anchor'], *case['days'], case['resume']],
            {case['code']: {case['anchor']: {'close': Decimal(case['close'])},
                            case['resume']: {'close': Decimal(case['resume_close'])}}})


class Historical2015SuspensionTests(unittest.TestCase):
    def test_exact_62_sessions_use_latest_already_public_source(self):
        self.assertEqual([len(c['days']) for c in CASES], [2, 60])
        for case in CASES:
            rows = [r for r in DISCLOSURES['full_day_suspensions']
                    if r['symbol'] == case['code'] and case['days'][0] <= r['day'] <= case['days'][-1]]
            self.assertEqual([r['day'] for r in rows], case['days'])
            for day in case['days']:
                available, ident, digest = [s for s in case['sources'] if s[0] <= day][-1]
                source_date = f'{available[:4]}-{available[4:6]}-{available[6:]}'
                with self.subTest(code=case['code'], day=day):
                    self.assertEqual(full_day_suspension(case['code'], day),
                        {'symbol': case['code'], 'day': day, 'full_day': True,
                         'available_on': available, 'pages': '1',
                         'source': f'https://static.cninfo.com.cn/finalpage/{source_date}/{ident}.PDF',
                         'source_sha256': digest})
            for day in (case['anchor'], case['resume']):
                self.assertIsNone(full_day_suspension(case['code'], day))
        for code, day in [('000726.SZ', '20150711'), ('000726.SZ', '20150712'),
                          ('000858.SZ', '20150903'), ('000858.SZ', '20151007')]:
            self.assertIsNone(full_day_suspension(code, day))

    def test_later_resumption_date_and_missing_middle_proof_cannot_supply_chain(self):
        for case in CASES:
            rows = [{**r, 'available_on': case['resume_public']}
                    for r in DISCLOSURES['full_day_suspensions'] if r['symbol'] == case['code']]
            with patch.dict(DISCLOSURES, {'full_day_suspensions': rows}):
                for day in case['days']:
                    self.assertIsNone(full_day_suspension(case['code'], day))
            sessions, bars = context(case)
            missing = case['days'][len(case['days']) // 2 - 1]
            rows = [r for r in DISCLOSURES['full_day_suspensions']
                    if (r['symbol'], r['day']) != (case['code'], missing)]
            with patch.dict(DISCLOSURES, {'full_day_suspensions': rows}), self.assertRaisesRegex(AppError, '未经核实'):
                suspension_values({case['code']: 100}, case['days'][-1], sessions, bars, {},
                                  {case['code']: case['anchor']})

    def test_real_anchor_bar_rejects_a_conflicting_suspension_registration(self):
        case = CASES[1]
        actual = {'open': '25.53', 'high': '26.17', 'low': '25.33', 'close': '25.73',
                  'vol': '208285.26', 'up': '28.16', 'down': '23.04'}
        data = {'sessions': [case['anchor'], case['days'][0]],
                'bars': {case['code']: {case['anchor']: actual}}, 'industries': {case['code']: '食品饮料'}}
        # Deliberately corrupt a proof onto the real, partially traded anchor session.
        conflict = {**full_day_suspension(case['code'], case['days'][0]),
                    'day': case['anchor'], 'available_on': case['anchor']}
        with patch.dict(DISCLOSURES, {'full_day_suspensions': [conflict]}), self.assertRaisesRegex(AppError, '日线冲突'):
            _inputs(data, {}, 20)

    def test_cash_or_stock_ex_date_anywhere_in_chain_still_blocks_carry(self):
        for case in CASES:
            sessions, bars = context(case)
            for ratio in ('0', '.3'):
                for ex_date in (case['days'][0], case['days'][len(case['days']) // 2], case['days'][-1]):
                    event = {'ex_date': ex_date, 'cash_div_tax': '.6' if ratio == '0' else '0', 'stk_div': ratio}
                    with self.subTest(code=case['code'], ex_date=ex_date, stock=ratio), self.assertRaisesRegex(AppError, '跨越除权除息'):
                        suspension_values({case['code']: 100}, case['days'][-1], sessions, bars,
                                          {case['code']: [event]}, {case['code']: case['anchor']})

    def test_real_missing_bar_context_accumulates_staleness_without_fills(self):
        for case in CASES:
            with self.subTest(code=case['code']):
                sessions, bars = context(case)
                original = copy.deepcopy(bars)
                ledger = Ledger(':memory:', load_config())
                self.addCleanup(ledger.close)
                ledger.initialize('paper', '100000', '5000', '20000', case['anchor'])
                # Seeded test shares exercise missing-price handling, not an actual strategy holding.
                ledger.opening('paper', case['code'], 100, case['anchor'])
                ledger.db.execute("INSERT INTO metadata VALUES('automatic-rules','historical-replay')")
                ledger.db.execute("INSERT INTO metadata VALUES('historical-sessions',?)", (json.dumps(sessions),))
                ledger.db.commit()
                row = {'symbol': case['code'], 'industry': '测试行业', 'automatic_signal': True}
                industries = {case['code']: '测试行业'}
                ledger.create_plan('paper', row, case['anchor'], case['days'][0], 100, case['close'], 'sell',
                    '停牌执行边界', {case['code']: {'trade_date': case['anchor'], 'close': case['close']}}, industries)
                for stale, day in enumerate(case['days'], 1):
                    ledger.settle_paper(day, bars, suspensions={case['code']: full_day_suspension(case['code'], day)})
                    values = suspension_values({case['code']: 100}, day, sessions, bars, {},
                                               {case['code']: case['anchor']})
                    value = values[case['code']]
                    self.assertEqual((value['price_date'], value['stale_sessions']), (case['anchor'], stale))
                    self.assertEqual(Decimal(value['market_value']), Decimal(case['close']) * 100)
                    self.assertFalse(value['tradable'])
                    self.assertEqual([p['day'] for p in value['proofs']], case['days'][:stale])
                    ledger.mark('paper', day, {}, valuation_only=values)
                    with self.assertRaisesRegex(AppError, '不可用于'):
                        ledger.create_plan('paper', row, day, sessions[sessions.index(day) + 1], 100,
                            case['close'], 'sell', '旧价不能交易', {}, industries, valuation_only=values)
                ledger.settle_paper(case['resume'], bars)
                ledger.mark('paper', case['resume'],
                    {case['code']: {'trade_date': case['resume'], 'close': case['resume_close']}})
                result = ledger.summary('paper')
                self.assertEqual(result['fills'], [])
                self.assertEqual(result['cash_events'], [])
                self.assertEqual(result['stock_events'], [])
                self.assertEqual(result['positions'], {case['code']: 100})
                self.assertEqual(ledger.cash('paper'), Decimal('100000'))
                self.assertEqual(len(result['plans']), 1)
                self.assertEqual(result['plans'][0]['status'], 'expired')
                self.assertIn('不延至复牌', result['plans'][0]['note'])
                self.assertEqual(len(result['marks']), len(case['days']) + 1)
                self.assertEqual(bars, original)


if __name__ == '__main__':
    unittest.main()
