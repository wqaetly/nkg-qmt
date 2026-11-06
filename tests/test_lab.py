import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, call, patch

from qmt.config import AppError, atomic_json, load_config
from qmt.experiment import historical_name, prepare_experiment, run_experiment, cohort_assessment, EXPERIMENT, LONG_EXPERIMENT, QUALITY_EXPERIMENT, QUALITY_LONG_EXPERIMENT, history_bounds, historical_input_issues, protocol
from qmt.lab import research_pool, run_lab
from qmt.selection import automatic_path, load_rules
from qmt.service import read_json
from test_automatic import fixture


def pool_inputs():
    rows=[]; assessments=[]
    for i in range(30):
        code=f'600{i:03d}.SH'
        rows.append({'symbol':code,'name':'示例'+str(i),'industry':str(i%10),'price':100,'evidence_hash':'hash'})
        assessments.append({'symbol':code,'state':'等待价格','reason':'未达入场价','estimated_yield':str(.03-i*.0001),
            'dimensions':{'valuation':{'state':'通过'},'liquidity':{'state':'通过'},'dividend':{'state':'等待价格'}}})
    report={'source':'Tushare','complete':True,'as_of':'20261106','rows':rows,'selection_rules':{'version':'test'}}
    automatic={'day':'20261106','rules':report['selection_rules'],'assessments':assessments,'focus_symbols':[]}
    return report,automatic


def quality_client(codes=('600001.SH','600002.SH','600003.SH','600004.SH')):
    day,end='20261105','20261106'
    sessions=sorted((datetime.strptime(day,'%Y%m%d')-timedelta(days=i)).strftime('%Y%m%d')
                    for i in range(30) if (datetime.strptime(day,'%Y%m%d')-timedelta(days=i)).weekday()<5)[-20:]
    dossiers={code:fixture(symbol=code)[1] for code in codes}
    # The highest-yield candidate's missing audit becomes available only after selection.
    if '600001.SH' in dossiers:dossiers['600001.SH']['data']['fina_audit'][-1]['ann_date']=end
    if '600002.SH' in dossiers:dossiers['600002.SH']['data']['balancesheet'][-1]['total_liab']=9000
    quotes={code:{'ts_code':code,'trade_date':day,'close':'20' if code=='600003.SH' else '8',
                  'pe_ttm':'10','total_share':'.1','dv_ttm':10-i} for i,code in enumerate(codes)}
    members=[{'ts_code':code,'l1_code':'801000.SI','l1_name':'测试历史行业',
              'in_date':'20100101','out_date':None,'is_new':'Y'} for code in codes]
    def response(api,params,fields):
        if api=='stock_basic':
            return [{'ts_code':f'600{i:03d}.SH','list_date':'20100101','delist_date':None,
                     'industry':'当前标签不可倒灌'} for i in range(1000)] if params['list_status']=='L' else []
        if api=='daily':
            return [{'ts_code':code,'trade_date':params['trade_date'],'amount':100000} for code in codes]
        if api=='namechange':
            return [{'ts_code':params['ts_code'],'name':'虚构测试公司','start_date':'20100101',
                     'end_date':None,'ann_date':'20100101'}]
        if api=='index_member_all':
            return [] if params['is_new']=='N' else [r for r in members if r['ts_code']==params.get('ts_code',r['ts_code'])]
        raise AssertionError(api)
    def dossier(code,as_of,start_year):
        return {**copy.deepcopy(dossiers[code]),'as_of':as_of}
    def yearly(api,code,start,end,fields):
        if api=='income':return [r for r in dossiers[code]['data']['income'] if start<=r['ann_date']<=end]
        if api=='daily':return [{'ts_code':code,'trade_date':day,'amount':100000}]
        if api=='daily_basic':return [quotes[code]]
        raise AssertionError(api)
    client=Mock()
    client.call.side_effect=response
    client.prices.return_value=quotes
    client.calendar.side_effect=lambda start,finish: [day,end] if start==day else sessions+([end] if finish==end else [])
    client.dossier.side_effect=dossier
    client.yearly.side_effect=yearly
    client.bars.return_value={}
    return client


class LabTests(unittest.TestCase):
    def test_cohort_financial_gates_allow_waiting_price_but_not_unknown_or_risk(self):
        rules=load_rules()
        for price,state in (('8','满足价格条件'),('20','等待价格')):
            row,dossier=fixture(price=price)
            result=cohort_assessment(row,dossier,rules)
            self.assertEqual(result['state'],state)
            self.assertTrue(result['eligible'])
            self.assertEqual(result['evidence_hash'],row['evidence_hash'])
        row,dossier=fixture()
        dossier['data']['balancesheet'][-1]['st_borr']=None
        self.assertEqual(cohort_assessment(row,dossier,rules)['state'],'待数据核实')
        self.assertFalse(cohort_assessment(row,dossier,rules)['eligible'])
        dossier['data']['balancesheet'][-1].update(st_borr=100,total_liab=9000)
        self.assertEqual(cohort_assessment(row,dossier,rules)['state'],'排除')
        self.assertFalse(cohort_assessment(row,dossier,rules)['eligible'])

    def test_quality_cohort_uses_selection_day_and_keeps_shortfall(self):
        client=quality_client();cfg=load_config()
        experiment={**QUALITY_EXPERIMENT,'selection_day':'20261105','validation_start':'20261105'}
        with tempfile.TemporaryDirectory() as tmp,patch('qmt.experiment.Tushare',return_value=client),patch('qmt.experiment.now_cn',return_value=datetime(2026,11,7)):
            path,_,cohort,data,dossiers,*_=prepare_experiment(Path(tmp),'20261106',lambda *a,**k:None,cfg,experiment)
            self.assertEqual([r['symbol'] for r in cohort['stocks']],['600003.SH','600004.SH'])
            self.assertEqual([r['selection_quality']['state'] for r in cohort['stocks']],['等待价格','满足价格条件'])
            self.assertEqual([r['state'] for r in cohort['rejected']],['待数据核实','排除'])
            self.assertEqual((cohort['candidate_count'],cohort['evaluated_count'],cohort['target_count']),(4,4,20))
            self.assertEqual(client.dossier.call_args_list,
                [call(f'60000{i}.SH','20261105',start_year=2021) for i in range(1,5)]+
                [call(f'60000{i}.SH','20261106',start_year=2021) for i in (3,4)])
            self.assertTrue(all(d['as_of']=='20261106' for d in dossiers.values()))
            self.assertEqual(set(data['bars']),{'600003.SH','600004.SH'})
            self.assertEqual(read_json(path/'cohort.json'),cohort)
            self.assertFalse(data['errors'])
            with self.assertRaisesRegex(AppError,'协议已变化'):
                prepare_experiment(Path(tmp),'20261106',cfg=cfg,experiment={**experiment,'quality_first':False})

    def test_zero_quality_candidates_preserve_audit_without_successful_returns(self):
        client=quality_client(('600001.SH',));cfg=load_config()
        experiment={**QUALITY_EXPERIMENT,'selection_day':'20261105','validation_start':'20261105'}
        with tempfile.TemporaryDirectory() as tmp,patch('qmt.experiment.Tushare',return_value=client),patch('qmt.experiment.now_cn',return_value=datetime(2026,11,7)),patch('qmt.comparison.build_comparisons',return_value=[]):
            result=run_experiment(Path(tmp),'20261106',cfg,lambda *a,**k:None,experiment)
            path=Path(tmp)/'backtests'/experiment['version']
            cohort=read_json(path/'cohort.json')
            self.assertEqual(cohort['stocks'],[])
            self.assertEqual(cohort['rejected'][0]['state'],'待数据核实')
            self.assertEqual(cohort['evaluated_count'],1)
            self.assertIn('cohort',read_json(path/'market.json')['errors'])
            self.assertEqual(result['status'],'blocked')
            self.assertEqual(result['primary_metrics'],{})
            self.assertEqual(len(result['runs']),len(experiment['entry_yields']))
            self.assertTrue(all(r['status']=='blocked' and r['metrics']=={} for r in result['runs']))
            self.assertFalse(list(path.glob('*-result.json')))
            client.dossier.assert_called_once_with('600001.SH','20261105',start_year=2021)
            client.bars.assert_not_called()

    def test_quality_protocol_is_separate_and_preserves_other_registered_rules(self):
        cfg=load_config()
        for original,quality in ((EXPERIMENT,QUALITY_EXPERIMENT),(LONG_EXPERIMENT,QUALITY_LONG_EXPERIMENT)):
            old,new=protocol(cfg,original,'20261106'),protocol(cfg,quality,'20261106')
            self.assertNotEqual(old.pop('version'),new.pop('version'))
            self.assertTrue(new.pop('quality_first'))
            self.assertEqual(old,new)

    def test_known_historical_industry_conflict_and_end_date_are_recorded(self):
        self.assertIn('600882.SH',historical_input_issues(['600882.SH'],'20141231','20261106'))
        self.assertEqual(historical_input_issues(['600882.SH'],'20221230','20261106'),{})
        self.assertEqual(historical_input_issues(['600001.SH'],'20141231','20261106'),{})
        self.assertNotEqual(protocol({},LONG_EXPERIMENT,'20261106'),protocol({},LONG_EXPERIMENT,'20261109'))

    def test_long_history_fetches_predecision_financials_and_estimates(self):
        self.assertEqual(history_bounds(LONG_EXPERIMENT),(2009,'20120101','20141101'))
        self.assertEqual(history_bounds(EXPERIMENT),(2017,'20200101','20221031'))
        self.assertNotEqual(LONG_EXPERIMENT['version'],EXPERIMENT['version'])

    def test_twenty_stocks_separate_from_affordability_and_order_permission(self):
        r,a=pool_inputs();result=research_pool(r,a)
        self.assertEqual(len(result['stocks']),20)
        self.assertEqual(result['target_status'],'待验证')
        self.assertTrue(all(s['price']==100 and 'approved' not in s for s in result['stocks']))

    def test_no_padding_hard_rejects_and_sector_cap(self):
        r,a=pool_inputs()
        for row in r['rows']:row['industry']='同一行业'
        a['assessments'][0]['state']='排除'
        result=research_pool(r,a)
        self.assertEqual(len(result['stocks']),3)
        self.assertNotIn('600000.SH',[s['symbol'] for s in result['stocks']])

    def test_stale_or_incomplete_report_rejected(self):
        r,a=pool_inputs()
        for change in ({'complete':False},{'source':'DEMO'},{'as_of':'20261105'},{'selection_rules':{}}):
            with self.assertRaises(AppError):research_pool({**r,**change},a)

    def test_historical_name_does_not_use_future_rename(self):
        rows=[{'name':'公司','start_date':'20100101','end_date':'20231231','ann_date':'20100101'},
              {'name':'ST公司','start_date':'20240101','end_date':None,'ann_date':'20231231'}]
        self.assertEqual(historical_name(rows,'20221230'),'公司')
        self.assertEqual(historical_name(rows,'20240101'),'ST公司')
        self.assertIsNone(historical_name(rows,'20090101'))
        self.assertIsNone(historical_name(rows+rows,'20221230'))

    def test_registered_protocol_cannot_silently_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'backtests'/EXPERIMENT['version'];folder.mkdir(parents=True)
            (folder/'protocol.json').write_text('{}',encoding='utf-8')
            with self.assertRaises(AppError):prepare_experiment(Path(tmp),'20261106')

    def test_failed_context_retries_and_plain_run_keeps_enriched_sources(self):
        r,a=pool_inputs();r['rows']=r['rows'][:1];a['assessments']=a['assessments'][:1]
        failed={'sources':[],'analysis':'','warnings':['接口失败']}
        success={'sources':[{'url':'https://example.com/disclosure'}],'analysis':'待核实草稿','warnings':[]}
        with tempfile.TemporaryDirectory() as tmp,patch('qmt.charts.update_charts'),patch('qmt.lab.research_context',side_effect=[failed,success]) as fetch:
            root=Path(tmp);atomic_json(automatic_path(root)/'latest.json',a)
            run_lab(root,r,True,lambda *a,**k:None)
            result=run_lab(root,r,True,lambda *a,**k:None)
            self.assertEqual(fetch.call_count,2)
            self.assertIn('https://example.com/disclosure',result['stocks'][0]['source_urls'])
            result=run_lab(root,r,False,lambda *a,**k:None)
            self.assertIn('https://example.com/disclosure',result['stocks'][0]['source_urls'])


if __name__=='__main__':unittest.main()
