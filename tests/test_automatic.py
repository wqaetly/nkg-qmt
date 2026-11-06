import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from qmt.automatic import assess, allocate, plan_day, load_rules, save_meta, run_automatic, benchmark, focus_symbols
from qmt.config import AppError, load_config, workspace_lock
from qmt.ledger import Ledger
from qmt.market import Tushare
from qmt.research import summarize
from qmt.report import chart_panel


def fixture(day='20261105',symbol='600001.SH',price='8'):
    data={key:[] for key in ('income','fina_indicator','cashflow','balancesheet','fina_audit','dividend')}
    def period(end,ann,profit,deducted):
        common={'ts_code':symbol,'end_date':end,'ann_date':ann,'report_type':'1'}
        data['income'].append({**common,'comp_type':'1','n_income_attr_p':profit})
        data['fina_indicator'].append({**common,'profit_dedt':deducted})
        data['cashflow'].append({**common,'n_cashflow_act':2000,'c_pay_acq_const_fiolta':300})
        data['balancesheet'].append({**common,'total_share':1000,'total_assets':10000,'total_liab':1000,'money_cap':1000,'st_borr':100,'non_cur_liab_due_1y':100})
    for year in range(2021,2026):
        period(f'{year}1231',f'{year+1}0401',1000,900)
        data['fina_audit'].append({'end_date':f'{year}1231','ann_date':f'{year+1}0401','audit_result':'标准无保留意见'})
        data['dividend'].append({'end_date':f'{year}1231','ann_date':f'{year+1}0401','imp_ann_date':f'{year+1}0501',
            'div_proc':'实施','record_date':f'{year+1}0510','ex_date':f'{year+1}0511','pay_date':f'{year+1}0512','cash_div_tax':'.6','stk_div':0})
    period('20250930','20251030',750,675)
    period('20260930','20261030',750,675)
    dossier={'symbol':symbol,'as_of':day,'data':data,'errors':{}}
    row=summarize(dossier,{'name':'虚构测试公司','industry':'测试行业'},{'trade_date':day,'close':price,'total_share':'.1','dv_ttm':7.5})
    row['selection']={'coarse_pass':True,'valuation':{'state':'通过','reason':'测试估值'},'liquidity':{'state':'通过','reason':'测试成交额'}}
    return row,dossier


def report(row,next_day='20261106'):
    return {'source':'Tushare','as_of':row['as_of'],'requested_date':row['as_of'],'next_session':next_day,
        'rows':[row],'industries':{row['symbol']:row['industry']},'prices':{row['symbol']:{'trade_date':row['as_of'],'close':row['price']}}}


class AutomaticTests(unittest.TestCase):
    def setUp(self):
        self.cfg=load_config();self.rules={**load_rules(),'max_holdings':5};self.ledger=Ledger(':memory:',self.cfg)
        self.ledger.initialize('paper','10000','2000','4000','20261105')
        save_meta(self.ledger,'automatic-rules',self.rules)

    def tearDown(self):self.ledger.close()

    def test_mechanical_scenarios_use_profit_not_cashflow(self):
        row,dossier=fixture()
        result=assess(row,dossier,self.rules)
        self.assertTrue(row['gate'])
        self.assertEqual(result['state'],'满足价格条件')
        self.assertEqual(Decimal(result['estimated_eps']),Decimal('.9'))
        self.assertEqual(Decimal(result['scenarios'][1]['scenario_dividend_per_share']),Decimal('.54'))
        self.assertFalse(row['scenarios'])
        self.assertNotEqual(row['status'],'可进一步评估')

    def test_future_disclosure_does_not_enter_ttm(self):
        row,dossier=fixture()
        future={**dossier['data']['income'][-1],'ann_date':'20261110','n_income_attr_p':999999}
        dossier['data']['income'].append(future)
        self.assertEqual(assess(row,dossier,self.rules)['estimated_eps'],'0.9')

    def test_new_year_uses_latest_disclosed_annual_not_unpublished_year(self):
        row,dossier=fixture(day='20270105')
        self.assertEqual(assess(row,dossier,self.rules)['state'],'满足价格条件')

    def test_missing_debt_freezes_not_zero(self):
        row,dossier=fixture()
        dossier['data']['balancesheet'][-1]['st_borr']=None
        self.assertEqual(assess(row,dossier,self.rules)['state'],'待数据核实')

    def test_negative_cashflow_is_risk_exit(self):
        row,dossier=fixture();row['gate']=False;row['annual'][0]['simple_fcf']='-1'
        self.assertEqual(assess(row,dossier,self.rules)['state'],'排除')

    def test_expensive_lot_is_not_forced(self):
        row,dossier=fixture(price='39')
        a=assess(row,dossier,self.rules)
        a.update(state='满足价格条件',estimated_yield='.06',reference_price='50')
        r=report(row);self.ledger.mark('paper',r['as_of'],r['prices'])
        allocation=allocate(self.ledger,r,[a],self.rules)
        self.assertEqual(allocation['targets'],{})
        self.assertIn('不足一手',allocation['allocation_notes'][row['symbol']])

    def test_full_signal_fill_hold_risk_exit_cycle_and_idempotency(self):
        row,dossier=fixture();r=report(row);a=assess(row,dossier,self.rules)
        self.ledger.mark('paper',r['as_of'],r['prices'])
        first=plan_day(self.ledger,r,[a],self.rules)
        self.assertEqual(len(first['plan_ids']),1)
        self.assertEqual(plan_day(self.ledger,r,[a],self.rules),first)
        self.assertEqual(len(self.ledger.open_plans('paper')),1)
        self.assertEqual(self.ledger.positions('paper'),{})
        bar={key:Decimal(value) for key,value in {'open':'8','high':'8.2','low':'7.9','close':'8','up':'8.8','down':'7.2','vol':'10000'}.items()}
        self.ledger.settle_paper('20261106',{row['symbol']:{'20261106':bar}})
        self.assertEqual(self.ledger.positions('paper'),{row['symbol']:200})
        row,dossier=fixture(day='20261106');r=report(row,'20261109');a=assess(row,dossier,self.rules)
        self.ledger.mark('paper',r['as_of'],r['prices'])
        self.assertEqual(plan_day(self.ledger,r,[a],self.rules)['plan_ids'],[])
        row,dossier=fixture(day='20261109');r=report(row,'20261110');a=assess(row,dossier,self.rules)
        a.update(state='排除',reason='虚构测试利润恶化')
        self.ledger.mark('paper',r['as_of'],r['prices'])
        result=plan_day(self.ledger,r,[a],self.rules)
        self.assertEqual(len(result['plan_ids']),1)
        self.assertEqual(self.ledger.open_plans('paper')[0]['side'],'sell')
        self.ledger.settle_paper('20261110',{row['symbol']:{'20261110':bar}})
        self.assertEqual(self.ledger.positions('paper'),{})
        self.assertEqual(len(self.ledger.summary('paper')['fills']),2)

    def test_pending_orders_reserve_and_do_not_duplicate(self):
        row,dossier=fixture();r=report(row);a=assess(row,dossier,self.rules)
        self.ledger.mark('paper',r['as_of'],r['prices']);plan_day(self.ledger,r,[a],self.rules)
        row2,dossier2=fixture(day='20261106');r2=report(row2,'20261109')
        self.ledger.mark('paper',r2['as_of'],r2['prices'])
        result=plan_day(self.ledger,r2,[assess(row2,dossier2,self.rules)],self.rules)
        self.assertEqual(result['plan_ids'],[])
        self.assertEqual(len(self.ledger.open_plans('paper')),1)

    def test_missing_held_data_blocks_buys_without_selling(self):
        row,dossier=fixture();self.ledger.opening('paper',row['symbol'],100,'20261104')
        r=report(row);self.ledger.mark('paper',r['as_of'],r['prices'])
        a=assess(row,dossier,self.rules);a.update(state='待数据核实')
        self.assertEqual(plan_day(self.ledger,r,[a],self.rules)['plan_ids'],[])
        self.assertEqual(self.ledger.positions('paper'),{row['symbol']:100})

    def test_automatic_signal_cannot_enter_manual_account(self):
        self.ledger.initialize('manual','10000','2000','4000','20261105')
        row,_=fixture();row['automatic_signal']=True
        with self.assertRaises(AppError):
            self.ledger.create_plan('manual',row,'20261105','20261106',100,'8','buy','test',{}, {row['symbol']:row['industry']})

    def test_partial_universe_rejected(self):
        with tempfile.TemporaryDirectory() as tmp,self.assertRaises(AppError):
            run_automatic(Path(tmp),{'source':'Tushare','complete':False,'covered':3},self.cfg)

    def test_monthly_targets_do_not_follow_daily_price_drop(self):
        row,dossier=fixture();r=report(row);a=assess(row,dossier,self.rules)
        self.ledger.mark('paper',r['as_of'],r['prices']);first=plan_day(self.ledger,r,[a],self.rules)
        self.ledger.close_plan(first['plan_ids'][0],'测试取消')
        row,dossier=fixture(day='20261106',price='4');r=report(row,'20261109')
        self.ledger.mark('paper',r['as_of'],r['prices'])
        second=plan_day(self.ledger,r,[assess(row,dossier,self.rules)],self.rules)
        self.assertEqual(second['target']['targets'],first['target']['targets'])

    def test_focus_is_at_most_five_and_preserves_holdings(self):
        account={'positions':{'600099.SH':100},'plans':[]}
        assessments=[{'symbol':f'60000{i}.SH','estimated_yield':str(i/100)} for i in range(1,9)]
        focused=focus_symbols(account,assessments)
        self.assertEqual(len(focused),5)
        self.assertEqual(focused[0],'600099.SH')
        self.assertEqual(focused[1],'600008.SH')

    def test_sector_budget_keeps_unused_slots_in_cash(self):
        rows=[fixture(symbol=f'60000{i}.SH')[0] for i in range(1,7)]
        r=report(rows[0]);r['rows']=rows
        self.ledger.mark('paper',r['as_of'],{})
        assessments=[assess(row,fixture(symbol=row['symbol'])[1],self.rules) for row in rows]
        targets=allocate(self.ledger,r,assessments,self.rules)['targets']
        self.assertLessEqual(len(targets),3)
        self.assertLessEqual(sum(qty*Decimal('8.08') for qty in targets.values()),Decimal('4000'))

    def test_liquidity_cap_does_not_invent_fill(self):
        row,dossier=fixture();r=report(row);a=assess(row,dossier,self.rules)
        self.ledger.mark('paper',r['as_of'],r['prices']);plan_day(self.ledger,r,[a],self.rules)
        bar={key:Decimal(value) for key,value in {'open':'8','high':'8.2','low':'7.9','close':'8','up':'8.8','down':'7.2','vol':'100'}.items()}
        self.ledger.settle_paper('20261106',{row['symbol']:{'20261106':bar}})
        self.assertEqual(self.ledger.positions('paper'),{})
        self.assertEqual(self.ledger.summary('paper')['plans'][0]['status'],'expired')


class ChartAndBenchmarkTests(unittest.TestCase):
    def test_etf_factor_ratio_accounts_for_distribution(self):
        def response(api,params,fields):
            key='close' if api=='fund_daily' else 'adj_factor'
            values=('10','9') if key=='close' else ('1','1.2')
            return [{'ts_code':'515180.SH','trade_date':day,key:value} for day,value in zip(('20261105','20261106'),values)]
        with patch.object(Tushare,'call',side_effect=response):
            self.assertEqual(Decimal(benchmark(Tushare(),'20261105','20261106',load_rules())['return']),Decimal('.08'))

    def test_missing_benchmark_factor_is_not_price_only_fallback(self):
        with patch.object(Tushare,'call',return_value=[]),self.assertRaises(AppError):
            benchmark(Tushare(),'20261105','20261106',load_rules())

    def test_candle_boundary_validations(self):
        row={'ts_code':'600001.SH','trade_date':'20261105','open':8,'high':9,'low':7,'close':8.5,'vol':1}
        with patch.object(Tushare,'call',return_value=[row]):
            self.assertEqual(Tushare().candles('600001.SH','20261101','20261106')[0]['day'],'20261105')
        for rows in ([row,row],[{**row,'low':10}],[{**row,'ts_code':'600002.SH'}]):
            with patch.object(Tushare,'call',return_value=rows),self.assertRaises(AppError):
                Tushare().candles('600001.SH','20261101','20261106')

    def test_chart_payload_escapes_script_and_keeps_order_dates_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            row,_=fixture();r=report(row)
            data={'assessments':[],'notes':{},'chart_plans':[{'symbol':row['symbol'],'signal_date':'20261105','execute_date':'20261106','note':'</script><script>alert(1)</script>'}],
                  'chart_fills':[{'symbol':row['symbol'],'trade_date':'20261106','price':'8'}]}
            page=chart_panel(Path(tmp),r,data)
            self.assertNotIn('</script><script>alert(1)',page)
            self.assertIn('signal_date',page);self.assertIn('trade_date',page)

    def test_workspace_prevents_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as tmp,workspace_lock(Path(tmp)):
            with self.assertRaises(AppError),workspace_lock(Path(tmp)):
                pass


if __name__=='__main__':unittest.main()
