import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from qmt.config import load_config
from qmt.experiment import ANNUAL_EXPERIMENT, protocol


from qmt import prepared_inputs as adapter


class PreparedInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.workspace=self.root/'workspace';self.end='20261106'
        self.cfg={**load_config(),'paper_cash':'100000','paper_max_position':'5000','paper_max_sector':'20000'}
        self.old={**ANNUAL_EXPERIMENT,'version':'historical-dividend-v33-fixture','cohort_limit':None,
                  'entry_mode':'yield_rank','entry_yields':['0.05'],'dividend_policy':'approved_annual_cash',
                  'cashflow_policy':'three_year_coverage','capital_policy':'equity_ratio'}
        self.new={**self.old,'version':'historical-dividend-v35-fixture-pe-context','valuation_policy':'historical_context'}
        self.source=self.workspace/'backtests'/self.old['version'];self.target=self.workspace/'backtests'/self.new['version']
        self.days=('20221230','20231229');self.pinned={}
        self.raw={'cohort':{'stocks':[{'symbol':'600000.SH'}],'rebalances':[{'day':day} for day in self.days]},
                  'market':{'sessions':list(self.days),'bars':{}},'financials':{'600000.SH':{}},
                  'names':{'600000.SH':[]},'valuation-history':{'600000.SH':[]},
                  'amounts':{'600000.SH':{'20221230':'123456789.123456789'}},
                  'industry-history':{'stocks':{'600000.SH':[]},'peers':{'600001.SH':[]}}}
        for folder,item in [(self.source,self.old),*[(self.source/'cohorts'/day,
                {**self.old,'version':self.old['version']+'/cohorts/'+day,'selection_day':day}) for day in self.days]]:
            folder.mkdir(parents=True)
            for name,value in [('protocol',protocol(self.cfg,item,self.end)),*self.raw.items()]:
                path=folder/(name+'.json');path.write_text(json.dumps(value,ensure_ascii=False,indent=1),encoding='utf-8')
                self.pinned[path.relative_to(self.root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
        for name in ('yield-rank-signals.json','yield-rank-result.json','summary.json'):
            (self.source/name).write_text('{"old_only":true}',encoding='utf-8')

    def prepare(self,**changes):
        args={'workspace':self.workspace,'end':self.end,'progress':lambda *a,**k:None,'cfg':self.cfg,
              'experiment':self.new,'baseline':self.old['version'],'root':self.root,'pinned':self.pinned}
        return adapter.prepare_frozen(**{**args,**changes})

    def snapshot(self):
        return {path.relative_to(self.root).as_posix():path.read_bytes() for path in self.root.rglob('*') if path.is_file()}

    def after_sale_source(self):
        self.old={**self.old,'exit_policy':'known_failure_exit'}
        self.new={**self.old,'version':'historical-dividend-v38-fixture-after-sale-rebalance',
                  'rebalance_policy':'after_confirmed_sale'}
        self.target=self.workspace/'backtests'/self.new['version']
        for path in self.source.rglob('protocol.json'):
            saved=json.loads(path.read_bytes());saved['exit_policy']='known_failure_exit'
            path.write_text(json.dumps(saved),encoding='utf-8')
            self.pinned[path.relative_to(self.root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()

    def test_copies_only_pinned_raw_bytes_restores_decimal_and_registers_annual_protocols(self):
        adapter.register_experiment(self.workspace,self.end,self.cfg,self.new)
        original=self.snapshot();prepared=self.prepare()
        self.assertEqual(prepared[0],self.target)
        self.assertEqual(prepared[7],{'600000.SH':{'20221230':Decimal('123456789.123456789')}})
        self.assertIsInstance(prepared[7]['600000.SH']['20221230'],Decimal)
        self.assertEqual(prepared[8:],(self.raw['industry-history']['stocks'],self.raw['industry-history']['peers']))
        for suffix in (Path('.'),*(Path('cohorts')/day for day in self.days)):
            source=self.source/suffix;target=self.target/suffix
            for name in adapter.RAW:
                self.assertEqual((source/(name+'.json')).read_bytes(),(target/(name+'.json')).read_bytes())
            expected=self.new if suffix==Path('.') else {**self.new,'version':self.new['version']+'/'+suffix.as_posix(),'selection_day':suffix.name}
            self.assertEqual(json.loads((target/'protocol.json').read_text(encoding='utf-8')),protocol(self.cfg,expected,self.end))
            self.assertEqual({p.name for p in target.iterdir() if p.is_file()},
                             {name+'.json' for name in adapter.RAW}|{'protocol.json','registration.json'})
        for name,payload in original.items():self.assertEqual((self.root/name).read_bytes(),payload)
        before=self.snapshot()
        with self.assertRaisesRegex(RuntimeError,'retry refused'):self.prepare()
        self.assertEqual(self.snapshot(),before)

    def test_changed_or_unpinned_last_source_rejects_before_any_write(self):
        path=self.source/'cohorts'/self.days[-1]/'industry-history.json'
        key=path.relative_to(self.root).as_posix()
        for pinned in ({**self.pinned,key:'0'*64},{k:v for k,v in self.pinned.items() if k!=key}):
            with self.subTest(pinned=key in pinned):
                before=self.snapshot()
                with self.assertRaisesRegex(RuntimeError,'changed or unregistered'):self.prepare(pinned=pinned)
                self.assertEqual(self.snapshot(),before);self.assertFalse(self.target.exists())

    def test_changed_account_or_selection_day_rejects_without_output(self):
        for changes in ({'cfg':{**self.cfg,'paper_cash':'200000'}},
                        {'experiment':{**self.new,'selection_day':'20221229'}}):
            with self.subTest(changes=changes):
                before=self.snapshot()
                with self.assertRaisesRegex(RuntimeError,'differs beyond'):self.prepare(**changes)
                self.assertEqual(self.snapshot(),before);self.assertFalse(self.target.exists())

    def test_exit_policy_reuses_exact_annual_inputs_without_valuation_change(self):
        self.new={**self.old,'version':'historical-dividend-v36-fixture-known-failure-exit',
                  'exit_policy':'known_failure_exit'}
        prepared=self.prepare()
        for suffix in (Path('.'),*(Path('cohorts')/day for day in self.days)):
            for name in adapter.RAW:
                self.assertEqual((prepared[0]/suffix/(name+'.json')).read_bytes(),
                                 (self.source/suffix/(name+'.json')).read_bytes())
            registered=json.loads((prepared[0]/suffix/'protocol.json').read_bytes())
            self.assertEqual(registered['exit_policy'],'known_failure_exit')
            self.assertEqual(registered.get('valuation_policy','historical_gate'),'historical_gate')
        self.assertFalse((prepared[0]/'yield-rank-result.json').exists())

    def test_changed_source_exit_or_valuation_policy_rejects_before_output(self):
        for suffix in (Path('.'),Path('cohorts')/self.days[-1]):
            path=self.source/suffix/'protocol.json'; original=path.read_bytes()
            for change in ({'exit_policy':'known_failure_exit'}, {'valuation_policy':'historical_context'}):
                with self.subTest(suffix=suffix,change=change):
                    path.write_text(json.dumps({**json.loads(original),**change}),encoding='utf-8')
                    self.pinned[path.relative_to(self.root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
                    before=self.snapshot()
                    with self.assertRaisesRegex(RuntimeError,'differs'):self.prepare()
                    self.assertEqual(self.snapshot(),before);self.assertFalse(self.target.exists())
            path.write_bytes(original)
            self.pinned[path.relative_to(self.root).as_posix()]=hashlib.sha256(original).hexdigest()

    def test_after_sale_reuses_all_raw_bytes_without_old_signals_or_results(self):
        self.after_sale_source();before=self.snapshot();prepared=self.prepare()
        for suffix in (Path('.'),*(Path('cohorts')/day for day in self.days)):
            for name in adapter.RAW:
                self.assertEqual((prepared[0]/suffix/(name+'.json')).read_bytes(),
                                 (self.source/suffix/(name+'.json')).read_bytes())
            registered=json.loads((prepared[0]/suffix/'protocol.json').read_bytes())
            self.assertEqual(registered['rebalance_policy'],'after_confirmed_sale')
            self.assertEqual(registered['exit_policy'],'known_failure_exit')
            self.assertEqual(registered.get('valuation_policy','historical_gate'),'historical_gate')
        for name,payload in before.items():self.assertEqual((self.root/name).read_bytes(),payload)
        for name in ('yield-rank-signals.json','yield-rank-result.json','summary.json'):
            self.assertFalse((prepared[0]/name).exists())

    def test_after_sale_rejects_nonmonthly_or_different_exit_source_before_writes(self):
        self.after_sale_source()
        for suffix in (Path('.'),Path('cohorts')/self.days[-1]):
            path=self.source/suffix/'protocol.json';original=path.read_bytes()
            for change in ({'rebalance_policy':'after_confirmed_sale'},{'exit_policy':'review_freeze'},
                           {'valuation_policy':'historical_context'}):
                with self.subTest(suffix=suffix,change=change):
                    path.write_text(json.dumps({**json.loads(original),**change}),encoding='utf-8')
                    self.pinned[path.relative_to(self.root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
                    before=self.snapshot()
                    with self.assertRaisesRegex(RuntimeError,'differs'):self.prepare()
                    self.assertEqual(self.snapshot(),before);self.assertFalse(self.target.exists())
            path.write_bytes(original)
            self.pinned[path.relative_to(self.root).as_posix()]=hashlib.sha256(original).hexdigest()

    def test_after_sale_rejects_second_account_or_selection_change(self):
        self.after_sale_source()
        for changes in ({'cfg':{**self.cfg,'paper_max_sector':'30000'}},
                        {'experiment':{**self.new,'selection_day':'20221229'}}):
            with self.subTest(changes=changes):
                before=self.snapshot()
                with self.assertRaisesRegex(RuntimeError,'differs beyond'):self.prepare(**changes)
                self.assertEqual(self.snapshot(),before);self.assertFalse(self.target.exists())

    def test_existing_raw_output_rejects_without_mutation(self):
        self.target.mkdir(parents=True);(self.target/'market.json').write_bytes(b'preserve')
        before=self.snapshot()
        with self.assertRaisesRegex(RuntimeError,'retry refused'):self.prepare()
        self.assertEqual(self.snapshot(),before)

    def test_changed_annual_source_protocol_rejects_before_any_write(self):
        path=self.source/'cohorts'/self.days[-1]/'protocol.json'
        saved=json.loads(path.read_text(encoding='utf-8'));saved['selection_day']='20231228'
        path.write_text(json.dumps(saved),encoding='utf-8')
        self.pinned[path.relative_to(self.root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
        before=self.snapshot()
        with self.assertRaisesRegex(RuntimeError,'annual prepared protocol differs'):self.prepare()
        self.assertEqual(self.snapshot(),before);self.assertFalse(self.target.exists())

    def test_existing_strategy_result_rejects_before_registration_or_copy(self):
        self.target.mkdir(parents=True);(self.target/'yield-rank-result.json').write_bytes(b'preserve')
        before=self.snapshot()
        with self.assertRaises(RuntimeError):self.prepare()
        self.assertEqual(self.snapshot(),before)

    def test_destination_cannot_escape_backtests(self):
        before=self.snapshot()
        with self.assertRaises((RuntimeError,ValueError)):
            self.prepare(experiment={**self.new,'version':'../outside-account'})
        self.assertEqual(self.snapshot(),before)

    def test_conflicting_annual_registration_rejects_before_parent_registration(self):
        folder=self.target/'cohorts'/self.days[-1];folder.mkdir(parents=True)
        (folder/'protocol.json').write_text('{"conflicting":true}',encoding='utf-8')
        before=self.snapshot()
        with self.assertRaises(RuntimeError):self.prepare()
        self.assertEqual(self.snapshot(),before)


if __name__=='__main__':unittest.main()
