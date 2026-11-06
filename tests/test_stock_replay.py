import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from qmt.automatic import assess, load_rules, plan_day, save_meta
from qmt.backtest import simulate
from qmt.config import AppError, load_config
from qmt.ledger import Ledger
from qmt.research import DISCLOSURES, stock_distribution_terms
from qmt.service import update_ledger
from test_automatic import fixture, report
from test_backtest import CODE, DAYS, bar, dividend, inputs


def registered(event):
    return {'expected':event,'available_on':event['imp_ann_date'],'stock_list_date':event['ex_date'],
            'source':'https://example.com/fictional-stock-distribution','source_sha256':'a'*64}


class StockReplayTests(unittest.TestCase):
    def test_real_terms_require_exact_rows_and_publication_date(self):
        for item in DISCLOSURES['stock_distributions']:
            event=copy.deepcopy(item['expected'])
            before=(datetime.strptime(item['available_on'],'%Y%m%d')-timedelta(days=1)).strftime('%Y%m%d')
            self.assertIsNone(stock_distribution_terms(event,before))
            self.assertEqual(stock_distribution_terms(event,item['available_on']),
                {key:item[key] for key in ('stock_list_date','source','available_on')})
            for changed in ({**event,'stk_div':'.9'},{**event,'stock_list_date':item['stock_list_date']}):
                self.assertIsNone(stock_distribution_terms(changed,'20261106'))

    def test_invalid_registered_listing_or_source_is_rejected(self):
        event={**dividend(),'stk_div':'.3'}
        for changes in ({'stock_list_date':'20200131'},{'source':'javascript:alert(1)'},
                        {'source_sha256':'unknown'},{'available_on':'20200204'}):
            with self.subTest(changes=changes),patch.dict(DISCLOSURES,{'stock_distributions':[{**registered(event),**changes}]}):
                with self.assertRaises(AppError):stock_distribution_terms(event,'20200205')

    def test_raw_price_cash_and_integer_share_credit_preserve_equity(self):
        data,signals,cfg=inputs()
        event={**dividend(),'cash_div_tax':'1.5','stk_div':'.3'}
        data['dividends']={CODE:[event]}
        for day in DAYS[2:]:data['bars'][CODE][day]=bar('5')  # (8 - 1.5) / 1.3, no return.
        with patch.dict(DISCLOSURES,{'stock_distributions':[registered(event)]}):
            result=simulate(data,signals,cfg)
        self.assertEqual(result['equity_curve'][1]['positions'],{CODE:200})
        self.assertEqual(result['equity_curve'][2]['positions'],{CODE:260})
        self.assertTrue(all(Decimal(p['equity'])==Decimal('9995') for p in result['equity_curve'][1:]))
        self.assertEqual(Decimal(result['equity_curve'][2]['receivable']),Decimal(300))
        self.assertEqual(Decimal(result['cash_events'][0]['amount']),Decimal(300))
        self.assertEqual(result['stock_events'][0]['record_quantity'],200)
        self.assertEqual(result['stock_events'][0]['quantity'],60)
        self.assertEqual(len(result['trades']),1)

    def test_replay_can_sell_the_whole_odd_remainder_then_exit(self):
        data,signals,cfg=inputs()
        event={**dividend(),'stk_div':'.3'}
        data['dividends']={CODE:[event]}
        signals[DAYS[2]]=[{'symbol':CODE,'reference_price':'9','known_on':DAYS[2]}]
        signals[DAYS[3]]=[]
        with patch.dict(DISCLOSURES,{'stock_distributions':[registered(event)]}):
            result=simulate(data,signals,cfg)
        self.assertEqual([(t['side'],t['quantity']) for t in result['trades']],
                         [('buy',200),('sell',60),('sell',200)])
        self.assertEqual(result['equity_curve'][-1]['positions'],{})

    def test_daily_planner_sells_the_odd_remainder_without_buying_fractional_lots(self):
        cfg=load_config();rules={**load_rules(),'max_holdings':5}
        ledger=Ledger(':memory:',cfg)
        try:
            ledger.initialize('paper','10000','2000','4000','20261101')
            save_meta(ledger,'automatic-rules',rules)
            row,dossier=fixture();current=report(row)
            ledger.opening('paper',row['symbol'],260,'20261104')
            ledger.mark('paper',row['as_of'],current['prices'])
            plan_day(ledger,current,[assess(row,dossier,rules)],rules)
            plans=ledger.open_plans('paper')
            self.assertEqual([(p['side'],p['quantity']) for p in plans],[('sell',60)])
        finally:
            ledger.close()

    def test_daily_catchup_processes_verified_stock_event_idempotently(self):
        item=DISCLOSURES['stock_distributions'][0];event=item['expected'];code=event['ts_code']
        cfg=load_config();ledger=Ledger(':memory:',cfg)
        try:
            ledger.initialize('paper','10000','5000','10000','20230601')
            ledger.opening('paper',code,300,'20230615')
            with tempfile.TemporaryDirectory() as tmp,patch('qmt.service.Tushare') as client, \
                 patch('qmt.service.implemented_dividends',return_value=[event]):
                client.return_value.dossier.return_value={'data':{'dividend':[]},'errors':{}}
                current={'as_of':'20230620','cache_path':tmp,'prices':{code:{'trade_date':'20230620','close':'10'}}}
                update_ledger(Path(tmp),ledger,current)
                update_ledger(Path(tmp),ledger,current)
            self.assertEqual(ledger.positions('paper'),{code:390})
            self.assertEqual(ledger.cash('paper'),Decimal(10300))
            self.assertEqual(len(ledger.summary('paper')['stock_events']),1)
        finally:
            ledger.close()


if __name__=='__main__':
    unittest.main()
