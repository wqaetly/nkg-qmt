import html
import json
from pathlib import Path

from .config import ROOT, now_cn
from .context import safe_url


def esc(value):
    return html.escape("—" if value is None else str(value),quote=True)


def number(value, places=2):
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{places}f}"
    except (ValueError,TypeError):
        return "—"


def table(headings, rows):
    return '<div class="table-scroll"><table><thead><tr>'+''.join(f'<th>{esc(x)}</th>' for x in headings)+'</tr></thead><tbody>'+''.join('<tr>'+''.join(f'<td>{esc(c)}</td>' for c in row)+'</tr>' for row in rows)+'</tbody></table></div>'


def render(workspace, ledger, report, status=None):
    status = status or {"ok":True,"message":"最新运行完成"}
    source = report.get("source", "未运行")
    demo = source == "DEMO"
    details = []
    for row in report.get("rows",[]):
        state = row["status"]
        annual = table(["年度","归母利润 / 万元","扣非利润 / 万元","经营现金 / 万元","资本支出 / 万元","简化 FCF / 万元","已实施 DPS / 元","审计"],
            [[a['period'][:4],*[number(float(a[k])/10000) if a.get(k) is not None else '待核实' for k in ('profit','deducted_profit','operating_cash','capex','simple_fcf')],number(a.get('implemented_dps'),3),a.get('audit')] for a in row.get('annual',[])])
        scenarios = row.get("scenarios",[])
        matrix = table(["经营情景","要求税前股息率","预计 DPS / 元","参考价上限 / 元","按当前价情景股息率","价格条件"],
            [[s['scenario'],f"{float(s['required_yield'])*100:.0f}%",number(s['scenario_dividend_per_share'],3),s['reference_price_2dp_floor'],f"{float(s['scenario_yield_at_price'])*100:.2f}%",'满足假设，待决定' if s.get('meets_yield') else '继续观察'] for s in scenarios]) if scenarios else '<p class="empty">尚未形成经确认的盈利和派息情景。历史股息率不会被替换成预测收益。</p>'
        context_path = workspace / "context" / f"{row['symbol']}.json"
        context = json.loads(context_path.read_text(encoding="utf-8")) if context_path.exists() else {}
        sources = ''.join(f'<li><a href="{esc(safe_url(s["url"]))}" rel="noreferrer" target="_blank">[{esc(s["id"])}] {esc(s["title"])}</a><p>{esc(s.get("snippet",""))}</p></li>' for s in context.get("sources",[]) if safe_url(s.get("url")))
        analysis = '<pre class="analysis">'+esc(context.get("analysis") or "未生成 AI 研究草稿。可用 research 命令为该公司补充线索。")+'</pre>'
        warnings = ''.join('<li>'+esc(w)+'</li>' for w in context.get('warnings',[]))
        issues = ''.join('<li>'+esc(issue)+'</li>' for issue in row.get("issues",[]))
        details.append(f'''<details class="candidate" data-search="{esc(row['symbol']+' '+row['name']+' '+row['industry']+' '+state)}" data-status="{esc(state)}">
        <summary><span class="company"><strong>{esc(row['name'])}</strong><small>{esc(row['symbol'])} · {esc(row['industry'])}</small></span>
        <span class="quote">¥ {number(row.get('price'))}<small>{esc(row.get('as_of'))} 收盘</small></span>
        <span class="yield">{number(row.get('historic_yield_pct'))}%<small>历史 TTM 股息率</small></span>
        <span class="pill {'danger' if state=='排除' else 'ready' if state=='可进一步评估' else ''}">{esc(state)}</span><span class="expand">＋</span></summary>
        <div class="detail-body"><div class="detail-grid"><section><h3>核查结论</h3><ul>{issues}</ul><p class="muted">最新报告期 {esc(row.get('latest_period'))} · 取数截至 {esc(row.get('as_of'))}</p></section>
        <section><h3>下一步</h3><p>{esc(row.get('review_note','补充原始公告与情景假设后再评估。'))}</p><p class="muted">研究确认有效期由配置控制；新披露或股本变化将使旧确认失效。不会因价格下跌自动加仓。</p></section></div>
        <h3>近年分红与支付能力</h3>{annual}<p class="muted">金额按原始披露口径；同年度中期与年度派息合计，不含未实施预案。DPS 未统一历史股本口径，不能直接比较增速；空白不是零。金融企业不计算工业 FCF。</p>
        <h3>三种情景 × 股息率参考价</h3>{matrix}
        <details class="research"><summary>联网线索与 AI 研究草稿</summary><p class="muted">{esc(context.get('fetched_at','尚未运行'))} · 搜索摘要未代替原文核查；AI 不改变计划和账本。</p>{analysis}<ul>{warnings}</ul><ol class="sources">{sources}</ol></details></div></details>''')
    accounts = []
    for mode,title in (("paper","模拟账本"),("manual","中信手动交易账本")):
        if not ledger.db.execute('SELECT 1 FROM accounts WHERE mode=?',(mode,)).fetchone():
            accounts.append(f'<section class="panel"><h2>{title}</h2><p class="empty">尚未启用。明确策略现金、单股与行业上限后初始化；不会连接证券账户。</p></section>')
            continue
        data = ledger.summary(mode)
        mark = data['marks'][-1] if data['marks'] else {}
        plans = table(["计划 ID","代码","方向","数量 / 已成交","限价","有效交易日","状态","说明"],
            [[p['id'],p['symbol'],'买入' if p['side']=='buy' else '卖出',f"{p['quantity']} / {p['filled']}",p['limit_price'],p['execute_date'],{'pending':'待核对','partial':'部分成交','filled':'已成交','closed':'已关闭','expired':'已失效'}.get(p['status'],p['status']),p['note']] for p in data['plans']])
        fills = table(["日期","代码","方向","数量","实际/模拟价格","总费用","成交编号"],
            [[f['trade_date'],f['symbol'],f['side'],f['quantity'],f['price'],f['fee'],f['reference']] for f in data['fills']])
        events = table(["日期","代码","金额","说明"],[[e['day'],e['symbol'],e['amount'],e['note']] for e in data['cash_events']])
        holdings = '、'.join(f'{esc(s)} × {q} 股' for s,q in data['positions'].items()) or '当前无持仓'
        accounts.append(f'''<section class="panel"><div class="section-title"><h2>{title}</h2><span class="pill">{'虚拟资金 · 税前分红' if mode=='paper' else '仅记账 · 不会发单'}</span></div>
        <div class="account-stats"><div><small>账本现金</small><strong>¥ {number(data['cash'])}</strong></div><div><small>最近估值总资产</small><strong>¥ {number(mark.get('equity'))}</strong></div><div><small>待到账分红</small><strong>¥ {number(mark.get('receivable'))}</strong></div><div><small>估值日期</small><strong>{esc(mark.get('day'))}</strong></div></div>
        <p>{holdings}</p><p class="muted">单股上限 ¥{number(data['account']['max_position'])} · 行业上限 ¥{number(data['account']['max_sector'])}。已过有效日期的未核对计划不能继续执行；先核对成交及撤单。</p>
        <h3>交易计划</h3>{plans}<details><summary>成交与现金流水</summary>{fills}{events}</details></section>''')
    rows = report.get('rows',[])
    style = (ROOT/'qmt/style.css').read_text(encoding='utf-8')
    page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NKG · 红利研究工作台</title><style>{style}</style></head>
    <body><main><header><a class="brand" href="#">NKG<span> / DIVIDEND LAB</span></a><span class="local">● 本地研究 · 人工执行</span></header>
    <section class="hero"><div><p class="eyebrow">RESEARCH FIRST, EXECUTION SECOND</p><h1>先看分红能否持续，<br>再等价格满足假设。</h1><p>从中证红利成分初筛，保留反对证据与资料缺口。<br>研究、模拟、手动成交，分开记录。</p></div><div class="date-card"><small>行情截至</small><strong>{esc(report.get('as_of','尚未运行'))}</strong><span>{esc(source)} · 成分快照 {esc(report.get('pool_date','—'))}</span><span>生成于 {esc(report.get('generated_at','—'))}</span></div></section>
    <div class="notice {'error' if not status.get('ok') or demo else ''}">{'虚构演示数据，不可用于真实交易。 ' if demo else ''}{esc(status.get('message',''))}</div>
    <section class="metrics"><div><small>候选池覆盖</small><strong>{report.get('covered',0)}<i> / {report.get('pool_size',0)}</i></strong><span>月度指数成分快照，不代表全市场</span></div><div><small>待核实 / 观察</small><strong>{sum(r['status'] in ('待核实','观察') for r in rows)}</strong><span>资料不齐时不生成价格结论</span></div><div><small>排除</small><strong>{sum(r['status']=='排除' for r in rows)}</strong><span>保留具体原因，避免只看成功案例</span></div><div><small>可进一步评估</small><strong>{sum(r['status']=='可进一步评估' for r in rows)}</strong><span>满足研究条件 ≠ 自动买入</span></div></section>
    <section class="panel"><div class="section-title"><h2>01 / 候选与证据</h2><span class="muted">按代码排列，不按股息率排名</span></div><div class="filters"><label>查找公司、代码或行业<input id="search" type="search" placeholder="例如：银行、600…"></label><label>研究状态<select id="status"><option value="">全部状态</option><option>待核实</option><option>观察</option><option>排除</option><option>可进一步评估</option></select></label></div>
    <div id="candidates">{''.join(details) or '<p class="empty">运行 daily 获取候选池与数据。</p>'}</div><p id="no-result" hidden>没有匹配的候选。</p></section>
    {''.join(accounts)}<footer><strong>工作台边界</strong><p>参考价基于正常化盈利与派息假设，税前，不是内在价值。模拟使用下一交易日开盘加不利滑点，含估计佣金与卖出印花税；不保证实际成交。现金分红按登记日资格记应收、派息日入账；个人股息税及送转股需另核对。本项目尚无策略回测或实盘收益证明。</p><p>密钥仅保存在本地配置。研究请求只包含公司公开数据，不上传账本。报告含个人记账信息时请勿公开分享。</p></footer></main>
    <script>const search=document.querySelector('#search'),status=document.querySelector('#status');function filter(){{let n=0;document.querySelectorAll('.candidate').forEach(el=>{{const visible=el.dataset.search.toLowerCase().includes(search.value.toLowerCase())&&(!status.value||el.dataset.status===status.value);el.hidden=!visible;if(visible)n++;}});document.querySelector('#no-result').hidden=n>0;}}search.addEventListener('input',filter);status.addEventListener('change',filter);</script></body></html>'''
    path = workspace/'dashboard.html'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(page,encoding='utf-8')
    tmp.replace(path)
    return path
