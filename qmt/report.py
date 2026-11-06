import html
import json
import math
import re
from collections import Counter
from pathlib import Path

from .config import ROOT, now_cn
from .context import safe_url
from .selection import automatic_path


EXPERIMENT_STATES={'diagnostic':'仅诊断，待验证','running':'检验中','pending':'待运行','failed':'检验未完成','blocked':'输入待核实','complete':'历史实验完成'}


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


def optional_report(path):
    if not path.exists():
        return {}
    try:
        result=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(result,dict):
            raise ValueError()
        return result
    except (OSError,ValueError):
        return {'status':'数据不可用','message':'研究记录格式无效，请重新运行研究命令；未用其他结果替代。'}


def percent(value):
    try:
        numeric=float(value)
        return number(numeric*100)+'%' if math.isfinite(numeric) else '—'
    except (ValueError,TypeError,OverflowError):
        return '—'


def experiment_history_panel(workspace):
    versions={}
    for path in sorted((workspace/'backtests').glob('*/summary.json')):
        summary=optional_report(path)
        protocol=optional_report(path.with_name('protocol.json')) or summary.get('protocol',{})
        version=protocol.get('version',path.parent.name)
        primary=next((run for run in summary.get('runs',[])
                      if run.get('label') in ('5%股息门槛 · 全期','税前股息门槛 5%')), {})
        state=summary.get('status') or primary.get('status') or 'pending'
        metrics=summary.get('primary_metrics',primary.get('metrics',{}))
        if state=='blocked' or primary.get('status')=='blocked':
            state='blocked'
            metrics={}
        coverage=summary.get('coverage',{})
        cash=summary.get('initial_cash',summary.get('account',{}).get('initial_cash',protocol.get('account_config',{}).get('paper_cash')))
        period=f"¥ {number(cash)} · {coverage.get('start',protocol.get('selection_day','—'))}—{coverage.get('end',summary.get('as_of',protocol.get('replay_end','—')))}"
        cohort=f"{version} · 首次建池 {protocol.get('selection_day','—')} · 累计 {coverage.get('stocks','—')} 只"
        versions[version]=[cohort,period,EXPERIMENT_STATES.get(state,state),percent(metrics.get('cagr')),
                           percent(metrics.get('average_exposure')),number(metrics.get('costs'))]
    if not versions:
        return ''
    rows=table(['实验版本 / 首次建池 / 累计股票数','本金 / 回放区间','状态','年化收益','平均仓位','费用 / 元'],versions.values())
    return f'''<details class="research-results" id="experiment-history"><summary>历次实验对照 · {len(versions)} 个版本</summary>
    <p class="muted">仅列本工作区保存的各版本 5% 门槛主方案全期结果；保留旧实验，未混入其他本金工作区。输入阻塞时指标为 —，不是 0% 收益。建池日期、本金和区间不同的结果不能直接视作策略改进。</p>{rows}</details>'''


def comparison_chart(backtest):
    series=backtest.get('comparison_series',[])
    if not series:
        return '<p class="muted">比较曲线待取数，缺少数据时不绘制替代走势。</p>'
    missing_strategy='' if any(line.get('kind')=='strategy' for line in series) else '<p class="notice">本区间策略结果尚不可用。现金与 ETF 只作参照，不能把现金线当作策略收益或已通过验证。</p>'
    encoded=json.dumps(series,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return f'''<section class="comparison-panel"><h3>历史资产走势 · 起点 = 1</h3>{missing_strategy}<div id="comparison-legend" class="comparison-legend" aria-label="显示比较曲线"></div>
    <div id="comparison-chart" class="comparison-chart" tabindex="0" aria-label="历史资产比较；左右方向键查看日期"></div><p id="comparison-tip" class="chart-tip" aria-live="polite"></p>
    <p class="muted">策略净资产、ETF 复权价格和现金分别计算；ETF 不是可成交账本，费用及分红处理口径可能不同。图形只表示历史记录，不外推目标收益。</p>
    <script type="application/json" id="comparison-data">{encoded}</script></section>'''


def coverage_panel(coverage):
    if not coverage:
        return ''
    counts=table(['开始日期','结束日期','交易日','历史股票数','原始行情条数','估值条数'],[
        [coverage.get(key) for key in ('start','end','sessions','stocks','price_rows','valuation_rows')]])
    errors=coverage.get('input_errors',{})
    issues=table(['代码','输入疑点'],list(errors.items())) if errors else '<p class="muted">本次未记录输入错误；这不代表公司研究或策略收益已获验证。</p>'
    return f'''<details class="research-results" id="data-coverage"><summary>历史数据覆盖与取数质量 · 输入疑点 {len(errors)} 只</summary>{counts}
    <p class="muted">输入疑点表示数据或历史口径需要复核，不是股票亏损。记录条数只说明取得的数据量，不能代替逐项质量核查。没有策略曲线不代表现金策略已验证。</p>{issues}</details>'''


def period_diagnostics_panel(data):
    annual=data.get('annual',[])
    rolling=data.get('rolling_three_year',{})
    rolling_keys=('min_cagr','median_cagr','max_cagr','positive_fraction')
    has_rolling=(isinstance(rolling.get('windows'),int) and rolling['windows']>0
                 and all(isinstance(rolling.get(key),(int,float)) and math.isfinite(rolling[key]) for key in rolling_keys)
                 and rolling['min_cagr']<=rolling['median_cagr']<=rolling['max_cagr'] and 0<=rolling['positive_fraction']<=1)
    if not annual and not has_rolling:
        return ''
    yearly=table(['年度 / 实际区间','区间收益','最大回撤','平均仓位','成交笔数','费用 / 元'],[
        [f"{item['year']} · {item['start']}—{item['end']}",percent(item.get('metrics',{}).get('total_return')),
         percent(item.get('metrics',{}).get('max_drawdown')),percent(item.get('metrics',{}).get('average_exposure')),
         item.get('metrics',{}).get('trade_count'),number(item.get('metrics',{}).get('costs'))] for item in annual]) if annual else ''
    summary=table(['完整三年窗口数','最低年化','中位年化','最高年化','正收益窗口占比'],[
        [rolling['windows'],*[percent(rolling[key]) for key in rolling_keys]]]) if has_rolling else ''
    return f'''<details class="research-results" id="period-diagnostics"><summary>逐年收益与滚动三年</summary><p>{esc(data.get('label',''))}</p>
    {yearly}{'<p class="muted">年度表按列出的实际起止日计算区间收益；最后一年可能尚未结束，不能当作完整年度收益。</p>' if annual else ''}
    {summary if has_rolling else ''}{'<p class="muted">滚动三年统计汇总同一条历史资产曲线的不同窗口。重叠窗口不是独立样本，正收益占比不是未来盈利概率。</p>' if has_rolling else ''}</details>'''


def data_warnings_panel(warnings):
    if not warnings:
        return ''
    items=''.join(f'<li class="analysis">报告期 {esc(item.get("period"))} · 公告日 {esc(item.get("ann_date"))} · {esc(item.get("api"))}.{esc(item.get("field"))}<br>接口原始值：{esc(" / ".join(str(value) for value in item.get("values",[])))}</li>' for item in warnings)
    return f'''<div class="data-warnings"><p><strong>未参与计算的股数冲突 · 原文待核实</strong></p><ul>{items}</ul>
    <p class="muted">该季度股数字段未参与本次数值计算；这里只记录接口差异，尚未核对原文。年报股数及其他字段冲突仍按阻断处理。</p></div>'''


def research_panel(research, backtest, chart_symbols=(), history=''):
    if not research and not backtest and not history:
        return ''
    stocks=research.get('stocks',[])
    rows=[]
    for stock in stocks:
        code=stock['symbol']
        dimensions=[]
        evidence=[]
        for key in ('valuation','liquidity'):
            item=stock.get(key,{})
            dimensions.append(item.get('state','待核实') if isinstance(item,dict) else item)
            if isinstance(item,dict) and item.get('reason'):
                evidence.append(f'<p>{"估值" if key=="valuation" else "流动性"}：{esc(item["reason"])}</p>')
        sources=' '.join(f'<a href="{esc(safe_url(url))}" target="_blank" rel="noreferrer">来源 {i}</a>'
                        for i,url in enumerate(stock.get('source_urls',[]),1) if safe_url(url))
        button=(f'<button class="chart-link" type="button" data-chart-symbol="{esc(code)}">K 线</button>'
                if code in chart_symbols else '<span class="muted">K 线待取数</span>')
        search=' '.join(str(stock.get(k,'')) for k in ('symbol','name','industry','state','reason'))
        rows.append(f'''<tr class="research-stock" data-search="{esc(search)}"><td><strong>{esc(stock.get('name',code))}</strong><small>{esc(code)} · {esc(stock.get('industry'))}</small></td>
        <td>{esc(dimensions[0])}</td><td>{esc(dimensions[1])}</td><td>{percent(stock.get('estimated_yield'))}</td><td>{esc(stock.get('state','待核实'))}</td>
        <td>{button}<details><summary>研究依据</summary><p>{esc(stock.get('reason'))}</p>{''.join(evidence)}{sources}</details></td></tr>''')
    runs=backtest.get('runs',[])
    comparisons=table(['实验 / 区间','年化收益','总收益','最大回撤','平均仓位','成交笔数','费用 / 元','说明'],[
        [run.get('label','历史实验'),percent(run.get('metrics',{}).get('cagr')),
         percent(run.get('metrics',{}).get('total_return')),percent(run.get('metrics',{}).get('max_drawdown')),
         percent(run.get('metrics',{}).get('average_exposure')),run.get('metrics',{}).get('trade_count'),number(run.get('metrics',{}).get('costs')),
         run.get('message',EXPERIMENT_STATES.get(run.get('status'),run.get('status','')))] for run in runs]) if runs else '<p class="muted">尚无可展示的历史回测结果。</p>'
    diagnostics=backtest.get('diagnostics',{})
    selection=backtest.get('selection_audit',{})
    protocol=backtest.get('protocol',{})
    diagnoses=''
    if diagnostics or selection:
        states='；'.join(f'{state} {count} 次' for state,count in diagnostics.get('states',{}).items())
        entry=diagnostics.get('entry_funnel',[])
        funnel=''
        if entry:
            funnel='<div id="entry-funnel">'+table(['当期候选顺序筛选','公司·月次数'],[[item.get('stage'),item.get('count')] for item in entry])
            funnel+='<p class="muted">仅统计当期候选，排除退池后继续跟踪的公司。各层是按顺序取交集，不表示单项条件的独立因果作用；合格候选次数不是订单或成交笔数。</p></div>'
        months=diagnostics.get('candidate_months',{})
        if months:
            funnel+=f'<p id="candidate-months" class="muted">月度评估 {esc(months.get("months"))} 次，其中 {esc(months.get("empty"))} 次无买入候选；合格候选累计 {esc(months.get("total_candidates"))} 个公司·月。</p>'
        unused=diagnostics.get('unused_share_conflicts',[])
        conflicts=''
        if unused:
            conflicts='<div id="unused-share-conflicts"><p><strong>未参与计算的季度股数冲突 · 原文待核实</strong></p>'+table(
                ['公司','冲突报告期','字段','评估月次数','其中当期候选月次数'],
                [[item.get(key) for key in ('symbol','period','field','count','active_count')] for item in unused])
            conflicts+='<p class="muted">每行按公司、冲突报告期、评估月去重；同股不同报告期可分别计数，不能当作独立公司数或成交笔数。这些季度股数字段未参与本次数值计算，原文尚未核实；年报股数及其他字段冲突仍按阻断处理。</p></div>'
        rebalances=selection.get('rebalances',[])
        rejected=Counter(item.get('reason','未记录原因') for item in (selection.get('rejected',[]) if not rebalances else [item for pool in rebalances for item in pool.get('rejected',[])]))
        reason_rows=[['建池淘汰（公司 / 建池时点）' if rebalances else '建池淘汰（公司）',reason,count] for reason,count in rejected.most_common()]
        reason_rows.extend([stage,r.get('reason'),r.get('count')] for key,stage in (('reasons','筛选未通过（公司 / 时点）'),('skips','执行跳过（事件）')) for r in diagnostics.get(key,[]))
        reasons=table(['阶段','未通过 / 未成交原因','次数'],reason_rows)
        selected=len(selection.get('stocks',[]))
        cohort_limit=protocol.get('cohort_limit',20)
        target_count=selection.get('target_count',protocol.get('pool_size',cohort_limit))
        selection_note=f"建池 {selected} 只 / 目标 {target_count if target_count is not None else '—'} 只；候选 {selection.get('candidate_count','—')} 家，已评估 {selection.get('evaluated_count','—')} 家。" if selection else ''
        pool_table=''
        if rebalances:
            selection_note=(f"历年候选池覆盖 {selected} 只；年末初筛候选全部检查，合格候选池不设数量上限，共 {len(rebalances)} 次建池。" if cohort_limit is None else
                            f"历年涉及 {selected} 只；每期最多 {target_count if target_count is not None else '—'} 只，共 {len(rebalances)} 次建池。")
            if selection.get('holding_limit') is not None:
                selection_note+=f"持仓最多 {selection['holding_limit']} 只。"
            selection_note+='评估总数还包含退出研究池后继续跟踪的公司。'
            pool_table=table(['建池日','实际入池','候选','未入池候选','已检查'],[[pool.get('day'),len(pool.get('symbols',[])),pool.get('candidate_count'),
                max(0,pool['candidate_count']-len(pool.get('symbols',[]))) if isinstance(pool.get('candidate_count'),int) else None,
                pool.get('evaluated_count')] for pool in rebalances])
            pool_table+='<p class="muted">仅在年末重筛候选池，月度评估当期候选；不是每月扫描全市场。实际入池是候选覆盖，不是实际持仓；已检查数使用实验记录，旧记录未保存时显示“—”。未入池候选含条件不满足或排序未选中，不等于财务不合格；年度淘汰原因按公司 / 建池时点计数，同一公司可能重复出现。</p>'
        diagnoses=f'''<details class="research-results" id="research-diagnostics"><summary>建池淘汰、筛选与未成交原因</summary><p>{esc(selection_note)}</p><p>共 {esc(diagnostics.get('evaluations'))} 次公司 / 时点评估。{esc(states)}</p>
        {funnel}{pool_table}{conflicts}<p class="muted">建池淘汰按{'公司 / 建池时点' if rebalances else '公司'}计数，筛选按公司 / 时点计数，执行跳过按事件计数，三者不能相加。数据不足、未达到价格要求和资金不够买一手是不同原因；不能为了产生交易把它们视为通过。</p>{reasons}</details>'''
    limitations=research.get('limitations',[])+backtest.get('limitations',[])
    notes=''.join(f'<li>{esc(note)}</li>' for note in dict.fromkeys(limitations) if note!=backtest.get('protocol',{}).get('monthly_targets'))
    backtest_state=backtest.get('status') or research.get('backtest',{}).get('status') or '待运行'
    backtest_state=EXPERIMENT_STATES.get(backtest_state,backtest_state)
    historical_count=backtest.get('coverage',{}).get('stocks')
    historical_note=f'历史涉及 {historical_count} 只；与当日模拟账户分开记录' if historical_count is not None else '与当日模拟账户分开记录'
    retention=percent(protocol.get('retention_yield'))
    holding_note=f'<span>持有门槛 {retention} / 仅月末检查</span>' if retention!='—' else ''
    target_note=f'<p>回放调仓规则：{esc(protocol["monthly_targets"])}</p>' if protocol.get('monthly_targets') else ''
    message=backtest.get('message') or research.get('backtest',{}).get('message') or research.get('message','')
    return f'''<section class="panel research-lab" id="research-lab"><div class="section-title"><h2>策略研究台</h2><span class="pill">手动运行 · 不启用定时任务</span></div>
    <div class="research-summary"><div><small>研究目标</small><strong>年化 ≥ 10%</strong><span>待验证，不是收益承诺</span></div><div><small>股票研究池</small><strong>{len(stocks)} / {esc(research.get('target_count',20))}</strong><span>研究对象，不等于持仓数量</span></div><div><small>历史检验状态</small><strong>{esc(backtest_state)}</strong><span>{esc(historical_note)}</span>{holding_note}</div></div>
    <p class="muted">研究截至 {esc(research.get('as_of'))} · {esc(research.get('version'))}。{esc(research.get('method',''))}</p>
    <div class="filters"><label>搜索全部研究股票<input id="research-search" type="search" placeholder="公司、代码、行业或待核实问题"></label><span class="muted" id="research-count" aria-live="polite"></span></div>
    <p class="research-table-hint muted">表格可左右滑动，右侧查看研究依据与 K 线。</p><div class="table-scroll"><table id="research-table"><thead><tr><th>公司</th><th>估值</th><th>流动性</th><th>情景股息率</th><th>分红 / 综合状态</th><th>查看</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
    <p id="research-empty" class="muted" hidden>没有匹配的研究对象。</p><div class="research-pagination"><button id="research-prev" type="button">上一页</button><span id="research-page" aria-live="polite"></span><button id="research-next" type="button">下一页</button></div>
    {comparison_chart(backtest) if backtest else ''}{history}{coverage_panel(backtest.get('coverage',{}))}{diagnoses}{period_diagnostics_panel(backtest.get('period_diagnostics',{})) if backtest.get('status')!='blocked' else ''}<details class="research-results"><summary>历史实验指标与验证边界</summary><p>{esc(message)}</p>{target_note}{comparisons}<ul>{notes}</ul><p class="muted">平均仓位指每天投资市值占总资产比例的平均值；现金也计入收益分母。单个区间超过 10% 不代表目标已验证；还需独立区间、交易成本、退市与数据时点检查。历史结果不能保证未来收益。</p></details></section>'''


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
    <p class="muted">本账本初始虚拟本金 ¥{number(account['account']['initial_cash'])} · 持股上限 {esc(data['rules'].get('max_holdings'))} 只 · 现金底仓 {percent(data['rules'].get('cash_reserve'))}。持股上限与每页展示数量是两回事。</p>
    <p class="muted">起点 {esc(account['account']['start_date'])} 收盘；基准 {esc(data['rules'].get('benchmark'))}。ETF 比较未扣投资者交易费用；模拟分红税前、未自动再投资。首日都是 0%，不能据此比较收益优劣。{esc(data.get('benchmark_error') or '')}</p>
    <h3>当前观察名单中满足条件的候选及资金分配</h3>{picks if visible else '<p class="empty">当前观察名单无满足全部条件的候选；完整结果见折叠摘要。</p>'}
    <p class="muted">机械盈利代理 = 近三年较低口径利润中位数、最近年度较低利润、TTM 归母、TTM 扣非四者取小；历史派息率是实验假设，不是公司承诺。未自动核实公告全文、业务与特别分红，不能将本结果用于实盘研究确认。</p>
    <details><summary>查看全部初筛原因与规则</summary>{screening}<pre class="analysis">{esc(json.dumps(data['rules'],ensure_ascii=False,indent=2))}</pre></details></section>'''


def historical_chart_data(workspace):
    latest=optional_report(workspace/'backtests'/'latest.json')
    if latest.get('status') not in ('diagnostic','complete') or not isinstance(latest.get('protocol'),dict):
        return {}
    version=latest.get('protocol',{}).get('version','')
    if not isinstance(version,str) or not re.fullmatch(r'historical-[a-z0-9]+(?:-[a-z0-9]+)*',version):
        return {}
    base=(workspace/'backtests').resolve()
    folder=(base/version).resolve()
    if not folder.is_relative_to(base):
        return {}
    def record(name):
        path=folder/name
        return optional_report(path) if path.resolve().is_relative_to(folder) else {}
    summary=record('summary.json')
    if summary.get('protocol',{}).get('version')!=version or summary.get('status') not in ('diagnostic','complete'):
        return {}
    coverage=summary.get('coverage',{})
    if not all(isinstance(coverage.get(key),str) and re.fullmatch(r'\d{8}',coverage[key]) for key in ('start','end')) or coverage['start']>coverage['end']:
        return {}
    market=record('market.json')
    stocks={}
    try:
        for code,bars in market.get('bars',{}).items():
            if not re.fullmatch(r'\d{6}\.(SH|SZ)',code):
                raise ValueError()
            candles=[{'day':day,**{key:float(bar[key]) for key in ('open','high','low','close','vol')}} for day,bar in sorted(bars.items())]
            if any(not re.fullmatch(r'\d{8}',bar['day']) or not all(math.isfinite(bar[key]) for key in ('open','high','low','close','vol')) for bar in candles):
                raise ValueError()
            stocks[code]={'candles':candles,'error':None}
    except (TypeError,ValueError,KeyError,AttributeError):
        return {}
    if not stocks:
        return {}
    names={s['symbol']:s.get('name',s['symbol']) for s in record('cohort.json').get('stocks',[])}
    names.update({s['symbol']:s.get('name',s['symbol']) for s in summary.get('selection_audit',{}).get('stocks',[])})
    runs=[]
    for rate in ('004','005','006'):
        label=f'{int(rate)}%股息门槛 · 全期'
        if not any(run.get('label')==label and run.get('status') in ('diagnostic','complete') for run in summary.get('runs',[])):
            continue
        result=record(f'yield-{rate}-result.json')
        if (result.get('start')!=coverage.get('start') or result.get('end')!=coverage.get('end')
                or not isinstance(result.get('plans'),list) or not isinstance(result.get('trades'),list)):
            continue
        runs.append({'id':rate,'label':f'本次历史 {int(rate)}%'+(' · 主方案' if rate=='005' else ''),
                     'plans':result['plans'],'fills':result['trades']})
    return {'version':version,'start':coverage.get('start'),'end':coverage.get('end'),'stocks':stocks,'names':names,'runs':runs} if runs else {}


def chart_panel(workspace, report, automatic, research=None):
    chart_path=workspace/'charts.json'
    chart_data=json.loads(chart_path.read_text(encoding='utf-8')) if chart_path.exists() else {}
    stocks=chart_data.get('stocks',{}) if chart_data.get('as_of')==report.get('as_of') else {}
    assessments=automatic.get('assessments',[])
    focus=list(automatic.get('focus_symbols',[r['symbol'] for r in report.get('rows',[])[:5]]))
    research=research or {}
    research_stocks=research.get('stocks',[]) if research.get('as_of')==report.get('as_of') else []
    focus=list(dict.fromkeys(focus+[r['symbol'] for r in research_stocks if r['symbol'] in stocks]))
    if research_stocks:
        focus=[code for code in focus if stocks.get(code,{}).get('candles') and not stocks[code].get('error')]
    preferred=next((a['symbol'] for a in assessments if a['state']=='满足价格条件' and a['symbol'] in focus),None)
    names={r['symbol']:r['name'] for r in report.get('rows',[]) if r['symbol'] in focus}
    names.update({r['symbol']:r.get('name',r['symbol']) for r in research_stocks})
    codes=focus
    stocks={code:stocks[code] for code in codes if code in stocks}
    historical=historical_chart_data(workspace) if report.get('source')!='DEMO' else {}
    if not codes and not historical:
        return ''
    options=''.join(f'<option value="{esc(s)}" {"selected" if s==preferred else ""}>{esc(s+" · "+names.get(s,s))}</option>' for s in codes)
    sources='<option value="daily">日常模拟</option>'+''.join(f'<option value="{run["id"]}">{esc(run["label"])}</option>' for run in historical.get('runs',[]))
    payload={'stocks':stocks,'names':names,'as_of':report.get('as_of'),'assessments':assessments,'plans':automatic.get('chart_plans',[]),'fills':automatic.get('chart_fills',[]),'notes':automatic.get('notes',{}),'historical':historical}
    encoded=json.dumps(payload,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return f'''<section class="panel" id="chart-panel"><div class="section-title"><h2>股票 K 线与自动模拟操作</h2><span class="muted">红涨绿跌 · 未复权日线</span></div>
    <div class="filters" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr))"><label>来源<select id="chart-source" style="width:100%;max-width:none">{sources}</select></label><label>股票<select id="chart-symbol">{options}</select></label><label>显示范围<select id="chart-range" style="width:100%;max-width:none"><option value="30">近 30 根</option><option value="60" selected>近 60 根</option><option value="120">近 120 根</option><option value="10000">全部已取数据</option></select></label><label>定位成交<select id="chart-trade" style="width:100%;max-width:none"><option value="">最近行情</option></select></label></div><p class="muted" id="chart-context"></p>
    <p class="chart-legend">○ 信：生成订单的信号日　<span class="buy">▲ 买：模拟买入成交</span>　<span class="sell">▼ 卖：模拟卖出成交</span><span id="chart-reference-legend">　虚线：基准情景参考价（在显示范围内时）</span></p>
    <div id="stock-chart" class="stock-chart" tabindex="0" aria-label="股票日 K 线；使用左右方向键查看各日详情"></div><p id="chart-tip" class="chart-tip" aria-live="polite"></p>
    <div id="chart-explanation" class="chart-explanation"></div><p class="muted">未复权便于核对成交原价，除权除息可能产生缺口；不把它解释为投资损失。图中只标所选来源的订单与成交；尚无成交时没有买卖箭头。历史模式不套用今天的参考价，历史曾入池名单不等于当日观察名单。</p>
    <script type="application/json" id="market-chart-data">{encoded}</script></section>'''


def render(workspace, ledger, report, status=None):
    status = status or {"ok":True,"message":"最新运行完成"}
    source = report.get("source", "未运行")
    demo = source == "DEMO"
    research=optional_report(workspace/'research'/'latest.json') if not demo else {}
    backtest=optional_report(workspace/'backtests'/'latest.json') if not demo else {}
    charts=optional_report(workspace/'charts.json')
    chart_symbols={code for code,stock in charts.get('stocks',{}).items() if stock.get('candles') and not stock.get('error')} if charts.get('as_of')==report.get('as_of')==research.get('as_of') else set()
    auto_file=automatic_path(workspace)/'latest.json'
    automatic=json.loads(auto_file.read_text(encoding='utf-8')) if auto_file.exists() else {}
    if automatic.get('day')!=report.get('as_of') or automatic.get('rules',{}).get('version')!=report.get('strategy_version'):
        automatic={}
    focus=automatic.get('focus_symbols',[r['symbol'] for r in report.get('rows',[])[:5]])[:5]
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
        <div class="detail-body"><button type="button" class="chart-link" data-chart-symbol="{esc(row['symbol'])}">查看 K 线与模拟操作</button><h3>三项筛选 · {esc(assessment.get('state','尚未模拟评估'))}</h3>{gates}<p>{esc(lot_note)}</p><div class="detail-grid"><section><h3>核查结论</h3><ul>{issues}</ul>{data_warnings_panel(row.get('data_warnings',[]))}<p class="muted">最新报告期 {esc(row.get('latest_period'))} · 取数截至 {esc(row.get('as_of'))}</p></section>
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
    research_script = (ROOT/'qmt/research-ui.js').read_text(encoding='utf-8')
    benchmark_script = (ROOT/'qmt/benchmark-ui.js').read_text(encoding='utf-8')
    page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NKG · 红利研究工作台</title><style>{style}</style></head>
    <body><main><header><a class="brand" href="#">NKG<span> / DIVIDEND LAB</span></a><span class="local">● 本地研究 · 人工执行</span></header>
    <section class="hero"><div><p class="eyebrow">RESEARCH FIRST, EXECUTION SECOND</p><h1>先看分红能否持续，<br>再等价格满足假设。</h1><p>从沪深主板寻找低估值、持续高股息与强流动性，保留反对证据与资料缺口。<br>研究、模拟、手动成交，分开记录。</p></div><div class="date-card"><small>行情截至</small><strong>{esc(report.get('as_of','尚未运行'))}</strong><span>{esc(source)} · 市场快照 {esc(report.get('pool_date','—'))}</span><span>生成于 {esc(report.get('generated_at','—'))}</span></div></section>
    <div class="notice {'error' if not status.get('ok') or demo else ''}">{'虚构演示数据，不可用于真实交易。 ' if demo else ''}{esc(status.get('message',''))}</div>
    {research_panel(research,backtest,chart_symbols,experiment_history_panel(workspace) if not demo else '')}
    {chart_panel(workspace,report,report.get('demo_operations',{}) if demo else automatic,research)}
    {('<details class="panel account-fold"><summary>当日模拟账户与订单检查</summary>'+automatic_panel(automatic)+'</details>') if research and not demo else automatic_panel(automatic) if not demo else ''}
    <section class="metrics"><div><small>财报研究覆盖</small><strong>{report.get('covered',0)}<i> / {report.get('pool_size',0)}</i></strong><span>主板 {report.get('universe_count',0)} 家，财报批次不代表全市场</span></div><div><small>待核实 / 观察</small><strong>{sum(r['status'] in ('待核实','观察') for r in rows)}</strong><span>资料不齐时不生成价格结论</span></div><div><small>排除</small><strong>{sum(r['status']=='排除' for r in rows)}</strong><span>保留具体原因，避免只看成功案例</span></div><div><small>可进一步评估</small><strong>{sum(r['status']=='可进一步评估' for r in rows)}</strong><span>满足研究条件 ≠ 自动买入</span></div></section>
    <section class="panel"><div class="section-title"><h2>重点财报详情 · 当前展示 {len(details)} 只</h2><span class="muted">{'最多展示 5 只重点详情；完整研究池可在上方翻页' if research else '重点详情不代表持股上限；观察不等于买入'}</span></div><div class="filters"><label>在重点详情中查找<input id="search" type="search" placeholder="公司、代码或行业"></label><label>研究状态<select id="status"><option value="">全部状态</option><option>待核实</option><option>观察</option><option>排除</option><option>可进一步评估</option></select></label></div>
    <div id="candidates">{''.join(details) or '<p class="empty">运行 daily 获取候选池与数据。</p>'}</div><p id="no-result" hidden>没有匹配的候选。</p></section>
    {''.join(accounts)}<footer><strong>工作台边界</strong><p>参考价基于正常化盈利与派息假设，税前，不是内在价值。模拟使用下一交易日开盘加不利滑点，含估计佣金与卖出印花税；不保证实际成交。现金分红按登记日资格记应收、派息日入账；个人股息税及送转股需另核对。历史实验不能证明策略未来收益或实盘表现。</p><p>密钥仅保存在本地配置。研究请求只包含公司公开数据，不上传账本。报告含个人记账信息时请勿公开分享。</p></footer></main>
    <script>const search=document.querySelector('#search'),status=document.querySelector('#status');function filter(){{let n=0;document.querySelectorAll('.candidate').forEach(el=>{{const visible=el.dataset.search.toLowerCase().includes(search.value.toLowerCase())&&(!status.value||el.dataset.status===status.value);el.hidden=!visible;if(visible)n++;}});document.querySelector('#no-result').hidden=n>0;}}search.addEventListener('input',filter);status.addEventListener('change',filter);</script><script>{chart_script}</script><script>{research_script}</script><script>{benchmark_script}</script></body></html>'''
    path = workspace/'dashboard.html'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(page,encoding='utf-8')
    tmp.replace(path)
    return path
