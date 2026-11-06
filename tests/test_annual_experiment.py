import copy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from qmt.config import ROOT, AppError, atomic_json, load_config
from qmt.experiment import (ANNUAL_EXPERIMENT, experiment_signals, prepare_annual_experiment,
                            register_experiment, run_experiment)
from qmt.service import read_json


A, B = '600001.SH', '600002.SH'
SESSIONS = ['20221230','20230103','20231229','20240102','20241231','20250102','20251231','20260102']
DATES = [SESSIONS[i] for i in (0,2,4,6)]


def quiet(*args, **kwargs):
    pass


def annual_sources(workspace, pools):
    client=Mock()
    client.calendar.side_effect=lambda start,end:[d for d in SESSIONS if start<=d<=end]
    def prepare(root, end, progress, cfg, experiment):
        day=experiment['selection_day']
        path=register_experiment(root,end,cfg,experiment)
        codes=pools[day]
        stocks=[{'symbol':c,'name':c,'industry':'初次行业-'+day} for c in codes]
        cohort={'selected_on':day,'stocks':stocks,'candidate_count':len(codes),'rejected':[]}
        market={'sessions':[d for d in SESSIONS if d>=day],
                'bars':{c:{d:{'source_day':day} for d in SESSIONS if d>=day} for c in codes},
                'industries':{c:'初次行业-'+day for c in codes},
                'dividends':{c:[{'source_day':day}] for c in codes},
                'errors':{} if codes else {'cohort':'本年没有合格候选'}}
        sources=[{c:{'source_day':day,'symbol':c} for c in codes} for _ in range(4)]
        sectors={c:[{'source_day':day}] for c in codes}
        peers={'600099.SH':[{'l1_code':'801000.SI','in_date':'20000101','out_date':None}]}
        return path,client,cohort,market,*sources[:2],sources[2],sources[3],sectors,peers
    return client,prepare


class AnnualExperimentTests(unittest.TestCase):
    def test_year_end_refresh_keeps_first_entry_data_through_exit_and_reentry(self):
        pools=dict(zip(DATES,([A],[],[A,B],[B])))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);client,prepare=annual_sources(root,pools)
            with patch('qmt.experiment.Tushare',return_value=client),patch('qmt.experiment.prepare_experiment',side_effect=prepare) as children:
                result=prepare_annual_experiment(root,SESSIONS[-1],quiet,load_config())
            path,_,cohort,data,dossiers,names,history,amounts,industries,peers=result
            self.assertEqual([c.args[4]['selection_day'] for c in children.call_args_list],DATES)
            self.assertTrue(all(c.args[1]==SESSIONS[-1] for c in children.call_args_list))
            self.assertEqual([r['symbols'] for r in cohort['rebalances']],[[A],[],[A,B],[B]])
            self.assertEqual(data['universe_membership'][A],[{'start':DATES[0],'end':DATES[1]},
                                                          {'start':DATES[2],'end':DATES[3]}])
            self.assertEqual(data['universe_membership'][B],[{'start':DATES[2],'end':DATES[3]},
                                                          {'start':DATES[3],'end':None}])
            self.assertEqual(list(data['bars'][A]),SESSIONS)
            self.assertEqual(list(data['bars'][B]),SESSIONS[4:])
            self.assertFalse(data['errors'])
            for mapping in (dossiers,names,history,amounts):
                self.assertEqual(mapping[A]['source_day'],DATES[0])
                self.assertEqual(mapping[B]['source_day'],DATES[2])
            self.assertEqual(data['industries'][A],'初次行业-'+DATES[0])
            self.assertEqual(data['dividends'][A],[{'source_day':DATES[0]}])
            self.assertEqual(industries[A],[{'source_day':DATES[0]}])
            self.assertEqual(len(peers['600099.SH']),1)
            self.assertEqual(read_json(path/'market.json'),data)

    def test_future_members_excluded_and_exit_unknown_freezes_until_known(self):
        memberships={A:[{'start':DATES[0],'end':DATES[1]},{'start':DATES[2],'end':DATES[3]}],
                     B:[{'start':DATES[2],'end':None}]}
        stocks=[{'symbol':c,'industry':'测试行业'} for c in (A,B)]
        data={'sessions':SESSIONS,'universe_membership':memberships}
        dossiers={c:{'symbol':c} for c in (A,B)}
        names={c:[{'name':c,'start_date':'20000101','end_date':None,'ann_date':'20000101'}] for c in (A,B)}
        history={c:[{'ts_code':c,'trade_date':d,'pe_ttm':10,'dv_ttm':5} for d in SESSIONS] for c in (A,B)}
        client=Mock();client.calendar.return_value=SESSIONS;client.prices.return_value={}
        calls=[]
        def assess(row,dossier,rules,**kwargs):
            calls.append((row['symbol'],row['as_of']))
            unknown=row['symbol']==A and row['as_of']==DATES[1]
            return {'state':'待数据核实' if unknown else '满足价格条件','reason':'测试数据状态',
                    'estimated_yield':'.06','reference_price':'9','dimensions':{},'evidence_hash':'test'}
        with patch('qmt.experiment.summarize',side_effect=lambda d,b,q:{'symbol':d['symbol'],'as_of':d['as_of']}), \
             patch('qmt.experiment.assess',side_effect=assess), \
             patch('qmt.experiment.industry_at',return_value={'code':'801000.SI','label':'测试行业'}), \
             patch('qmt.experiment.liquidity',return_value={'state':'通过'}):
            signals,audits=experiment_signals(client,{'stocks':stocks},data,dossiers,names,history,
                                            {A:{},B:{}},'.05',{A:[],B:[]},{},quiet,ANNUAL_EXPERIMENT)
        self.assertTrue(all(day>=DATES[2] for code,day in calls if code==B))
        self.assertEqual([c['symbol'] for c in signals[DATES[0]]['candidates']],[A])
        self.assertEqual(signals[DATES[1]],{'candidates':[],'freeze':[A]})
        self.assertEqual(signals['20240102'],{'candidates':[],'freeze':[]})
        self.assertEqual([c['symbol'] for c in signals[DATES[2]]['candidates']],[A,B])
        self.assertEqual([c['symbol'] for c in signals[DATES[3]]['candidates']],[B])
        by_day={m['day']:{a['symbol']:a for a in m['assessments']} for m in audits}
        self.assertNotIn(B,by_day[DATES[1]])
        self.assertFalse(by_day[DATES[1]][A]['active'])
        self.assertEqual(by_day['20240102'][A]['state'],'退出研究池')
        self.assertTrue(by_day[DATES[2]][A]['active'])
        self.assertEqual(by_day[DATES[3]][A]['state'],'退出研究池')
        self.assertNotIn(SESSIONS[-1],signals)

    def test_all_empty_years_remain_blocked_and_still_run_independent_comparisons(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);client,prepare=annual_sources(root,{d:[] for d in DATES})
            with patch('qmt.experiment.Tushare',return_value=client), \
                 patch('qmt.experiment.prepare_experiment',side_effect=prepare), \
                 patch('qmt.comparison.build_comparisons',return_value=[]) as comparisons:
                result=run_experiment(root,SESSIONS[-1],load_config(),quiet,ANNUAL_EXPERIMENT)
            self.assertEqual(result['status'],'blocked')
            self.assertIn('cohort',result['coverage']['input_errors'])
            self.assertEqual(result['coverage']['pool_sizes'],[{'day':d,'stocks':0} for d in DATES])
            self.assertEqual(result['primary_metrics'],{})
            self.assertTrue(all(r['status']=='blocked' and r['metrics']=={} for r in result['runs']))
            self.assertFalse(list((root/'backtests'/ANNUAL_EXPERIMENT['version']).glob('*-result.json')))
            client.prices.assert_not_called()
            comparisons.assert_called_once()

    def test_each_threshold_simulates_one_continuous_account_across_all_years(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);client,prepare=annual_sources(root,dict(zip(DATES,([A],[],[A,B],[B]))))
            replay={'initial_cash':load_config()['paper_cash'],'metrics':{},'trades':[],'skipped':[],
                    'equity_curve':[{'day':d,'equity':load_config()['paper_cash'],'exposure':0} for d in SESSIONS]}
            with patch('qmt.experiment.Tushare',return_value=client), \
                 patch('qmt.experiment.prepare_experiment',side_effect=prepare) as children, \
                 patch('qmt.experiment.experiment_signals',return_value=({},[])), \
                 patch('qmt.backtest.simulate',side_effect=lambda *a,**k:copy.deepcopy(replay)) as simulate, \
                 patch('qmt.comparison.build_comparisons',return_value=[]):
                result=run_experiment(root,SESSIONS[-1],load_config(),quiet,ANNUAL_EXPERIMENT)
            self.assertEqual(children.call_count,4)
            self.assertEqual(simulate.call_count,len(ANNUAL_EXPERIMENT['entry_yields']))
            self.assertEqual(result['status'],'diagnostic')
            first_data=simulate.call_args_list[0].args[0]
            for call in simulate.call_args_list:
                self.assertIs(call.args[0],first_data)
                self.assertEqual(call.args[0]['sessions'],SESSIONS)
                self.assertEqual(call.kwargs['initial_cash'],load_config()['paper_cash'])

    def test_parent_and_each_source_protocol_cannot_change_silently(self):
        cfg=load_config()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);client,prepare=annual_sources(root,{d:[] for d in DATES})
            with patch('qmt.experiment.Tushare',return_value=client),patch('qmt.experiment.prepare_experiment',side_effect=prepare):
                path=prepare_annual_experiment(root,SESSIONS[-1],quiet,cfg)[0]
                before=(path/'protocol.json').read_bytes()
                for changed in ({**ANNUAL_EXPERIMENT,'pool_size':19},{**ANNUAL_EXPERIMENT,'pool_exit':'different'}):
                    with self.assertRaisesRegex(AppError,'协议已变化'):
                        prepare_annual_experiment(root,SESSIONS[-1],quiet,cfg,changed)
                self.assertEqual((path/'protocol.json').read_bytes(),before)
                source=path/'cohorts'/DATES[0]/'protocol.json'
                atomic_json(source,{'unrecognized':'source protocol'})
                with self.assertRaisesRegex(AppError,'协议已变化'):
                    prepare_annual_experiment(root,SESSIONS[-1],quiet,cfg)
                self.assertEqual(read_json(source),{'unrecognized':'source protocol'})

    def test_changed_vendor_calculation_rejects_same_registered_experiment(self):
        vendor=ROOT/'vendor/dividend_stock_research/dividend_price.py'
        original_read=Path.read_bytes
        def changed_source(path):
            content=original_read(path)
            return content+b'\n# mocked calculation change\n' if path.resolve()==vendor.resolve() else content
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=load_config()
            path=register_experiment(root,SESSIONS[-1],cfg,ANNUAL_EXPERIMENT)
            before=(path/'protocol.json').read_bytes()
            with patch.object(Path,'read_bytes',new=changed_source),self.assertRaisesRegex(AppError,'协议已变化'):
                register_experiment(root,SESSIONS[-1],cfg,ANNUAL_EXPERIMENT)
            self.assertEqual((path/'protocol.json').read_bytes(),before)

    def test_invalid_calendar_and_future_end_stop_before_building_any_cohort(self):
        client=Mock()
        with tempfile.TemporaryDirectory() as tmp,patch('qmt.experiment.Tushare',return_value=client), \
             patch('qmt.experiment.prepare_experiment') as prepare:
            for sessions in ([],[SESSIONS[0]],SESSIONS[1:],SESSIONS[:-1]):
                client.calendar.return_value=sessions
                with self.subTest(sessions=sessions),self.assertRaisesRegex(AppError,'起止日必须是交易日'):
                    prepare_annual_experiment(Path(tmp),SESSIONS[-1],quiet,load_config())
            prepare.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp,patch('qmt.experiment.now_cn',return_value=datetime(2026,1,1)), \
             patch('qmt.experiment.Tushare') as factory:
            with self.assertRaisesRegex(AppError,'截止日期'):
                prepare_annual_experiment(Path(tmp),SESSIONS[-1],quiet,load_config())
            factory.assert_not_called()


if __name__=='__main__':unittest.main()
