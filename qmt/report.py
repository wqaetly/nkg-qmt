import html
import json
from pathlib import Path

from .config import ROOT, now_cn
from .context import safe_url
from .selection import automatic_path


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


def automatic_panel(data):
    if not data:
        return '<section class="panel"><h2>每日自动模拟</h2><p>运行完整 daily 后启用独立自动模拟账本。</p></section>'
    account=data['account']
    mark=account['marks'][-1] if account['marks'] else {}
    equity=mark.get('equity')
    profit=(float(equity)/float(account['account']['initial_cash'])-1)*100 if equity else 0
    benchmark=data.get('benchmark')
    benchmark_text=f"{float(benchmark['return'])*100:.2f}%" if benchmark else '数据不可用'
    qualified=[a for a in data['assessments'] if a['state']=='满足价格条件']
    visible=[a for a in qualified if a['symbol'] in data.get('focus_symbols',[])][:5]
    notes=data.get('notes',{})
    picks=table(['公司','代码','基准情景股息率','参考上限','本月目标股数','操作/未操作原因'],
        [[a['name'],a['symbol'],f"{float(a['estimated_yield'])*100:.2f}%",a['reference_price'],data.get('target',{}).get('targets',{}).get(a['symbol'],0),notes.get(a['symbol'],a['reason'])] for a in visible])
    screening=table(['公司','代码','机械规则状态','原因'],[[a['name'],a['symbol'],a['state'],a['reason']] for a in data['assessments']])
    return f'''<section class="panel"><div class="section-title"><h2>自动策略模拟 · {esc(data['day'])}</h2><span class="pill">真实行情 · 虚拟资金</span></div>
    <p>已检查 {len(data['assessments'])} 家，{len(qualified)} 家满足买入价格条件。{'休市日：只更新结算与估值，不生成新订单。' if data.get('closed_market') else '每个交易日结算旧订单并检查新订单，月度固定目标股数；允许空仓。'}</p>
    <div class="account-stats"><div><small>自动模拟总资产</small><strong>¥ {number(equity)}</strong></div><div><small>成立以来税前模拟变动</small><strong>{profit:.2f}%</strong></div><div><small>同期红利 ETF 价格复权变动</small><strong>{benchmark_text}</strong></div><div><small>累计模拟成交</small><strong>{len(data.get('chart_fills',[]))} 笔</strong></div></div>
    <p class="muted">起点 {esc(account['account']['start_date'])} 收盘；基准 515180.SH。ETF 比较未扣投资者交易费用；模拟分红税前、未自动再投资。首日都是 0%，不能据此比较收益优劣。{esc(data.get('benchmark_error') or '')}</p>
    <h3>当前观察名单中满足条件的候选及资金分配</h3>{picks if visible else '<p class="empty">当前观察名单无满足全部条件的候选；完整结果见折叠摘要。</p>'}
    <p class="muted">机械盈利代理 = 近三年较低口径利润中位数、最近年度较低利润、TTM 归母、TTM 扣非四者取小；历史派息率是实验假设，不是公司承诺。未自动核实公告全文、业务与特别分红，不能将本结果用于实盘研究确认。</p>
    <details><summary>查看全部初筛原因与规则</summary>{screening}<pre class="analysis">{esc(json.dumps(data['rules'],ensure_ascii=False,indent=2))}</pre></details></section>'''


def chart_panel(workspace, report, automatic):
    chart_path=workspace/'charts.json'
    chart_data=json.loads(chart_path.read_text(encoding='utf-8')) if chart_path.exists() else {}
    stocks=chart_data.get('stocks',{}) if chart_data.get('as_of')==report.get('as_of') else {}
    assessments=automatic.get('assessments',[])
    focus=automatic.get('focus_symbols',[r['symbol'] for r in report.get('rows',[])[:5]])
    preferred=next((a['symbol'] for a in assessments if a['state']=='满足价格条件' and a['symbol'] in focus),None)
    names={r['symbol']:r['name'] for r in report.get('rows',[]) if r['symbol'] in focus}
    codes=focus
    stocks={code:stocks[code] for code in codes if code in stocks}
    if not codes:
        return ''
    options=''.join(f'<option value="{esc(s)}" {"selected" if s==preferred else ""}>{esc(s+" · "+names.get(s,s))}</option>' for s in codes)
    payload={'stocks':stocks,'assessments':assessments,'plans':automatic.get('chart_plans',[]),'fills':automatic.get('chart_fills',[]),'notes':automatic.get('notes',{})}
    encoded=json.dumps(payload,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return f'''<section class="panel" id="chart-panel"><div class="section-title"><h2>股票 K 线与自动模拟操作</h2><span class="muted">红涨绿跌 · 未复权日线</span></div>
    <div class="filters"><label>股票<select id="chart-symbol">{options}</select></label><label>显示范围<select id="chart-range"><option value="30">近 30 根</option><option value="60" selected>近 60 根</option><option value="120">近 120 根</option><option value="10000">全部已取数据</option></select></label></div>
    <p class="chart-legend">○ 信：生成订单的信号日　<span class="buy">▲ 买：模拟买入成交</span>　<span class="sell">▼ 卖：模拟卖出成交</span>　虚线：基准情景参考价（在显示范围内时）</p>
    <div id="stock-chart" class="stock-chart" tabindex="0" aria-label="股票日 K 线；使用左右方向键查看各日详情"></div><p id="chart-tip" class="chart-tip" aria-live="polite"></p>
    <div id="chart-explanation" class="chart-explanation"></div><p class="muted">行情截至 {esc(report.get('as_of'))}。未复权便于核对成交原价，除权除息可能产生缺口；不把它解释为投资损失。未来有效日的订单不画成成交；尚无成交时没有买卖箭头。图中只标独立自动模拟账本的操作。</p>
    <script type="application/json" id="market-chart-data">{encoded}</script></section>'''


def render(workspace, ledger, report, status=None):
    status = status or {"ok":True,"message":"最新运行完成"}
    source = report.get("source", "未运行")
    demo = source == "DEMO"
    auto_file=automatic_path(workspace)/'latest.json'
    automatic=json.loads(auto_file.read_text(encoding='utf-8')) if auto_file.exists() else {}
    if automatic.get('day')!=report.get('as_of') or automatic.get('rules',{}).get('version')!=report.get('strategy_version'):
        automatic={}
    focus=automatic.get('focus_symbols',[r['symbol'] for r in report.get('rows',[])[:5]])
    details = []
    for row in report.get("rows",[]):
        if row['symbol'] not in focus:
            continue
        state = row["status"]
        annual = table(["年度","归母利润 / 万元","扣非利润 / 万元","经营现金 / 万元","资本支出 / 万元","简化 FCF / 万元","已实施 DPS / 元","剔除已知特别后 DPS","审计"],
            [[a['period'][:4],*[number(float(a[k])/10000) if a.get(k) is not None else '待核实' for k in ('profit','deducted_profit','operating_cash','capex','simple_fcf')],number(a.get('implemented_dps'),3),number(a.get('ordinary_dps'),3),a.get('audit')] for a in row.get('annual',[])])
        scenarios = row.get("scenarios",[])
        matrix = table(["经营情景","要求税前股息率","预计 DPS / 元","参考价上限 / 元","按当前价情景股息率","价格条件"],
            [[s['scenario'],f"{float(s['required_yield'])*100:.0f}%",number(s['scenario_dividend_per_share'],3),s['reference_price_2dp_floor'],f"{float(s['scenario_yield_at_price'])*100:.2f}%",'满足假设，待决定' if s.get('meets_yield') else '继续观察'] for s in scenarios]) if scenarios else '<p class="empty">尚未形成经确认的盈利和派息情景。历史股息率不会被替换成预测收益。</p>'
        context_path = workspace / "context" / f"{row['symbol']}.json"
        context = json.loads(context_path.read_text(encoding="utf-8")) if context_path.exists() else {}
        sources = ''.join(f'<li><a href="{esc(safe_url(s["url"]))}" rel="noreferrer" target="_blank">[{esc(s["id"])}] {esc(s["title"])}</a><p>{esc(s.get("snippet",""))}</p></li>' for s in context.get("sources",[]) if safe_url(s.get("url")))
        analysis = '<pre class="analysis">'+esc(context.get("analysis") or "未生成 AI 研究草稿。可用 research 命令为该公司补充线索。")+'</pre>'
        warnings = ''.join('<li>'+esc(w)+'</li>' for w in context.get('warnings',[]))
        issues = ''.join('<li>'+esc(issue)+'</li>' for issue in row.get("issues",[]))
        assessment=next((a for a in automatic.get('assessments',[]) if a['symbol']==row['symbol']),{})
        dimensions=assessment.get('dimensions',{})
        gates=table(['筛选维度','状态','依据'],[[label,dimensions.get(key,{}).get('state'),dimensions.get(key,{}).get('reason')] for key,label in (('valuation','低估值'),('dividend','持续高股息'),('liquidity','流动性'))]) if dimensions else ''
        lot_note=f"当前一手约 ¥{number(float(row['price'])*100)}（未含费用）；资金限制不影响进入研究名单。" if row.get('price') else '缺少价格，无法估算一手金额。'
        corrections=''.join(f'<p>{esc(e["correction_note"])} · <a href="{esc(safe_url(e["correction_source"]))}" target="_blank" rel="noreferrer">公司实施公告</a></p>' for e in row.get('events',[]) if e.get('correction_source'))
        details.append(f'''<details class="candidate" data-search="{esc(row['symbol']+' '+row['name']+' '+row['industry']+' '+state)}" data-status="{esc(state)}">
        <summary><span class="company"><strong>{esc(row['name'])}</strong><small>{esc(row['symbol'])} · {esc(row['industry'])}</small></span>
        <span class="quote">¥ {number(row.get('price'))}<small>{esc(row.get('as_of'))} 收盘</small></span>
        <span class="yield">{number(row.get('historic_yield_pct'))}%<small>历史 TTM 股息率</small></span>
        <span class="pill {'danger' if state=='排除' else 'ready' if state=='可进一步评估' else ''}">{esc(state)}</span><span class="expand">＋</span></summary>
        <div class="detail-body"><button type="button" class="chart-link" data-chart-symbol="{esc(row['symbol'])}">查看 K 线与模拟操作</button><h3>三项筛选 · {esc(assessment.get('state','尚未模拟评估'))}</h3>{gates}<p>{esc(lot_note)}</p><div class="detail-grid"><section><h3>核查结论</h3><ul>{issues}</ul><p class="muted">最新报告期 {esc(row.get('latest_period'))} · 取数截至 {esc(row.get('as_of'))}</p></section>
        <section><h3>下一步</h3><p>{esc(row.get('review_note','补充原始公告与情景假设后再评估。'))}</p><p class="muted">研究确认有效期由配置控制；新披露或股本变化将使旧确认失效。不会因价格下跌自动加仓。</p></section></div>
        <h3>近年分红与支付能力</h3>{annual}{corrections}<p class="muted">金额按原始披露口径；同年度中期与年度派息合计，不含未实施预案。只剔除有原文依据的已知特别分红，其余记录仍需核实。DPS 未统一历史股本口径，不能直接比较增速；空白不是零。金融企业不计算工业 FCF。</p>
        <h3>三种情景 × 股息率参考价</h3>{matrix}
        <details class="research"><summary>联网线索与 AI 研究草稿</summary><p class="muted">{esc(context.get('fetched_at','尚未运行'))} · 搜索摘要未代替原文核查；AI 不改变计划和账本。</p>{analysis}<ul>{warnings}</ul><ol class="sources">{sources}</ol></details></div></details>''')
    accounts = []
    for mode,title in (("automatic","自动策略模拟账本"),("paper","手动指定计划的模拟账本"),("manual","中信手动交易账本")):
        if mode=='automatic' and not automatic:
            continue
        if mode!='automatic' and not ledger.db.execute('SELECT 1 FROM accounts WHERE mode=?',(mode,)).fetchone():
            accounts.append(f'<section class="panel"><h2>{title}</h2><p class="empty">尚未启用。明确策略现金、单股与行业上限后初始化；不会连接证券账户。</p></section>')
            continue
        data = automatic['account'] if mode=='automatic' else ledger.summary(mode)
        mark = data['marks'][-1] if data['marks'] else {}
        plans = table(["计划 ID","代码","方向","数量 / 已成交","限价","有效交易日","状态","说明"],
            [[p['id'],p['symbol'],'买入' if p['side']=='buy' else '卖出',f"{p['quantity']} / {p['filled']}",p['limit_price'],p['execute_date'],{'pending':'待核对','partial':'部分成交','filled':'已成交','closed':'已关闭','expired':'已失效'}.get(p['status'],p['status']),p['note']] for p in data['plans']])
        fills = table(["日期","代码","方向","数量","实际/模拟价格","总费用","成交编号"],
            [[f['trade_date'],f['symbol'],f['side'],f['quantity'],f['price'],f['fee'],f['reference']] for f in data['fills']])
        events = table(["日期","代码","金额","说明"],[[e['day'],e['symbol'],e['amount'],e['note']] for e in data['cash_events']])
        holdings = '、'.join(f'{esc(s)} × {q} 股' for s,q in data['positions'].items()) or '当前无持仓'
        accounts.append(f'''<section class="panel"><div class="section-title"><h2>{title}</h2><span class="pill">{'虚拟资金 · 税前分红' if mode!='manual' else '仅记账 · 不会发单'}</span></div>
        <div class="account-stats"><div><small>账本现金</small><strong>¥ {number(data['cash'])}</strong></div><div><small>最近估值总资产</small><strong>¥ {number(mark.get('equity'))}</strong></div><div><small>待到账分红</small><strong>¥ {number(mark.get('receivable'))}</strong></div><div><small>估值日期</small><strong>{esc(mark.get('day'))}</strong></div></div>
        <p>{holdings}</p><p class="muted">单股上限 ¥{number(data['account']['max_position'])} · 行业上限 ¥{number(data['account']['max_sector'])}。已过有效日期的未核对计划不能继续执行；先核对成交及撤单。</p>
        <h3>交易计划</h3>{plans}<details><summary>成交与现金流水</summary>{fills}{events}</details></section>''')
    rows = report.get('rows',[])
    style = (ROOT/'qmt/style.css').read_text(encoding='utf-8')
    chart_script = (ROOT/'qmt/chart.js').read_text(encoding='utf-8')
    page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NKG · 红利研究工作台</title><style>{style}</style></head>
    <body><main><header><a class="brand" href="#">NKG<span> / DIVIDEND LAB</span></a><span class="local">● 本地研究 · 人工执行</span></header>
    <section class="hero"><div><p class="eyebrow">RESEARCH FIRST, EXECUTION SECOND</p><h1>先看分红能否持续，<br>再等价格满足假设。</h1><p>从沪深主板寻找低估值、持续高股息与强流动性，保留反对证据与资料缺口。<br>研究、模拟、手动成交，分开记录。</p></div><div class="date-card"><small>行情截至</small><strong>{esc(report.get('as_of','尚未运行'))}</strong><span>{esc(source)} · 市场快照 {esc(report.get('pool_date','—'))}</span><span>生成于 {esc(report.get('generated_at','—'))}</span></div></section>
    <div class="notice {'error' if not status.get('ok') or demo else ''}">{'虚构演示数据，不可用于真实交易。 ' if demo else ''}{esc(status.get('message',''))}</div>
    {automatic_panel(automatic) if not demo else ''}{chart_panel(workspace,report,report.get('demo_operations',{}) if demo else automatic)}
    <section class="metrics"><div><small>财报研究覆盖</small><strong>{report.get('covered',0)}<i> / {report.get('pool_size',0)}</i></strong><span>月度指数市场快照，不代表全市场</span></div><div><small>待核实 / 观察</small><strong>{sum(r['status'] in ('待核实','观察') for r in rows)}</strong><span>资料不齐时不生成价格结论</span></div><div><small>排除</small><strong>{sum(r['status']=='排除' for r in rows)}</strong><span>保留具体原因，避免只看成功案例</span></div><div><small>可进一步评估</small><strong>{sum(r['status']=='可进一步评估' for r in rows)}</strong><span>满足研究条件 ≠ 自动买入</span></div></section>
    <section class="panel"><div class="section-title"><h2>重点观察 · 最多 5 只</h2><span class="muted">优先持仓与关注名单，其次满足规则的候选；观察不等于买入</span></div><div class="filters"><label>在观察名单中查找<input id="search" type="search" placeholder="公司、代码或行业"></label><label>研究状态<select id="status"><option value="">全部状态</option><option>待核实</option><option>观察</option><option>排除</option><option>可进一步评估</option></select></label></div>
    <div id="candidates">{''.join(details) or '<p class="empty">运行 daily 获取候选池与数据。</p>'}</div><p id="no-result" hidden>没有匹配的候选。</p></section>
    {''.join(accounts)}<footer><strong>工作台边界</strong><p>参考价基于正常化盈利与派息假设，税前，不是内在价值。模拟使用下一交易日开盘加不利滑点，含估计佣金与卖出印花税；不保证实际成交。现金分红按登记日资格记应收、派息日入账；个人股息税及送转股需另核对。本项目尚无策略回测或实盘收益证明。</p><p>密钥仅保存在本地配置。研究请求只包含公司公开数据，不上传账本。报告含个人记账信息时请勿公开分享。</p></footer></main>
    <script>const search=document.querySelector('#search'),status=document.querySelector('#status');function filter(){{let n=0;document.querySelectorAll('.candidate').forEach(el=>{{const visible=el.dataset.search.toLowerCase().includes(search.value.toLowerCase())&&(!status.value||el.dataset.status===status.value);el.hidden=!visible;if(visible)n++;}});document.querySelector('#no-result').hidden=n>0;}}search.addEventListener('input',filter);status.addEventListener('change',filter);</script><script>{chart_script}</script></body></html>'''
    path = workspace/'dashboard.html'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(page,encoding='utf-8')
    tmp.replace(path)
    return path
