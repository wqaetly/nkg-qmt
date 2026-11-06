import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from qmt.automatic import assess, focus_symbols, plan_day, save_meta
from qmt.config import AppError, load_config
from qmt.ledger import Ledger
from qmt.research import implemented_dividends
from qmt.selection import automatic_path, load_rules, liquidity, select_universe, valuation
from test_automatic import fixture, report


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.rules=load_rules()
        self.sessions=[(datetime(2026,10,1)+timedelta(days=i)).strftime('%Y%m%d') for i in range(20)]

    def test_liquidity_keeps_missing_days_and_rejects_latest_suspension(self):
        amounts={day:Decimal('50000000') for day in self.sessions}
        self.assertEqual(liquidity(amounts,self.sessions,self.rules)['state'],'通过')
        for day in self.sessions[:3]: amounts.pop(day)
        self.assertEqual(liquidity(amounts,self.sessions,self.rules)['state'],'未通过')
        amounts={day:Decimal('50000000') for day in self.sessions[:-1]}
        self.assertEqual(liquidity(amounts,self.sessions,self.rules)['state'],'未通过')

    def history(self,value=20):
        return [{'ts_code':'600001.SH','trade_date':(datetime(2026,1,1)+timedelta(days=i)).strftime('%Y%m%d'),'pe_ttm':value} for i in range(250)]

    def test_valuation_industry_and_history_are_both_required(self):
        from qmt.market import Tushare
        with patch.object(Tushare,'call',return_value=self.history()):
            result=valuation(Tushare(),'600001.SH','20261106',{'pe_ttm':10},[Decimal(15)]*5,self.rules)
            self.assertEqual(result['state'],'通过')
            self.assertEqual(result['percentile'],'0.0')
            self.assertEqual(valuation(Tushare(),'600001.SH','20261106',{'pe_ttm':16},[Decimal(15)]*5,self.rules)['state'],'未通过')
        with patch.object(Tushare,'call',return_value=self.history(8)):
            self.assertEqual(valuation(Tushare(),'600001.SH','20261106',{'pe_ttm':10},[Decimal(15)]*5,self.rules)['state'],'未通过')
        with patch.object(Tushare,'call',return_value=self.history()[:249]):
            self.assertEqual(valuation(Tushare(),'600001.SH','20261106',{'pe_ttm':10},[Decimal(15)]*5,self.rules)['state'],'待核实')

    def test_history_future_or_duplicate_is_rejected(self):
        from qmt.market import Tushare
        for rows in (self.history()+self.history()[:1],[{**self.history()[0],'trade_date':'20261107'}]):
            with patch.object(Tushare,'call',return_value=rows),self.assertRaises(AppError):
                valuation(Tushare(),'600001.SH','20261106',{'pe_ttm':10},[Decimal(15)]*5,self.rules)

    def test_market_selection_needs_no_index_or_affordability_filter(self):
        codes=[f'60000{i}.SH' for i in range(1,7)]
        basics={c:{'name':'测试公司','industry':'测试行业'} for c in codes}
        quotes={c:{'pe_ttm':10,'dv_ttm':4,'close':300} for c in codes}
        rules={**self.rules,'research_watchlist':['600001.SH']}
        class Client:
            def calendar(_,start,end): return self.sessions
            def call(_,api,params,fields):
                if api=='daily':
                    return [{'ts_code':c,'trade_date':params['trade_date'],'amount':50000} for c in codes]
                return [{**r,'ts_code':params['ts_code']} for r in self.history()]
        with tempfile.TemporaryDirectory() as tmp:
            selected,checks,count=select_universe(Client(),self.sessions[-1],basics,quotes,rules,Path(tmp),lambda *a,**k:None)
        self.assertEqual(count,6)
        self.assertEqual(len(selected),6)
        self.assertEqual(checks['600001.SH']['liquidity']['median_amount'],'50000000')

    def test_each_dimension_can_block_entry_and_missing_data_is_not_pass(self):
        row,dossier=fixture()
        for key in ('valuation','liquidity'):
            changed=copy.deepcopy(row);changed['selection'][key]['state']='未通过'
            self.assertEqual(assess(changed,dossier,self.rules)['state'],'等待筛选条件')
        row['selection']['coarse_pass']=False
        self.assertEqual(assess(row,dossier,self.rules)['state'],'等待筛选条件')
        row.pop('selection')
        self.assertEqual(assess(row,dossier,self.rules)['state'],'待数据核实')

    def test_high_payout_and_debt_require_research_not_company_rejection(self):
        row,dossier=fixture(cash_div='1.2')
        self.assertEqual(assess(row,dossier,self.rules)['state'],'待数据核实')
        row,dossier=fixture();dossier['data']['balancesheet'][-1]['st_borr']=10000
        self.assertIn('金融资产',assess(row,dossier,self.rules)['reason'])

    def test_payout_above_old_seventy_percent_cap_is_not_truncated(self):
        row,dossier=fixture(cash_div='.8')
        self.assertEqual(Decimal(assess(row,dossier,self.rules)['payout']),Decimal('.8'))

    def test_watch_names_dont_force_orders_and_old_ledger_path_is_preserved(self):
        account={'positions':{},'plans':[]}
        rows=[{'symbol':'600001.SH','state':'待数据核实'},{'symbol':'600002.SH','state':'满足价格条件','estimated_yield':'.06'}]
        self.assertEqual(focus_symbols(account,rows,watchlist=['600001.SH']),['600001.SH','600002.SH'])
        self.assertEqual(automatic_path(Path('data')),Path('data/auto/value-dividend-v3'))

    def test_missing_held_assessment_does_not_create_sell(self):
        row,_=fixture();cfg=load_config();ledger=Ledger(':memory:',cfg)
        try:
            ledger.initialize('paper','10000','2000','4000','20261105')
            ledger.opening('paper',row['symbol'],100,'20261104')
            save_meta(ledger,'automatic-rules',self.rules)
            r=report(row);r['rows']=[];ledger.mark('paper',r['as_of'],r['prices'])
            self.assertEqual(plan_day(ledger,r,[],self.rules)['plan_ids'],[])
        finally:ledger.close()

    def event_rows(self):
        common={'ts_code':'600600.SH','div_proc':'实施','record_date':'20230713','ex_date':'20230714','pay_date':'20230714','imp_ann_date':'20230707','stk_div':0}
        return [{**common,'end_date':'20221231','ann_date':'20230617','cash_div_tax':1.8},
                {**common,'end_date':'20230323','ann_date':'20230323','cash_div_tax':.5}]

    def test_disclosed_special_dividend_is_not_double_counted(self):
        rows=self.event_rows();events=implemented_dividends(rows,'20261106')
        self.assertEqual(len(events),1)
        self.assertEqual(Decimal(str(events[0]['cash_div_tax'])),Decimal('1.8'))
        self.assertEqual(Decimal(events[0]['ordinary_cash_div_tax']),Decimal('1.3'))
        self.assertEqual(len(events[0]['correction_originals']),2)
        self.assertEqual(implemented_dividends(rows[:1],'20261106')[0]['ordinary_cash_div_tax'],'1.30')
        self.assertEqual(implemented_dividends(rows,'20230706'),[])
        rows[0]['cash_div_tax']=1.9
        with self.assertRaises(AppError):implemented_dividends(rows,'20261106')


if __name__=='__main__':unittest.main()
