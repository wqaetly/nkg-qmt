import copy
import contextlib
import io
import json
import tempfile
import unittest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from qmt.config import AppError,load_config,dec,date_key
from qmt.ledger import Ledger
from qmt.market import Tushare
from qmt.research import statements,implemented_dividends,evaluate_review,review_template,summarize
from qmt.report import render
from qmt.demo import create_demo
from qmt.__main__ import main
from qmt.service import update_ledger
from vendor.dividend_stock_research.dividend_price import calculate


def candidate(symbol='600001.SH',industry='测试行业'):
    return {'symbol':symbol,'industry':industry,'name':'虚构公司','gate':True,'scenarios':[1],
            'evidence_hash':'facts','as_of':'20261106','price':'10','status':'可进一步评估','issues':[]}


def review_for(row):
    review=review_template(row)
    review.update(approved=True,status='可进一步评估',ordinary_dividend_only=True,
                  opposing_reasons=['需求下降'],invalidation_events=['削减派息'])
    for item in review['evidence'].values():
        item.update(status='支持',reason='虚构测试证据',url='https://example.com/filing',ann_date='20261030',original_read=True)
    for scenario,eps in zip(review['scenarios'],('1','2','3')):
        scenario.update(eps=eps,payout='0.6',haircut='0.8',reason='虚构盈利及派息假设')
    return review


class FormulaTests(unittest.TestCase):
    def test_upstream_example(self):
        r=calculate('2','0.6','0.8','0.04','20')
        self.assertEqual(Decimal(r['scenario_dividend_per_share']),Decimal('.96'))
        self.assertEqual(Decimal(r['reference_price_unrounded']),Decimal('24'))
        self.assertEqual(Decimal(r['scenario_yield_at_price']),Decimal('.048'))

    def test_invalid_model_inputs(self):
        for params in [('-1','.6','.8','.04'),('2','1.1','.8','.04'),('2','.6','NaN','.04'),('2','.6','.8','0')]:
            with self.subTest(params=params),self.assertRaises(ValueError):
                calculate(*params)

    def test_yield_and_haircut_direction(self):
        a=calculate('2','.6','1','.04')
        for r in (calculate('2','.6','.8','.04'),calculate('2','.6','1','.06')):
            self.assertLess(Decimal(r['reference_price_unrounded']),Decimal(a['reference_price_unrounded']))

    def test_zero_dividend(self):
        self.assertEqual(Decimal(calculate('2','0','1','.05')['reference_price_unrounded']),0)


class ResearchTests(unittest.TestCase):
    def test_statements_preserve_periods_and_disclosure(self):
        rows=[{'end_date':'20260630','ann_date':'20260801','report_type':'1','profit':6},
              {'end_date':'20260930','ann_date':'20261030','report_type':'1','profit':9},
              {'end_date':'20260630','ann_date':'20261201','report_type':'4','profit':60}]
        result=statements(rows,'20261106')
        self.assertEqual(result['20260630']['profit'],6)
        self.assertEqual(result['20260930']['profit'],9)

    def test_null_duplicates_merge_conflicts_block(self):
        base={'end_date':'20260930','ann_date':'20261030','profit':9,'roe':None}
        self.assertEqual(statements([base,{**base,'roe':5}],'20261106')['20260930']['roe'],5)
        with self.assertRaises(AppError):
            statements([base,{**base,'profit':99}],'20261106')

    def event(self):
        return {'end_date':'20251231','ann_date':'20260301','imp_ann_date':'20260501','div_proc':'实施',
                'cash_div_tax':'.5','stk_div':'0','record_date':'20260510','ex_date':'20260511','pay_date':'20260512'}

    def test_dividend_dedup_and_proposals(self):
        event=self.event()
        rows=[event,dict(event),{**event,'div_proc':'预案','cash_div_tax':'5'}]
        self.assertEqual(len(implemented_dividends(rows,'20261106')),1)
        self.assertEqual(implemented_dividends(rows,'20260401'),[])
        interim={**event,'end_date':'20250630','record_date':'20251001','ex_date':'20251002','pay_date':'20251003'}
        self.assertEqual(len(implemented_dividends([event,interim],'20261106')),2)

    def test_missing_dividend_value_is_not_zero(self):
        with self.assertRaises(AppError):
            implemented_dividends([{**self.event(),'cash_div_tax':None}],'20261106')

    def test_review_gates_and_nine_scenarios(self):
        cfg=load_config(); row=candidate(); review=review_for(row)
        self.assertEqual(len(evaluate_review(row,review,cfg)[0]),9)
        for change in ({'approved':False},{'evidence_hash':'old'},{'reviewed_on':'20260101'}):
            with self.subTest(change=change):
                revised={**review,**change}
                if change.get('approved') is False:
                    self.assertEqual(evaluate_review(row,revised,cfg)[0],[])
                else:
                    with self.assertRaises(AppError):evaluate_review(row,revised,cfg)
        with self.assertRaises(AppError):evaluate_review({**row,'gate':False},review,cfg)
        review['evidence']['cash_and_debt']['original_read']=False
        with self.assertRaises(AppError):evaluate_review(row,review,cfg)
        review['evidence']['cash_and_debt']['original_read']='false'
        with self.assertRaises(AppError):evaluate_review(row,review,cfg)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.cfg=load_config(); self.ledger=Ledger(':memory:',self.cfg)
        for mode in ('paper','manual'):
            self.ledger.initialize(mode,'10000','2000','4000','20261101')

    def tearDown(self):self.ledger.close()

    def plan(self,mode='manual',side='buy',day='20261102',next_day='20261103',quantity=100,symbol='600001.SH',price='10.00',quotes=None):
        row=candidate(symbol)
        return self.ledger.create_plan(mode,row,day,next_day,quantity,price,side,'测试原因',quotes or {},{symbol:'测试行业'})

    def test_partial_fills_idempotency_and_conflicting_ref(self):
        p=self.plan()
        self.assertTrue(self.ledger.record_manual(p,'20261103',40,'10','2','a'))
        self.assertFalse(self.ledger.record_manual(p,'20261103',40,'10','2','a'))
        self.assertEqual(self.ledger.cash('manual'),Decimal('9598'))
        with self.assertRaises(AppError):self.ledger.record_manual(p,'20261103',41,'10','2','a')
        self.ledger.record_manual(p,'20261103',60,'10','3','b')
        self.assertEqual(self.ledger.positions('manual'),{'600001.SH':100})
        self.assertEqual(self.ledger.cash('paper'),Decimal('10000'))

    def test_overfill_and_future_plan_date_rejected_atomically(self):
        p=self.plan()
        for day,qty in [('20261102',100),('20261103',101)]:
            with self.assertRaises(AppError):self.ledger.record_manual(p,day,qty,'10','5','bad')
        self.assertEqual(self.ledger.positions('manual'),{})

    def test_t_plus_one(self):
        self.ledger.opening('manual','600001.SH',100,'20261103')
        p=self.plan(side='sell')
        with self.assertRaises(AppError):self.ledger.record_manual(p,'20261103',100,'10','5','x')

    def test_selling_old_then_buying_cannot_resell_today(self):
        self.ledger.opening('manual','600001.SH',100,'20261101')
        sell=self.plan(side='sell',day='20261101')
        self.ledger.record_manual(sell,'20261103',100,'10','5','old-sell')
        buy=self.plan()
        self.ledger.record_manual(buy,'20261103',100,'10','5','new-buy')
        resell=self.plan(side='sell')
        with self.assertRaises(AppError):
            self.ledger.record_manual(resell,'20261103',100,'10','5','resell')
        self.assertEqual(self.ledger.positions('manual'),{'600001.SH':100})

    def test_duplicate_plan_and_position_budget(self):
        p=self.plan()
        with self.assertRaises(AppError):self.plan()
        self.ledger.close_plan(p,'未执行，已核对')
        with self.assertRaises(AppError):self.plan()
        with self.assertRaises(AppError):self.plan(symbol='600002.SH',quantity=300)

    def test_cash_reservation(self):
        self.ledger.db.execute("UPDATE accounts SET initial_cash='1500',max_position='1500',max_sector='1500' WHERE mode='manual'")
        self.ledger.db.commit()
        self.plan()
        with self.assertRaises(AppError):self.plan(symbol='600002.SH')

    def test_missing_bar_preserves_plan(self):
        self.plan(mode='paper')
        with self.assertRaises(AppError):self.ledger.settle_paper('20261103',{})
        self.assertEqual(len(self.ledger.open_plans('paper')),1)
        self.assertEqual(self.ledger.positions('paper'),{})

    def test_price_gap_does_not_fake_fill(self):
        self.plan(mode='paper')
        bar={'open':Decimal('11'),'low':Decimal('10'),'high':Decimal('12'),'up':Decimal('12'),'down':Decimal('8'),'vol':1000}
        self.ledger.settle_paper('20261103',{'600001.SH':{'20261103':bar}})
        self.assertEqual(self.ledger.positions('paper'),{})
        self.assertEqual(self.ledger.summary('paper')['plans'][0]['status'],'expired')

    def test_dividend_receivable_and_payment_only_once(self):
        self.ledger.opening('paper','600001.SH',100,'20261101')
        events={'600001.SH':[{'record_date':'20261103','ex_date':'20261104','pay_date':'20261106','cash_div_tax':'.5','stk_div':'0'}]}
        self.ledger.dividends(events,'20261104')
        self.assertEqual(self.ledger.cash('paper'),Decimal('10000'))
        self.ledger.mark('paper','20261104',{'600001.SH':{'trade_date':'20261104','close':'9.50'}})
        mark=self.ledger.summary('paper')['marks'][-1]
        self.assertEqual(Decimal(mark['receivable']),50)
        self.assertEqual(Decimal(mark['equity']),11000)
        self.ledger.dividends(events,'20261106'); self.ledger.dividends(events,'20261106')
        self.assertEqual(self.ledger.cash('paper'),Decimal('10050'))
        self.assertEqual(self.ledger.cash('manual'),Decimal('10000'))

    def test_ex_date_buyer_has_no_previous_dividend(self):
        self.ledger.opening('paper','600001.SH',100,'20261104')
        events={'600001.SH':[{'record_date':'20261103','ex_date':'20261104','pay_date':'20261106','cash_div_tax':'.5','stk_div':'0'}]}
        self.ledger.dividends(events,'20261106')
        self.assertEqual(self.ledger.cash('paper'),Decimal('10000'))

    def test_dividend_before_initial_cash_is_not_added_again(self):
        self.ledger.opening('paper','600001.SH',100,'20261001')
        events={'600001.SH':[{'record_date':'20261028','ex_date':'20261029','pay_date':'20261101','cash_div_tax':'.5','stk_div':'0'}]}
        self.ledger.dividends(events,'20261106')
        self.assertEqual(self.ledger.cash('paper'),Decimal('10000'))
        with self.assertRaises(AppError):
            self.ledger.cash_event('old','20261031','50','600001.SH','期初已含')

    def test_cash_and_fills_must_be_chronological(self):
        p=self.plan()
        self.ledger.record_manual(p,'20261103',100,'10','5','buy')
        with self.assertRaises(AppError):
            self.ledger.cash_event('tax','20261102','-9500','600001.SH','补税')
        self.ledger.cash_event('div','20261106','50','600001.SH','分红')
        p=self.plan(day='20261103',next_day='20261104',quotes={'600001.SH':{'trade_date':'20261103','close':'10'}})
        with self.assertRaises(AppError):
            self.ledger.record_manual(p,'20261104',100,'10','5','late-buy')

    def test_string_zero_stock_dividend_does_not_stop_update(self):
        self.ledger.opening('paper','600001.SH',100,'20261101')
        events=[{'record_date':'20261103','ex_date':'20261104','pay_date':'20261106','cash_div_tax':'.5','stk_div':'0'}]
        with tempfile.TemporaryDirectory() as tmp,patch('qmt.service.Tushare') as client,patch('qmt.service.implemented_dividends',return_value=events):
            client.return_value.dossier.return_value={'errors':{},'data':{'dividend':[]}}
            update_ledger(Path(tmp),self.ledger,{'as_of':'20261106','cache_path':tmp,'prices':{'600001.SH':{'trade_date':'20261106','close':'10'}}})
        self.assertEqual(self.ledger.cash('paper'),Decimal('10050'))

    def test_split_stops_and_cash_event_dedup(self):
        self.ledger.opening('paper','600001.SH',100,'20261101')
        events={'600001.SH':[{'record_date':'20261103','ex_date':'20261104','pay_date':'20261106','cash_div_tax':'.5','stk_div':'.1'}]}
        with self.assertRaises(AppError):self.ledger.dividends(events,'20261106')
        self.ledger.cash_event('div1','20261106','50','600001.SH','实收分红')
        self.ledger.cash_event('div1','20261106','50','600001.SH','实收分红')
        self.ledger.cash_event('tax1','20261106','-5','600001.SH','实际补税')
        self.assertEqual(self.ledger.cash('manual'),Decimal('10045'))


class IntegrationTests(unittest.TestCase):
    def test_stale_or_failed_report_cannot_create_plan(self):
        now=datetime(2026,11,6,22,tzinfo=timezone(timedelta(hours=8)))
        for ok,day in ((False,'20261106'),(True,'20261105')):
            with tempfile.TemporaryDirectory() as tmp,patch('qmt.__main__.now_cn',return_value=now),contextlib.redirect_stderr(io.StringIO()),contextlib.redirect_stdout(io.StringIO()):
                workspace=Path(tmp)
                (workspace/'status.json').write_text(json.dumps({'ok':ok}),encoding='utf-8')
                (workspace/'screen.json').write_text(json.dumps({'as_of':day,'requested_date':day,'rows':[]}),encoding='utf-8')
                self.assertEqual(main(['--workspace',tmp,'plan','--symbol','600001.SH','--quantity','100']),1)
                ledger=Ledger(workspace/'ledger.sqlite3',load_config())
                self.assertEqual(ledger.open_plans('paper'),[])
                ledger.close()

    def test_held_stock_can_exit_after_leaving_index(self):
        now=datetime(2026,11,6,22,tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as tmp,patch('qmt.__main__.now_cn',return_value=now),contextlib.redirect_stdout(io.StringIO()):
            workspace=Path(tmp)
            ledger=Ledger(workspace/'ledger.sqlite3',load_config())
            ledger.initialize('manual','10000','2000','4000','20261101')
            ledger.opening('manual','600001.SH',100,'20261101')
            ledger.close()
            (workspace/'status.json').write_text(json.dumps({'ok':True}),encoding='utf-8')
            (workspace/'screen.json').write_text(json.dumps({'as_of':'20261106','requested_date':'20261106','next_session':'20261109','rows':[],'prices':{},'industries':{}}),encoding='utf-8')
            self.assertEqual(main(['--workspace',tmp,'plan','--mode','manual','--side','sell','--symbol','600001.SH','--quantity','100','--price','10','--reason','退出指数后核实卖出']),0)
            ledger=Ledger(workspace/'ledger.sqlite3',load_config())
            self.assertEqual(len(ledger.open_plans('manual')),1)
            ledger.close()

    def test_invalid_numeric_and_date(self):
        for value in ('NaN','Infinity','-1',None):
            with self.assertRaises(AppError):dec(value)
        with self.assertRaises(AppError):date_key('20260230')

    def test_offline_demo_and_report_escaping(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp)
            path=create_demo(workspace,load_config())
            page=path.read_text(encoding='utf-8')
            self.assertIn('虚构演示数据',page)
            self.assertIn('10044.00'.replace('10044','10,044'),page)
            ledger=Ledger(workspace/'ledger.sqlite3',load_config())
            report=json.loads((workspace/'screen.json').read_text(encoding='utf-8'))
            report['rows'][0]['name']='<script>alert(1)</script>'
            text=render(workspace,ledger,report).read_text(encoding='utf-8')
            self.assertNotIn('<script>alert(1)</script>',text)
            self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;',text)
            ledger.close()

    def test_api_error_never_echoes_upstream_secret(self):
        with patch.dict('os.environ',{'TUSHARE_TOKEN':'fake'}),patch('qmt.market.request_json',return_value={'code':-1,'msg':'SECRET'}):
            with self.assertRaises(AppError) as e:Tushare().call('daily',{},'ts_code')
            self.assertNotIn('SECRET',str(e.exception))

    def test_api_schema_failure(self):
        with patch.dict('os.environ',{'TUSHARE_TOKEN':'fake'}),patch('qmt.market.request_json',return_value={'code':0,'data':{'fields':['ts_code'],'items':[['a','b']]}}):
            with self.assertRaises(AppError):Tushare().call('daily',{},'ts_code')


if __name__=='__main__':unittest.main()
