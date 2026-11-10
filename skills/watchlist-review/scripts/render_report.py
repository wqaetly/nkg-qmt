"""Render an agent-authored private research report; never generate trading signals."""
import argparse
import html
import json
import math
from pathlib import Path
from urllib.parse import urlsplit


def esc(value):
    return html.escape(str(value), quote=True)


def source_link(source):
    url = source['url']
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        raise ValueError('来源必须使用不含凭据的 HTTPS 链接')
    return f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(source["title"])}</a>'


COLORS = ('#177e89', '#e9a23b', '#7070b5', '#d06a58', '#70a288', '#a18a73', '#8eabc1')
GROUPS = {'priority': ('优先复核', '#b8523e'), 'wait': ('等待验证', '#a47724'), 'hold': ('持有观察', '#13776b')}


def amount(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('图表数值必须是有限非负数；缺失不能填零')
    return value


def allocation_chart(group):
    items = group['items']
    values = [amount(item['value']) for item in items]
    total = sum(values)
    if not items or not math.isfinite(total) or total <= 0:
        raise ValueError('持仓分布必须有正数合计')
    parts = [f'<article class="allocation"><h3>{esc(group["title"])}</h3>',
             f'<p class="muted">{esc(group["currency"])} · {esc(group["basis"])}<br>{esc(group["as_of"])}</p>',
             '<div class="distribution"><svg viewBox="0 0 140 140" role="img" aria-label="持仓市值占比，逐项数值见右侧图例"><circle cx="70" cy="70" r="54" fill="none" stroke="#edf2f2" stroke-width="19"/>']
    offset = 0
    for index, value in enumerate(values):
        share = value / total * 100
        parts.append(f'<circle cx="70" cy="70" r="54" fill="none" stroke="{COLORS[index % len(COLORS)]}" stroke-width="19" pathLength="100" stroke-dasharray="{share:.6f} {100-share:.6f}" stroke-dashoffset="{-offset:.6f}" transform="rotate(-90 70 70)"/>')
        offset += share
    parts.append(f'<text x="70" y="66" text-anchor="middle" class="ring-value">{len(items)}</text><text x="70" y="85" text-anchor="middle" class="ring-label">项资产</text></svg><div class="legend">')
    for index, (item, value) in enumerate(zip(items, values)):
        parts.append(f'<div><span class="dot" style="background:{COLORS[index % len(COLORS)]}"></span><span>{esc(item["label"])}</span><b>{value/total:.1%}</b><small>{value:,.2f} {esc(group["currency"])}</small></div>')
    parts.append(f'</div></div><p class="chart-total">分母合计 <b>{total:,.2f}</b> {esc(group["currency"])}</p><p class="muted">{esc(group["note"])}</p></article>')
    return ''.join(parts)


def dashboard(report):
    visuals = report.get('visuals', {})
    parts = ['<section id="overview"><h2>研究优先级 <small>分组是研究意见，不是自动交易指令</small></h2><div class="action-board">']
    assigned = set()
    for key, (label, color) in GROUPS.items():
        entries = [(i, item) for i, item in enumerate(report['securities']) if item.get('review_group') == key]
        assigned.update(i for i, _ in entries)
        parts.append(f'<article style="border-top:4px solid {color}"><span class="board-count">{len(entries):02d}</span><h3>{label}</h3><div class="chips">')
        parts.extend(f'<a href="#security-{i}">{esc(item["name"])}</a>' for i, item in entries)
        parts.append('</div></article>')
    if len(assigned) != len(report['securities']):
        parts.append('<p class="muted">部分标的未分组，请看逐项建议；未分组不代表低风险。</p>')
    parts.append('</div><h2>资金分布 <small>各币种独立展示，不合并为总资产饼图</small></h2><div class="allocation-grid">')
    parts.extend(allocation_chart(group) for group in visuals.get('allocations', []))
    if not visuals.get('allocations'):
        parts.append('<p class="empty">持仓分布数据未提供，不能以零值代替。</p>')
    parts.append('</div>')
    cashflow = visuals.get('cashflow')
    if cashflow and cashflow['items']:
        values = [(amount(row['operating']), amount(row['capex'])) for row in cashflow['items']]
        maximum = max(max(pair) for pair in values) or 1
        parts.append(f'<article class="cashflow"><h2>现金流与资本投入</h2><p class="muted">{esc(cashflow["period"])} · {esc(cashflow["unit"])} · {esc(cashflow["source"])}</p><div class="key"><span>● 经营现金流</span><span>● 购建长期资产支出</span></div>')
        for row, (operating, capex) in zip(cashflow['items'], values):
            parts.append(f'<div class="cash-row"><b>{esc(row["label"])}</b><div class="paired-bars"><div><i style="width:{operating/maximum*100:.3f}%"></i><span>{operating:.2f}</span></div><div><i style="width:{capex/maximum*100:.3f}%"></i><span>{capex:.2f}</span></div></div><span class="difference {"negative" if operating < capex else "positive"}">差额 {operating-capex:+.2f}</span></div>')
        parts.append(f'<p class="muted">{esc(cashflow["note"])}</p></article>')
    parts.append('<h2>下一步看什么 <small>已核实事件与待定事项</small></h2><div class="timeline">')
    for event in visuals.get('events', []):
        parts.append(f'<article><time>{esc(event["date"])}</time><h3>{esc(event["label"])}</h3><p>{esc(event["detail"])}</p>{source_link(event["source"])}</article>')
    if not visuals.get('events'):
        parts.append('<p class="empty">暂无已核实的后续事件日期。</p>')
    parts.append('</div></section>')
    return ''.join(parts)


def render(report):
    entries = report['securities']
    symbols = [entry['symbol'] for entry in entries]
    if not symbols or len(set(symbols)) != len(symbols) or set(symbols) != set(report['expected_symbols']):
        raise ValueError('报告必须完整覆盖研究名单且不能重复')
    if report['status'] not in ('complete', 'partial'):
        raise ValueError('未知完成状态')
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta name="referrer" content="no-referrer">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
             f'<title>{esc(report["title"])}</title><style>' + CSS + '</style><body><main>',
             '<header><span class="eyebrow">NKG / AI RESEARCH · 私人研究报告</span>',
             f'<h1>{esc(report["title"])}</h1><p>{esc(report["subtitle"])}</p>',
             f'<p class="muted">信息截止 {esc(report["as_of"])} · 生成 {esc(report["generated_at"])}</p></header>',
             '<section class="metrics">',
             f'<div><strong>{len(entries)}</strong><span>逐项覆盖</span></div>',
             f'<div><strong>{"已完成" if report["status"] == "complete" else "部分完成"}</strong><span>本次流程状态</span></div>',
             '<div><strong>中长期</strong><span>数月至更久</span></div>',
             '<div><strong>手动复核</strong><span>交易由你决定</span></div></section>',
             '<section class="lead"><h2>这次最值得做什么</h2>',
             f'<p class="decision">{esc(report["decision"])}</p><details><summary>展开判断依据</summary><ul>']
    parts.extend(f'<li>{esc(item)}</li>' for item in report['summary'])
    parts.append('</ul></details></section><nav aria-label="报告目录"><a href="#overview">图形总览</a><a href="#holdings">逐项建议</a><a href="#portfolio">组合风险</a><a href="#audit">证据与缺口</a></nav>')
    parts.append(dashboard(report))
    parts.append('<section id="holdings"><h2>全部持仓 · 首次基线 / 后续变化</h2><div class="table"><table><thead><tr><th>证券</th><th>本次建议</th><th>价格与时间</th><th>结论变化</th></tr></thead><tbody>')
    for index, item in enumerate(entries):
        parts.append(f'<tr><td><a href="#security-{index}">{esc(item["name"])}</a><small>{esc(item["symbol"])} · {esc(item["market"])}</small></td><td>{esc(item["action"])}</td><td>{esc(item["quote"])}</td><td>{esc(item["change"])}</td></tr>')
    parts.append('</tbody></table></div><div class="cards">')
    for index, item in enumerate(entries):
        parts.append(f'<article id="security-{index}"><span class="tag">{esc(item["action"])}</span><h3>{esc(item["name"])}</h3><p class="muted">{esc(item["symbol"])} · {esc(item["evidence_status"])}</p><p>{esc(item["judgment"])}</p><details><summary>展开事实、反证与行动条件</summary><dl>')
        for key, label in [('facts', '已核对的事实'), ('support', '支持理由'), ('risk', '反对理由 / 失效点'), ('condition', '什么条件会改变建议'), ('next_review', '下次复核'), ('gap', '仍缺什么')]:
            parts.append(f'<dt>{label}</dt><dd>{esc(item[key])}</dd>')
        parts.append('</dl><h4>来源与核验口径</h4><ul>')
        parts.extend(f'<li>{source_link(source)}</li>' for source in item['sources'])
        parts.append('</ul></details></article>')
    parts.append('</div></section><section id="portfolio"><h2>组合风险 · 仅在本机计算</h2>')
    for group in report['portfolio']:
        parts.append(f'<details class="disclosure"><summary>{esc(group["title"])}</summary><p>{esc(group["text"])}</p></details>')
    parts.append('</section><section id="audit"><h2>证据、研究状态与待核验事项</h2>')
    for item in report['audit']:
        parts.append(f'<details class="disclosure" {"open" if "Gemini" in item["title"] else ""}><summary>{esc(item["title"])}</summary><p>{esc(item["text"])}</p></details>')
    parts.append(f'</section><footer>{esc(report["footer"])}</footer></main></body></html>')
    return '\n'.join(parts)


CSS = '''
:root{color-scheme:light;--ink:#162d37;--muted:#596b73;--line:#dce5e5;--accent:#13776b}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{overflow-wrap:anywhere;margin:0;background:#f4f7f6;color:var(--ink);font:16px/1.75 "Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:1280px;margin:auto;padding:30px 28px}header{padding-bottom:8px}.eyebrow{color:var(--accent);letter-spacing:.14em;font-size:12px;font-weight:700}h1{font-size:38px;line-height:1.3;margin:14px 0}h2{font-size:23px;margin:28px 0 18px}h3{font-size:20px;margin:8px 0}p{margin:8px 0}.muted,small{color:var(--muted);font-size:13px}small{display:block}a{color:#086b66;text-underline-offset:4px;overflow-wrap:anywhere}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);background:#fff;border:1px solid var(--line);border-radius:16px;margin:12px 0 24px}.metrics div{padding:20px 24px}.metrics strong{display:block;font-size:25px}.metrics span{color:var(--muted);font-size:13px}.lead{background:#e6f2ef;padding:24px 30px;border-radius:16px;border-left:5px solid var(--accent)}.lead h2{margin:0 0 12px}.decision{font-size:21px;font-weight:600}li{margin:8px 0}nav{display:flex;gap:28px;padding:24px 0;border-bottom:1px solid var(--line)}nav a{text-decoration:none;font-weight:600}
.table{overflow-x:auto;background:white;border:1px solid var(--line);border-radius:14px}table{border-collapse:collapse;width:100%;min-width:660px;text-align:left}th,td{padding:15px 18px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:13px;background:#edf3f2}td{font-size:14px}.cards{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:24px}article{background:#fff;border:1px solid var(--line);border-radius:14px;padding:24px;margin-bottom:16px;break-inside:avoid}.cards article{margin:0;scroll-margin-top:20px}.tag{display:inline-block;color:#86631a;background:#faf1d9;border-radius:20px;padding:3px 12px;font-size:12px;font-weight:600}dt{font-weight:650;color:#34515c;font-size:13px;margin-top:16px}dd{margin:4px 0;font-size:14px}details{border-top:1px solid var(--line);padding-top:12px;margin-top:20px;font-size:13px}summary{cursor:pointer;color:var(--accent)}footer{font-size:13px;color:var(--muted);border-top:1px solid var(--line);padding-top:24px;margin-top:30px}
@media(max-width:720px){main{padding:28px 16px}h1{font-size:28px}.metrics{grid-template-columns:1fr 1fr}.metrics div{padding:16px}.cards{grid-template-columns:1fr}.lead{padding:20px}nav{gap:18px;font-size:14px}}
@media print{body{background:white}main{max-width:none;padding:0}.cards{display:block}nav{display:none}a{color:inherit}details{display:block}article{border-radius:0}}
.action-board{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.board-count{float:right;font-size:34px;line-height:1.3;color:#5e737b}.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}.chips a{font-size:13px;text-decoration:none;border:1px solid #dce5e5;padding:4px 10px;border-radius:20px}.chips a:hover{background:#e6f2ef}.allocation-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.allocation-grid article:last-child:nth-child(odd){grid-column:1/-1}.distribution{display:flex;align-items:center;gap:22px;margin:20px 0}.distribution svg{width:145px;flex-shrink:0}.ring-value{fill:#162d37;font-size:30px;font-weight:700}.ring-label{fill:#596b73;font-size:10px}.legend{flex:1;min-width:0}.legend>div{display:grid;grid-template-columns:10px 1fr auto;gap:1px 8px;margin:10px 0;font-size:13px;align-items:center}.legend small{grid-column:2/4;font-size:11px}.dot{width:8px;height:8px;border-radius:50%}.chart-total{border-top:1px solid #dce5e5;padding-top:12px;font-size:13px}.cashflow{margin-top:8px}.cashflow h2{margin-top:0}.key{display:flex;gap:24px;font-size:12px;margin:16px 0}.key span:first-child{color:#177e89}.key span:last-child{color:#a47724}.cash-row{display:grid;grid-template-columns:85px 1fr 125px;align-items:center;gap:18px;margin:24px 0;font-size:13px}.paired-bars>div{height:24px;display:flex;align-items:center;gap:8px;padding-right:45px}.paired-bars i{display:block;min-width:0;height:12px;background:#177e89;border-radius:3px}.paired-bars>div:last-child i{background:#e9a23b}.paired-bars span{white-space:nowrap;font-size:12px}.difference{font-size:12px;border-radius:6px;padding:6px 8px;background:#e6f2ef}.negative{color:#a34a3b;background:#faebe6}.positive{color:#13776b}.timeline{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.timeline article{border-top:3px solid #177e89;padding:18px}.timeline time{color:#13776b;font-size:19px;font-weight:700}.timeline h3{font-size:16px}.timeline p,.timeline a{font-size:12px}.disclosure{background:white;border:1px solid #dce5e5;border-radius:10px;padding:16px 22px;margin:10px 0}.disclosure summary{font-weight:600}.empty{color:#596b73;background:#edf2f2;padding:20px}.cards article:target{outline:3px solid #177e89}a:focus-visible,summary:focus-visible{outline:3px solid #177e89;outline-offset:3px}h2 small{font-weight:400;margin-top:4px}
@media(max-width:850px){.allocation-grid{grid-template-columns:1fr}.timeline{grid-template-columns:1fr 1fr}.action-board{gap:10px}.action-board article{padding:16px}.board-count{float:none;font-size:28px}.action-board h3{font-size:17px}}
@media(max-width:520px){.action-board{grid-template-columns:1fr}.board-count{float:right}.distribution{gap:14px}.distribution svg{width:110px}.allocation{padding:18px}.cash-row{grid-template-columns:65px 1fr;gap:10px}.difference{grid-column:2;justify-self:start}.timeline{grid-template-columns:1fr 1fr}nav{flex-wrap:wrap}.cashflow{padding:18px}.key{gap:12px}}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path, help='Agent 已完成核验的 report.json')
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding='utf-8-sig'))
    output = args.report.with_suffix('.html')
    page = render(report)
    output.write_text(page, encoding='utf-8')
    print(output.resolve())


if __name__ == '__main__':
    main()
