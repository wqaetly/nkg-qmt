import copy
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from qmt.__main__ import main, parser
from qmt.automatic import assess, dividend_assessment
from qmt.config import AppError, atomic_json, load_config
from qmt.experiment import (ANNUAL_EXPERIMENT, ANNUAL_LONG_EXPERIMENT, FORWARD_CASH_FIELDS,
                            prepare_annual_experiment, protocol, register_experiment, run_experiment)
from qmt.research import summarize
from test_backtest import bar
from test_broad_experiment import DAY, END, broad_client, quiet, signals
from test_yield_rank import ranking


def approved(base=ANNUAL_EXPERIMENT):
    selected=ranking(base)
    return {**selected,'version':selected['version']+'-approved-cash','dividend_policy':'approved_annual_cash'}


class DividendPolicyExperimentTests(unittest.TestCase):
    def test_policy_is_pinned_and_neither_unknown_nor_same_version_change_can_register(self):
        self.assertEqual(ANNUAL_EXPERIMENT['dividend_policy'],'implemented_only')
        self.assertTrue(ANNUAL_EXPERIMENT['version'].startswith('historical-dividend-v33-'))
        cfg=load_config()
        legacy={key:value for key,value in ANNUAL_EXPERIMENT.items() if key!='dividend_policy'}
        self.assertEqual(protocol(cfg,legacy,END)['dividend_policy'],'implemented_only')
        for bad in ('unknown',None,[],'' ):
            with self.subTest(bad=bad),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                with self.assertRaises(AppError):
                    register_experiment(root,END,cfg,{**approved(),'dividend_policy':bad})
                self.assertFalse((root/'backtests').exists())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with self.assertRaises(AppError):
                register_experiment(root,END,cfg,{**ANNUAL_EXPERIMENT,'dividend_policy':'approved_annual_cash'})
            old=ranking(ANNUAL_EXPERIMENT)
            folder=register_experiment(root,END,cfg,old)
            before=(folder/'protocol.json').read_bytes()
            with self.assertRaisesRegex(AppError,'协议已变化'):
                register_experiment(root,END,cfg,{**old,'dividend_policy':'approved_annual_cash'})
            self.assertEqual((folder/'protocol.json').read_bytes(),before)
            self.assertNotEqual(register_experiment(root,END,cfg,approved()),folder)

    def test_cli_requires_full_annual_rank_and_creates_isolated_versions(self):
        with tempfile.TemporaryDirectory() as tmp,patch('qmt.__main__.render'), \
             patch('qmt.experiment.run_experiment') as run,patch('sys.stdout',new_callable=io.StringIO), \
             patch('sys.stderr',new_callable=io.StringIO):
            root=Path(tmp);atomic_json(root/'screen.json',{'as_of':END})
            base=['--workspace',str(root),'backtest']
            for flags in ([],['--annual-pool'],['--annual-pool','--all-candidates'],
                          ['--annual-pool','--all-candidates','--yield-rank','--hold-band']):
                self.assertEqual(main(base+flags+['--approved-annual-cash']),1)
            run.assert_not_called()
            versions=set()
            for long in (False,True):
                for position in (False,True):
                    for basis in ('conservative_minimum','ttm'):
                        flags=['--annual-pool','--all-candidates','--yield-rank','--earnings-basis',basis]
                        if long:flags+=['--long-history']
                        if position:flags+=['--position-freeze']
                        self.assertEqual(main(base+flags),0)
                        control=run.call_args.kwargs['experiment']
                        self.assertEqual(control['dividend_policy'],'implemented_only')
                        self.assertEqual(main(base+flags+['--approved-annual-cash']),0)
                        chosen=run.call_args.kwargs['experiment']
                        self.assertEqual(chosen,{**control,'version':control['version']+'-approved-cash',
                                                 'dividend_policy':'approved_annual_cash'})
                        self.assertTrue(chosen['version'].startswith((ANNUAL_LONG_EXPERIMENT if long else ANNUAL_EXPERIMENT)['version']))
                        versions.add(chosen['version'])
            self.assertEqual(len(versions),8)
        self.assertFalse(hasattr(parser().parse_args(['daily']),'approved_annual_cash'))
        self.assertEqual(parser().parse_args(['forecast-check']).version,'historical-dividend-v33-annual-all-rank-approved-cash-three-year-fcf-equity-limits')

    def test_annual_child_and_monthly_paths_preserve_policy_evidence_without_new_payment_events(self):
        # These values test caller wiring; financial eligibility is independently tested in automatic/research.
        fields={'dividend_policy':'approved_annual_cash',
                'cash_decision':{'period':'20251231','stage':'approved','total_cash':'999999','ordinary_cash':'999999',
                                 'cash_scope':'annual','source':'https://example.com/agm.pdf','available_on':DAY},
                'payout_inputs':['.2','.6','.7'],'payout_uncapped_median':'.6','payout_latest_cap':'.2',
                'cash_coverage_basis':'commitment','fcf_coverage':'1.2','payout':'.2'}
        def summary_call(*args,**kwargs):
            self.assertEqual(kwargs['dividend_policy'],'approved_annual_cash')
            return summarize(*args)
        def quality_call(*args,**kwargs):
            self.assertEqual(kwargs.pop('dividend_policy'),'approved_annual_cash')
            return {**dividend_assessment(*args,**kwargs),**copy.deepcopy(fields)}
        def assessment_call(*args,**kwargs):
            self.assertEqual(kwargs.pop('dividend_policy'),'approved_annual_cash')
            return {**assess(*args,**kwargs),**copy.deepcopy(fields)}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);client,_=broad_client(expensive_prefix=False)
            client.bars.return_value={day:bar() for day in (DAY,END)}
            selected=approved({**ANNUAL_EXPERIMENT,'selection_day':DAY,'validation_start':DAY,'development_end':DAY})
            with patch('qmt.experiment.Tushare',return_value=client), \
                 patch('qmt.experiment.now_cn',return_value=datetime(2026,11,7)), \
                 patch('qmt.experiment.summarize',side_effect=summary_call) as summary_spy, \
                 patch('qmt.experiment.dividend_assessment',side_effect=quality_call) as quality_spy:
                prepared=prepare_annual_experiment(root,END,quiet,load_config(),selected)
            folder=prepared[0]
            child=json.loads((folder/'cohorts'/DAY/'protocol.json').read_text(encoding='utf8'))
            self.assertEqual(child['dividend_policy'],'approved_annual_cash')
            self.assertGreater(summary_spy.call_count,0);self.assertGreater(quality_spy.call_count,0)
            self.assertTrue(all(stock['selection_quality']['cash_decision']==fields['cash_decision'] for stock in prepared[2]['stocks']))
            before=copy.deepcopy(prepared[3]['dividends'])
            with patch('qmt.experiment.summarize',side_effect=summary_call), \
                 patch('qmt.experiment.assess',side_effect=assessment_call):
                snapshots,audits=signals(prepared,'0.05',selected)
            for row in audits[0]['assessments']:
                self.assertEqual({key:row[key] for key in FORWARD_CASH_FIELDS if key in row},fields)
            self.assertEqual(prepared[3]['dividends'],before)
            # An unresolved financial evaluation may carry an explicit null decision.
            audits[0]['assessments'][0]['cash_decision']=None
            audits[0]['assessments'][0].pop('cash_coverage_basis')
            with patch('qmt.experiment.prepare_annual_experiment',return_value=prepared), \
                 patch('qmt.experiment.experiment_signals',return_value=(snapshots,audits)), \
                 patch('qmt.comparison.build_comparisons',return_value=[]):
                result=run_experiment(root,END,load_config(),quiet,selected)
            total=len(audits[0]['assessments'])
            self.assertEqual(result['diagnostics']['cash_estimates'],
                             {'evaluations':total,'stages':{'approved':total-1,'not_recorded':1},
                              'coverage_bases':{'commitment':total-1,'not_recorded':1}})
            self.assertEqual(result['status'],'diagnostic',result['runs'])
            saved=json.loads((folder/'yield-rank-result.json').read_text(encoding='utf8'))
            self.assertEqual(saved['cash_events'],[])
            self.assertTrue(all(row['receivable']=='0' for row in saved['equity_curve']))


if __name__=='__main__':
    unittest.main()
