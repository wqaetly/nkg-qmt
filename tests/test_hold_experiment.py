import copy
import io
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock,patch

from qmt.__main__ import main
from qmt.config import AppError,atomic_json,load_config
from qmt.experiment import ANNUAL_EXPERIMENT,HOLD_EXPERIMENT,HOLD_LONG_EXPERIMENT,experiment_signals,run_experiment


CODE='600001.SH'
DAYS=['20221229','20221230','20230103']


def sources():
    client=Mock();client.calendar.return_value=DAYS
    client.prices.return_value={f'60000{i}.SH':{'pe_ttm':15} for i in range(2,7)}
    stocks=[{'symbol':CODE,'name':'测试公司','industry':'测试行业'}]
    data={'sessions':DAYS,'universe_membership':{CODE:[{'start':DAYS[0],'end':None}]}}
    history={CODE:[{'ts_code':CODE,'trade_date':(datetime(2022,1,1)+timedelta(days=i)).strftime('%Y%m%d'),'pe_ttm':10,'dv_ttm':4} for i in range(300)]}
    history[CODE]+=[{'ts_code':CODE,'trade_date':d,'pe_ttm':10,'dv_ttm':4} for d in DAYS]
    names={CODE:[{'name':'测试公司','start_date':'20000101','end_date':None,'ann_date':'20000101'}]}
    assessment={'state':'等待筛选条件','reason':'仅入场估值未通过','estimated_yield':'.03','reference_price':'10','evidence_hash':'fixture',
                'dimensions':{'dividend':{'state':'等待价格'},'valuation':{'state':'未通过'},'liquidity':{'state':'通过'}}}
    return client,stocks,data,history,names,assessment


class HoldExperimentTests(unittest.TestCase):
    def signals(self,change=None,*,active=True,known_valuation=True,experiment=HOLD_EXPERIMENT):
        client,stocks,data,history,names,a=sources()
        if change:change(a)
        if not active:data['universe_membership'][CODE][0]['end']=DAYS[1]
        if not known_valuation:history[CODE]=history[CODE][-3:]
        peers={code:[] for code in client.prices.return_value}
        with patch('qmt.experiment.summarize',return_value={}),patch('qmt.experiment.assess',side_effect=lambda *args,**kwargs:copy.deepcopy(a)), \
             patch('qmt.experiment.industry_at',return_value={'code':'sector','label':'测试行业'}), \
             patch('qmt.experiment.liquidity',return_value={'state':'通过'}):
            return experiment_signals(client,{'stocks':stocks},data,{CODE:{}},names,history,{CODE:{}},'.05',{CODE:[]},peers,lambda *a,**k:None,experiment)

    def test_retention_boundary_and_entry_overlap_do_not_change_buy_conditions(self):
        signals,audits=self.signals()
        self.assertEqual(signals[DAYS[1]],{'candidates':[],'freeze':[],'retain':[CODE]})
        self.assertTrue(audits[-1]['assessments'][0]['holding_eligible'])
        lower,_=self.signals(lambda a:a.update(estimated_yield='.029999'))
        self.assertEqual(lower[DAYS[1]]['retain'],[])
        entry,_=self.signals(lambda a:a.update(state='满足价格条件',estimated_yield='.06'))
        self.assertEqual(entry[DAYS[1]]['retain'],[CODE])
        self.assertEqual([c['symbol'] for c in entry[DAYS[1]]['candidates']],[CODE])
        baseline,_=self.signals(experiment=ANNUAL_EXPERIMENT)
        self.assertEqual(baseline[DAYS[1]],{'candidates':[],'freeze':[]})

    def test_data_risk_liquidity_and_membership_cannot_be_bypassed_by_retention(self):
        changes=[lambda a:a.update(state='待数据核实'),
                 lambda a:a['dimensions']['dividend'].update(state='排除'),
                 lambda a:a['dimensions']['liquidity'].update(state='未通过')]
        for change in changes:
            signals,_=self.signals(change)
            self.assertEqual(signals[DAYS[1]]['retain'],[])
        signals,_=self.signals(lambda a:a.update(state='待数据核实'))
        self.assertEqual(signals[DAYS[1]]['freeze'],[CODE])
        signals,audits=self.signals(active=False)
        self.assertEqual(signals[DAYS[1]]['retain'],[])
        self.assertEqual(audits[-1]['assessments'][0]['state'],'退出研究池')
        # A known normalized-PE failure must not conceal missing historical valuation inputs.
        signals,_=self.signals(known_valuation=False)
        self.assertEqual(signals[DAYS[1]]['retain'],[])

    def test_invalid_retention_protocol_stops_before_any_data_preparation(self):
        with tempfile.TemporaryDirectory() as tmp,patch('qmt.experiment.prepare_annual_experiment') as prepare:
            for change in ({'retention_yield':'0'},{'retention_yield':'.04'},{'retention_yield':'1'},{'pool_refresh':'none'}):
                with self.subTest(change=change),self.assertRaisesRegex(AppError,'持有缓冲实验'):
                    run_experiment(Path(tmp),DAYS[-1],load_config(),experiment={**HOLD_EXPERIMENT,**change})
            prepare.assert_not_called()

    def test_cli_routes_short_and_long_hold_to_separate_protocols(self):
        with tempfile.TemporaryDirectory() as tmp,patch('qmt.__main__.render'),patch('qmt.experiment.run_experiment') as run, \
             patch('sys.stdout',new_callable=io.StringIO),patch('sys.stderr',new_callable=io.StringIO) as stderr:
            root=Path(tmp);atomic_json(root/'screen.json',{'as_of':'20261106'})
            base=['--workspace',str(root),'backtest']
            self.assertEqual(main(base+['--hold-band']),1)
            self.assertIn('须与 --annual-pool 同用',stderr.getvalue())
            run.assert_not_called()
            for flags,expected in ((['--annual-pool','--hold-band'],HOLD_EXPERIMENT),
                                   (['--annual-pool','--hold-band','--long-history'],HOLD_LONG_EXPERIMENT)):
                main(base+flags)
                self.assertEqual(run.call_args.kwargs['experiment'],expected)
                self.assertEqual(run.call_args.args[1],'20261106')


if __name__=='__main__':unittest.main()
