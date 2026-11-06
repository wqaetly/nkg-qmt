import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qmt.config import AppError, atomic_json
from qmt.experiment import historical_name, prepare_experiment, EXPERIMENT
from qmt.lab import research_pool, run_lab
from qmt.selection import automatic_path


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


class LabTests(unittest.TestCase):
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
