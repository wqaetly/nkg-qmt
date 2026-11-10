import importlib.util
import unittest
from pathlib import Path

path = Path(__file__).resolve().parents[1] / 'skills/watchlist-review/scripts/render_report.py'
spec = importlib.util.spec_from_file_location('render_report', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReportTests(unittest.TestCase):
    def test_allocation_uses_explicit_denominator_and_rejects_unknown_values(self):
        group = {'title': '<account>', 'currency': 'USD', 'basis': '可见证券',
                 'as_of': '旧快照', 'note': '现金未知',
                 'items': [{'label': 'A', 'value': 25}, {'label': 'B', 'value': 75}]}
        page = module.allocation_chart(group)
        self.assertIn('25.0%', page)
        self.assertIn('75.0%', page)
        self.assertIn('100.00', page)
        self.assertIn('&lt;account&gt;', page)
        self.assertIn('现金未知', page)
        for invalid in (None, -1, float('nan'), float('inf'), True, '25'):
            group['items'][0]['value'] = invalid
            with self.assertRaises(ValueError):
                module.allocation_chart(group)
        group['items'] = [{'label': 'A', 'value': 0}]
        with self.assertRaises(ValueError):
            module.allocation_chart(group)

    def test_financial_bars_share_scale_and_show_actual_difference(self):
        report = {'securities': [], 'visuals': {'cashflow': {
            'period': '季度累计', 'unit': '亿元', 'source': '公告', 'note': '非标准自由现金流',
            'items': [{'label': 'A', 'operating': 10, 'capex': 20},
                      {'label': 'B', 'operating': 5, 'capex': 10}]}}}
        page = module.dashboard(report)
        self.assertIn('width:50.000%', page)
        self.assertIn('width:100.000%', page)
        self.assertIn('width:25.000%', page)
        self.assertIn('差额 -10.00', page)
        self.assertIn('非标准自由现金流', page)
        report['visuals']['cashflow']['items'][0]['operating'] = None
        with self.assertRaises(ValueError):
            module.dashboard(report)

    def test_untrusted_text_and_links(self):
        self.assertEqual(module.esc('<script>"&'), '&lt;script&gt;&quot;&amp;')
        for url in ['javascript:alert(1)', 'http://example.com', 'https://user:secret@example.com']:
            with self.assertRaises(ValueError):
                module.source_link({'title': 'source', 'url': url})
        link = module.source_link({'title': '<img>', 'url': 'https://example.com/?a="x"'})
        self.assertNotIn('<img>', link)
        self.assertIn('&quot;', link)

    def test_missing_or_duplicate_coverage_is_rejected(self):
        for securities in [[], [{'symbol': 'A'}], [{'symbol': 'A'}, {'symbol': 'A'}]]:
            with self.assertRaises(ValueError):
                module.render({'securities': securities, 'expected_symbols': ['A', 'B']})

    def test_full_report_is_local_and_escapes_source_content(self):
        item = dict.fromkeys(('symbol', 'name', 'market', 'action', 'quote', 'change',
                              'evidence_status', 'judgment', 'facts', 'support', 'risk',
                              'condition', 'next_review', 'gap'), '<script>alert(1)</script>')
        item.update(symbol='A', sources=[{'title': '公告', 'url': 'https://example.com/filing'}])
        report = dict.fromkeys(('title', 'subtitle', 'as_of', 'generated_at', 'decision', 'footer'), '示例')
        report.update(status='partial', expected_symbols=['A'], securities=[item],
                      summary=['待核验'], portfolio=[], audit=[])
        page = module.render(report)
        self.assertIn('部分完成', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<script', page)
        self.assertNotIn('src=', page)
        self.assertIn('default-src', page)


if __name__ == '__main__':
    unittest.main()
