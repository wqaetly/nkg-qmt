import copy
import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from qmt.backtest import suspension_values
from qmt.config import AppError, load_config
from qmt.ledger import Ledger
from qmt.research import DISCLOSURES, full_day_suspension


class SuspensionSourceTests(unittest.TestCase):
    def setUp(self):
        self.item=copy.deepcopy(DISCLOSURES['full_day_suspensions'][0])
        self.registry=patch.dict(DISCLOSURES,{'full_day_suspensions':[self.item]})
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def test_exact_dated_primary_proof_never_infers_other_dates_or_symbols(self):
        before=copy.deepcopy(self.item)
        proof=full_day_suspension('600566.SH','20250718')
        self.assertEqual(proof,{key:self.item[key] for key in
            ('symbol','day','full_day','source','source_sha256','pages','available_on')})
        self.assertEqual(proof['source'],'https://static.cninfo.com.cn/finalpage/2025-07-18/1224202264.PDF')
        self.assertIsNone(full_day_suspension('600566.SH','20250717'))
        self.assertIsNone(full_day_suspension('600566.SH','20250721'))
        self.assertIsNone(full_day_suspension('600567.SH','20250718'))
        self.assertEqual(self.item,before)

    def test_later_resumption_announcement_cannot_supply_earlier_proof(self):
        self.item['available_on']='20250721'
        self.assertIsNone(full_day_suspension('600566.SH','20250718'))
        self.assertIsNone(full_day_suspension('600566.SH','20250721'))

    def test_duplicate_or_non_full_day_registration_is_not_accepted(self):
        DISCLOSURES['full_day_suspensions'].append(copy.deepcopy(self.item))
        with self.assertRaisesRegex(AppError,'重复或冲突'):
            full_day_suspension('600566.SH','20250718')
        DISCLOSURES['full_day_suspensions'].pop()
        for value in (False,1,'true',None):
            self.item['full_day']=value
            with self.subTest(value=value),self.assertRaisesRegex(AppError,'全天停牌'):
                full_day_suspension('600566.SH','20250718')

    def test_source_and_identity_validation_fail_closed(self):
        original=copy.deepcopy(self.item)
        for change in ({'source':'http://example.org/file.pdf'},
                       {'source':'https://user:password@example.org/file.pdf'},
                       {'source_sha256':'f'*63},{'source_sha256':'g'*64},{'pages':''},
                       {'available_on':'20250230'}):
            self.item.clear();self.item.update({**original,**change})
            with self.subTest(change=change),self.assertRaises(AppError):
                full_day_suspension('600566.SH','20250718')
        for records in (None,{},['not a proof']):
            with self.subTest(records=records),patch.dict(DISCLOSURES,{'full_day_suspensions':records}),self.assertRaises(AppError):
                full_day_suspension('600566.SH','20250718')
        for code,day in (('bad','20250718'),('600566.SH','20250230')):
            with self.subTest(code=code,day=day),self.assertRaises(AppError):
                full_day_suspension(code,day)


class ChineseMediaSuspensionTests(unittest.TestCase):
    code = '600373.SH'
    halted = ['20231218', '20231219', '20231220']
    sessions = ['20231215', *halted, '20231221']

    def ledger(self):
        ledger = Ledger(':memory:', load_config())
        self.addCleanup(ledger.close)
        ledger.initialize('paper', '100000', '5000', '20000', '20231215')
        ledger.opening('paper', self.code, 200, '20231215')
        ledger.db.execute("INSERT INTO metadata VALUES('automatic-rules','historical-replay')")
        ledger.db.execute("INSERT INTO metadata VALUES('historical-sessions',?)", (json.dumps(self.sessions),))
        ledger.db.commit()
        return ledger

    def test_three_exact_days_use_the_prior_published_suspension_announcement(self):
        rows = [r for r in DISCLOSURES['full_day_suspensions'] if r['symbol'] == self.code]
        self.assertEqual([r['day'] for r in rows], self.halted)
        for day in self.halted:
            proof = full_day_suspension(self.code, day)
            self.assertEqual(proof, {'symbol': self.code, 'day': day, 'full_day': True,
                'available_on': '20231216', 'pages': '1,2',
                'source': 'https://static.cninfo.com.cn/finalpage/2023-12-16/1218635071.PDF',
                'source_sha256': 'ee2a6da41b2e7d04de6282bba19b0299f0bd1d29785bfafbb16f3a928d283be1'})
        for day in ('20231215', '20231216', '20231217', '20231221', '20231222'):
            self.assertIsNone(full_day_suspension(self.code, day))

    def test_later_resumption_proof_cannot_value_any_prior_missing_day(self):
        rows = [{**r, 'available_on': '20231221',
                 'source': 'https://static.cninfo.com.cn/finalpage/2023-12-21/1218673974.PDF',
                 'source_sha256': '56a90084aa36e0d15dacd9a7139c3411ab747c07ed5264f49775abd73e9760a9'}
                for r in DISCLOSURES['full_day_suspensions'] if r['symbol'] == self.code]
        ledger = self.ledger()
        with patch.dict(DISCLOSURES, {'full_day_suspensions': rows}):
            for day in self.halted:
                self.assertIsNone(full_day_suspension(self.code, day))
                with self.assertRaisesRegex(AppError, self.code + ' 在 ' + day + ' 缺少当日估值'):
                    ledger.mark('paper', day, {}, valuation_only={})
        self.assertEqual(ledger.summary('paper')['marks'], [])

    def test_prior_close_is_valuation_only_and_pending_orders_do_not_fill_or_chase(self):
        # Raw closes bracketing the announced halt; deliberately no bars on the three halted days.
        bars = {self.code: {'20231215': {'close': Decimal('14.66')},
                           '20231221': {'close': Decimal('13.19')}}}
        original = copy.deepcopy(bars)
        row = {'symbol': self.code, 'industry': '传媒', 'automatic_signal': True}
        industries = {self.code: '传媒'}
        for side in ('buy', 'sell'):
            with self.subTest(side=side):
                ledger = self.ledger()
                ledger.create_plan('paper', row, '20231215', '20231218', 100, '14.66', side,
                    '停牌执行边界', {self.code: {'trade_date': '20231215', 'close': '14.66'}}, industries)
                for index, day in enumerate(self.halted, 1):
                    ledger.settle_paper(day, bars, suspensions={self.code: full_day_suspension(self.code, day)})
                    values = suspension_values({self.code: 200}, day, self.sessions, bars, {},
                                               {self.code: '20231215'})
                    value = values[self.code]
                    self.assertEqual((value['price_date'], value['stale_sessions']), ('20231215', index))
                    self.assertEqual(Decimal(value['market_value']), Decimal('2932'))
                    self.assertFalse(value['tradable'])
                    self.assertEqual([p['day'] for p in value['proofs']], self.halted[:index])
                    ledger.mark('paper', day, {}, valuation_only=values)
                    next_day = self.sessions[self.sessions.index(day) + 1]
                    with self.assertRaisesRegex(AppError, '不可用于'):
                        ledger.create_plan('paper', row, day, next_day, 100, '14.66', side,
                            '旧价不能交易', {}, industries, valuation_only=values)
                ledger.settle_paper('20231221', bars)
                ledger.mark('paper', '20231221', {self.code: {'trade_date': '20231221', 'close': '13.19'}})
                result = ledger.summary('paper')
                self.assertEqual(result['fills'], [])
                self.assertEqual(result['cash_events'], [])
                self.assertEqual(result['stock_events'], [])
                self.assertEqual(result['positions'], {self.code: 200})
                self.assertEqual(ledger.cash('paper'), Decimal('100000'))
                self.assertEqual(len(result['plans']), 1)
                self.assertEqual(result['plans'][0]['status'], 'expired')
                self.assertIn('不延至复牌', result['plans'][0]['note'])
                self.assertEqual([Decimal(m['equity']) - Decimal(m['cash']) for m in result['marks']],
                                 [Decimal('2932')] * 3 + [Decimal('2638')])
        self.assertEqual(bars, original)


if __name__=='__main__':
    unittest.main()
