import copy
import unittest

from qmt.report import forecast_diagnostic_panel


class ForecastUiTests(unittest.TestCase):
    def data(self):
        return {
            'version':'dividend-forecast-v1','status':'diagnostic',
            'source_version':'historical-dividend-v19-annual-all','outcome_as_of':'20261106',
            'horizon':'固定年末预测下一完整财政年度；2026 年待观察。',
            'coverage':{'predictions':4,'proxy_scored':3,'verified_ordinary':0,'verified_zero':0,'pending':1},
            'methods':{'conservative_minimum':'保守盈利','ttm':'TTM盈利','last_annual':'最近已知年度延续'},
            'by_year':[
                {'origin':'20221230','target_year':2023,'candidates':3,'proxy_scored':3,
                 'status_counts':{'proxy_total_cash':3},'metrics':{
                     'conservative_minimum':{'count':3,'bias_yield_bp':'-10.125','mae_yield_bp':'20.125'},
                     'ttm':{'count':3,'bias_yield_bp':'0','mae_yield_bp':'0'},
                     'last_annual':{'count':3,'bias_yield_bp':'30','mae_yield_bp':'40'}}},
                {'origin':'20251231','target_year':2026,'candidates':1,'proxy_scored':0,
                 'status_counts':{'pending_year_incomplete':1},'metrics':{
                     method:{'count':0,'bias_yield_bp':0,'mae_yield_bp':0}
                     for method in ('conservative_minimum','ttm','last_annual')}}],
            'limitations':['未证明全年普通分红完整；已查看的历史不是未见数据验证。']}

    def test_proxy_is_separate_from_verified_and_pending_is_not_zero_error(self):
        data=self.data();before=copy.deepcopy(data)
        page=forecast_diagnostic_panel(data)
        self.assertIn('id="forecast-diagnostic"',page)
        self.assertNotIn(' open',page)
        self.assertIn('原文核实全年零派息 0 组；已观察现金派息代理 3 组',page)
        self.assertIn('共 4 组预测，未评分 1 组',page)
        self.assertIn('20221230 → FY2023</td><td>TTM盈利</td><td>3</td><td>0.00</td><td>0.00</td><td>代理对照',page)
        self.assertEqual(page.count('<td>0</td><td>—</td><td>—</td><td>待观察'),3)
        self.assertIn('正值表示高估',page)
        self.assertIn('其余停派 / 零分红待核实样本仍未纳入，不能据此选出更优规则',page)
        self.assertIn('不自动改变交易规则',page)
        self.assertEqual(data,before)

    def test_yearly_coverage_and_pending_reasons_are_aggregated_from_saved_rows(self):
        data=self.data()
        data['by_year'][0].update(candidates=8,status_counts={
            'proxy_total_cash':3,'pending_zero_or_annual_confirmation':2,'pending_stock_actions':3})
        data['by_year'].append({'origin':'20231229','target_year':2024,'candidates':5,'proxy_scored':1,
            'status_counts':{'proxy_total_cash':1,'pending_zero_or_annual_confirmation':2,'pending_stock_actions':2}})
        data['coverage'].update(predictions=14,proxy_scored=4,pending=10)
        page=forecast_diagnostic_panel(data)
        self.assertIn('FY2023：3 / 8；FY2026：0 / 1；FY2024：1 / 5',page)
        self.assertIn('年度 / 零预案待核实 4 组',page)
        self.assertIn('送转待核实 5 组',page)
        self.assertIn('目标年度未完成 1 组',page)
        self.assertNotIn('proxy_total_cash',page)

    def test_verified_zero_cases_have_separate_actual_values_and_safe_sources(self):
        data=self.data()
        data['coverage'].update(predictions=6,verified_ordinary=2,verified_zero=2)
        data['by_year'][0]['status_counts']['verified_zero_cash']=2
        data['verified_zero_cases']=[
            {'symbol':'002030.SZ','name':'零派息反例甲','origin':'20231229','target_year':2024,
             'estimates_dps':{'conservative_minimum':'.125','ttm':'.250','last_annual':'.375'},
             'label':{'observed_cash_dps':'0','source':'https://example.com/annual.pdf','available_on':'20250401','page':30}},
            {'symbol':'600438.SH','name':'<script>反例乙</script>','origin':'20231229','target_year':2024,
             'estimates_dps':{'conservative_minimum':'.5','ttm':'.6','last_annual':'.7'},
             'label':{'observed_cash_dps':'0','source':'javascript:alert(1)','available_on':'20250402','page':40}}]
        before=copy.deepcopy(data)
        page=forecast_diagnostic_panel(data)
        self.assertIn('原文核实全年零派息 2 组；已观察现金派息代理 3 组',page)
        self.assertNotIn('正式普通股息验证',page)
        self.assertIn('原文核实的零派息反例',page)
        self.assertIn('0.125</td><td>0.250</td><td>0.375</td><td>0.000',page)
        self.assertIn('href="https://example.com/annual.pdf"',page)
        self.assertIn('原文 · 20250401 · 第 30 页',page)
        self.assertIn('&lt;script&gt;反例乙&lt;/script&gt;',page)
        self.assertNotIn('javascript:',page)
        self.assertNotIn('<script>',page)
        self.assertNotIn('verified_zero_cash',page)
        self.assertIn('未评分原因：目标年度未完成 1 组',page)
        self.assertIn('零派息反例单列，不混入下方未分类现金派息代理误差',page)
        self.assertIn('TTM盈利</td><td>3</td><td>0.00</td><td>0.00</td><td>代理对照',page)
        self.assertEqual(data,before)

    def test_legacy_without_zero_cases_has_no_zero_case_table(self):
        data=self.data()
        data['coverage'].pop('verified_zero')
        page=forecast_diagnostic_panel(data)
        self.assertIn('原文核实全年零派息 0 组',page)
        self.assertNotIn('原文核实的零派息反例',page)
        self.assertEqual(page.count('<table>'),1)

    def test_missing_record_is_compatible_and_invalid_record_does_not_show_metrics(self):
        self.assertEqual(forecast_diagnostic_panel({}),'')
        for changes in ({'version':'unknown'},{'status':'failed'}):
            page=forecast_diagnostic_panel({**self.data(),**changes})
            self.assertIn('诊断记录不可用',page)
            self.assertNotIn('<table>',page)
            self.assertNotIn('0.00',page)

    def test_labels_and_notes_are_escaped(self):
        data=self.data();bad='<script>alert("x")</script>'
        data['source_version']=bad;data['horizon']=bad;data['limitations']=[bad]
        data['methods']['ttm']=bad
        data['by_year'][0]['origin']=bad
        page=forecast_diagnostic_panel(data)
        self.assertNotIn(bad,page)
        self.assertNotIn('<script>',page)
        self.assertIn('&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;',page)

    def test_missing_and_nonfinite_errors_remain_unknown(self):
        data=self.data()
        data['by_year'][0]['metrics']={'ttm':{'count':3,'bias_yield_bp':'NaN','mae_yield_bp':'Infinity'}}
        data['by_year'][1]['status_counts']={'pending_zero_or_annual_confirmation':1}
        page=forecast_diagnostic_panel(data)
        self.assertIn('TTM盈利</td><td>3</td><td>—</td><td>—</td><td>代理对照',page)
        self.assertEqual(page.count('<td>0</td><td>—</td><td>—</td><td>待核实'),5)
        self.assertNotIn('NaN',page)
        self.assertNotIn('Infinity',page)


if __name__=='__main__':
    unittest.main()
