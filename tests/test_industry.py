import unittest
from qmt.config import AppError, load_config
from qmt.industry import industry_at, load_industry_history
from qmt.ledger import Ledger
from qmt.backtest import simulate
from test_backtest import inputs, dividend, CODE


class IndustryTests(unittest.TestCase):
    def test_date_bounded_membership_and_ambiguous_intervals(self):
        old={'l1_code':'A','l1_name':'旧行业','in_date':'20200101','out_date':'20240101'}
        new={'l1_code':'B','l1_name':'新行业','in_date':'20240101','out_date':None}
        self.assertEqual(industry_at([old,new],'20221230')['code'],'A')
        self.assertEqual(industry_at([old,new],'20240101')['code'],'B')
        self.assertIsNone(industry_at([old,new],'20191231'))
        self.assertIsNone(industry_at([{**old,'out_date':None},new],'20240101'))
        self.assertIsNone(industry_at([{**old,'in_date':None}],'20221230'))
        self.assertEqual(industry_at([old,dict(old)],'20221230')['code'],'A')

    def test_potential_truncation_is_not_a_valid_peer_universe(self):
        class Client:
            def call(self,api,params,fields):return [{'ts_code':CODE}]*2000
        with self.assertRaises(AppError):load_industry_history(Client(),[CODE])

    def test_invalid_dates_stop_industry_mapping(self):
        with self.assertRaises(AppError):industry_at([{'in_date':'20230102','out_date':'20230101'}],'20230103')


class StockOnlyTests(unittest.TestCase):
    def event(self):
        return {**dividend(),'event_kind':'stock_only','stk_div':'.3','cash_div_tax':'0',
                'pay_date':None,'stock_list_date':'20200204'}

    def test_no_cash_date_is_not_fabricated_and_held_split_still_halts(self):
        data,signals,cfg=inputs();data['dividends']={CODE:[self.event()]}
        empty=simulate(data,{},cfg)
        self.assertEqual(empty['cash_events'],[])
        self.assertEqual(empty['metrics']['total_return'],0)
        with self.assertRaisesRegex(AppError,'送转'):simulate(data,signals,cfg)

    def test_pre_account_stock_only_event_ignored_without_fake_income(self):
        ledger=Ledger(':memory:',load_config())
        try:
            ledger.initialize('paper','10000','2000','4000','20210101')
            ledger.dividends({CODE:[self.event()]},'20210104')
            self.assertEqual(str(ledger.cash('paper')),'10000.00')
            self.assertEqual(ledger.summary('paper')['cash_events'],[])
        finally:ledger.close()

    def test_stock_only_cash_or_listing_contradiction_rejected(self):
        for fields in ({'cash_div_tax':'.2'},{'pay_date':'20200204'},{'stock_list_date':'20200131'}):
            data,signals,cfg=inputs();data['dividends']={CODE:[{**self.event(),**fields}]}
            with self.assertRaises(AppError):simulate(data,signals,cfg)


if __name__=='__main__':unittest.main()
