import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from qmt.config import AppError, load_config
from qmt.experiment import ANNUAL_EXPERIMENT, holding_freeze_diagnostics, register_experiment, run_experiment
from qmt.research import DISCLOSURES, full_day_suspension
from test_backtest import CODE, DAYS, inputs
from test_freeze_experiment import diagnostic_fixture, position
from test_yield_rank import ranking


class SuspensionExperimentTests(unittest.TestCase):
    def test_unknown_valuation_policy_stops_before_registration_or_fetch(self):
        for value in ('strict',None,True,[]):
            with self.subTest(value=value),tempfile.TemporaryDirectory() as tmp, \
                 patch('qmt.experiment.prepare_annual_experiment') as prepare:
                root=Path(tmp)
                experiment={**position(ranking(ANNUAL_EXPERIMENT)),'suspension_valuation':value}
                for function in (register_experiment,run_experiment):
                    with self.assertRaises(AppError):
                        function(root,DAYS[-1],load_config(),experiment=experiment)
                prepare.assert_not_called()
                self.assertFalse((root/'backtests').exists())

    def test_frozen_holding_diagnostics_use_actual_carry_without_fabricating_a_bar(self):
        args=diagnostic_fixture()
        day,code='20260130','600001.SH'
        event={'day':day,'symbol':code,'quantity':100,'price':'10','price_date':'20260129',
               'market_value':'1000','tradable':False,'method':'verified_suspension_prior_close'}
        del args[3]['bars'][code][day]
        args[0]['valuation_events']=[event]
        original=copy.deepcopy(args)
        actual=holding_freeze_diagnostics(*args,'position')
        row=actual['rows'][0]
        self.assertEqual((row['quantity'],Decimal(row['market_value']),Decimal(row['weight'])),(100,Decimal('1000'),Decimal('.1')))
        self.assertEqual(args,original)
        for changes in ({'quantity':200},{'market_value':'1001'},{'tradable':True},{'method':'unknown'}):
            args[0]['valuation_events']=[{**event,**changes}]
            with self.subTest(changes=changes),self.assertRaises(AppError):
                holding_freeze_diagnostics(*args,'position')
        args[0]['valuation_events']=[event,event]
        with self.assertRaises(AppError):
            holding_freeze_diagnostics(*args,'position')
        args[0]['valuation_events']=[]
        with self.assertRaises(AppError):
            holding_freeze_diagnostics(*args,'position')
        args[0]['valuation_events']=[event]
        args[3]['bars'][code][day]={'close':'10'}
        with self.assertRaisesRegex(AppError,'冲突'):
            holding_freeze_diagnostics(*args,'position')

    def test_complete_experiment_publishes_separate_proofs_and_actual_valuation_events(self):
        data,snapshots,_=inputs()
        snapshots={DAYS[0]:{'candidates':snapshots[DAYS[0]],'freeze':[],'portfolio_freeze':[]},
                   DAYS[2]:{'candidates':[],'freeze':[CODE],'portfolio_freeze':[]}}
        del data['bars'][CODE][DAYS[2]]
        original=copy.deepcopy(data)
        item={**DISCLOSURES['full_day_suspensions'][0],'symbol':CODE,'day':DAYS[2],'available_on':DAYS[2]}
        audits=[{'day':d,'assessments':[{'symbol':CODE,'active':True,'reason':'fixture',
                   'state':'排序候选' if d==DAYS[0] else '待数据核实'}]} for d in snapshots]
        experiment={**position(ranking(ANNUAL_EXPERIMENT)),'selection_day':DAYS[0],
                    'development_end':DAYS[2],'validation_start':DAYS[3]}
        with tempfile.TemporaryDirectory() as tmp,patch.dict(DISCLOSURES,{'full_day_suspensions':[item]}):
            root=Path(tmp);path=root/'backtests'/experiment['version'];path.mkdir(parents=True)
            prepared=(path,None,{'stocks':[{'symbol':CODE}]},data,{}, {}, {}, {}, {}, {})
            with patch('qmt.experiment.prepare_annual_experiment',return_value=prepared), \
                 patch('qmt.experiment.experiment_signals',return_value=(snapshots,audits)), \
                 patch('qmt.comparison.build_comparisons',return_value=[]):
                summary=run_experiment(root,DAYS[-1],{**load_config(),'slippage_bps':0},lambda *a,**k:None,experiment)
            self.assertEqual(summary['status'],'diagnostic')
            replay=json.loads((path/'yield-rank-result.json').read_text('utf8'))
            proof=json.loads((path/'suspensions.json').read_text('utf8'))
            self.assertEqual(proof,{'policy':'verified_full_day_prior_close','proofs':[full_day_suspension(CODE,DAYS[2])]})
            events=replay['valuation_events']
            self.assertEqual(len(events),1)
            self.assertEqual((events[0]['day'],events[0]['price_date'],events[0]['quantity']),(DAYS[2],DAYS[1],500))
            self.assertEqual(Decimal(events[0]['market_value']),Decimal('4000'))
            self.assertEqual(summary['diagnostics']['suspension_valuations'],{'days':1,'company_days':1,'rows':events})
            held=summary['diagnostics']['holding_freezes']['rows'][0]
            self.assertEqual(Decimal(held['market_value']),Decimal(events[0]['market_value']))
            self.assertEqual(Decimal(held['weight']),Decimal(events[0]['weight']))
            self.assertEqual(len(replay['trades']),1)
            self.assertEqual(replay['trades'][0]['trade_date'],DAYS[1])
        self.assertEqual(data,original)
        self.assertNotIn(DAYS[2],data['bars'][CODE])


if __name__=='__main__':
    unittest.main()
