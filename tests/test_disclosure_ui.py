import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from qmt.config import load_config
from qmt.demo import create_demo
from qmt.ledger import Ledger
from qmt.report import disclosure_diagnostics_panel, render, research_panel, zero_cash_proof_panel


class DisclosureUiTests(unittest.TestCase):
    def diagnostics(self):
        return {'evaluations':36, 'verified_zero_cash':[
            {'symbol':'002191.SZ','period':'20241231','available_on':'20250628',
             'source':'https://example.com/agm?a=1&b=2','note':'股东会批准全年不派现金','count':10}],
            'financial_gaps':[{'symbol':'600001.SH','period':'20231231','field':'st_borr',
                'label':'短期借款','available_on':'20240420','count':3}]}

    def test_saved_zero_and_missing_fields_stay_separate_inside_existing_fold(self):
        diagnostics=self.diagnostics()
        before=copy.deepcopy(diagnostics)
        page=research_panel({}, {'diagnostics':diagnostics})
        fold=page.split('<details class="research-results" id="research-diagnostics">',1)[1].split('</details>',1)[0]
        self.assertIn('已知不满足连续现金分红规则，不是数据缺口，也不是现金入账',fold)
        self.assertIn('缺失报表字段不能补零',fold)
        self.assertIn('002191.SZ</td><td>20241231</td><td>20250628</td><td>10</td>',fold)
        self.assertIn('600001.SH</td><td>20231231</td><td>st_borr</td><td>短期借款</td><td>20240420</td><td>3',fold)
        self.assertIn('href="https://example.com/agm?a=1&amp;b=2"',fold)
        self.assertIn('不等于独立公司数、订单或现金入账次数',fold)
        self.assertNotIn('id="research-diagnostics" open',page)
        self.assertEqual(diagnostics,before)

    def test_legacy_or_empty_diagnostics_do_not_add_empty_sections(self):
        for data in ({}, {'evaluations':5}, {'verified_zero_cash':[], 'financial_gaps':[]}):
            self.assertEqual(disclosure_diagnostics_panel(data),'')
            page=research_panel({}, {'diagnostics':data})
            self.assertNotIn('id="verified-zero-cash"',page)
            self.assertNotIn('id="financial-gaps"',page)
        self.assertEqual(zero_cash_proof_panel([{'period':'20241231','implemented_dps':None}]),'')
        self.assertEqual(zero_cash_proof_panel([{'period':'20241231','zero_cash_proof':{}}]),'')

    def test_untrusted_text_is_escaped_and_unsafe_sources_never_linked(self):
        data=self.diagnostics()
        malicious='<img src=x onerror=alert(1)>'
        for key in ('symbol','period','available_on','note','count'):
            data['verified_zero_cash'][0][key]=malicious
        for key in ('symbol','period','field','label','available_on','count'):
            data['financial_gaps'][0][key]=malicious
        for url in ('javascript:alert(1)','data:text/html,bad','https://user:password@example.com/file'):
            data['verified_zero_cash'][0]['source']=url
            page=disclosure_diagnostics_panel(data)
            proof=zero_cash_proof_panel([{'period':malicious,'zero_cash_proof':{'source':url,'available_on':malicious}}])
            for content in (page,proof):
                self.assertNotIn('<img',content)
                self.assertIn('&lt;img',content)
                self.assertNotIn('href=',content)
                self.assertIn('原文链接不可用',content)

    def test_fixture_render_distinguishes_announced_cash_from_payment_and_preserves_operations(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);cfg=load_config();create_demo(workspace,cfg)
            report=json.loads((workspace/'screen.json').read_text(encoding='utf-8'))
            ledger=Ledger(workspace/'ledger.sqlite3',cfg)
            try:
                original=render(workspace,ledger,report).read_text(encoding='utf-8')
                self.assertIn('已公告现金 DPS / 元',original)
                self.assertNotIn('已实施 DPS / 元',original)
                self.assertNotIn('class="muted zero-cash-proof"',original)
                annual=report['rows'][0]['annual']
                annual[1].update(implemented_dps='0',ordinary_dps='0',estimated_dividend_cash='0',
                    estimated_ordinary_cash='0',dividend_cash_basis=[],zero_cash_proof={
                    'source':'https://example.com/zero?a=1&b=2','available_on':'20250628'})
                annual[0].update(implemented_dps=None,ordinary_dps=None)
                before=copy.deepcopy(report)
                cash_before=copy.deepcopy(ledger.summary('paper')['cash_events'])
                page=render(workspace,ledger,report).read_text(encoding='utf-8')
                self.assertEqual(report,before)
                self.assertEqual(ledger.summary('paper')['cash_events'],cash_before)
            finally:
                ledger.close()
            self.assertIn('实施公告不等于已到账',page)
            self.assertIn('核实全年零不生成支付事件',page)
            self.assertIn('已核实全年零现金分红，不产生支付事件或派息股数依据。',page)
            self.assertIn('所属报告期 20241231：已核实全年现金分红为 0；公告可用日 20250628',page)
            self.assertIn('href="https://example.com/zero?a=1&amp;b=2"',page)
            self.assertIn('<td>0.000</td><td>0.000</td>',page)
            self.assertIn('<td>—</td><td>—</td>',page)
            chart=lambda html:re.search(r'<script type="application/json" id="market-chart-data">(.*?)</script>',html,re.S).group(1)
            self.assertEqual(chart(original),chart(page))
            self.assertIn('每页 5 只',page)


if __name__=='__main__':
    unittest.main()
