import html
import json
import math
import re
from collections import Counter
from pathlib import Path

from .config import ROOT, AppError, date_key, dec, money, now_cn
from .context import safe_url
from .selection import automatic_path


EXPERIMENT_STATES={'diagnostic':'仅诊断，待验证','running':'检验中','pending':'待运行','failed':'检验未完成','blocked':'输入待核实','complete':'历史实验完成'}
EARNINGS_BASES={'conservative_minimum':'历史及 TTM 取低','ttm':'最近十二个月取低'}
ENTRY_MODES={'yield_threshold':'固定股息门槛','yield_rank':'股息排序'}
FREEZE_POLICIES={'portfolio':'组合停买','position':'按股冻结'}


def esc(value):
    return html.escape("—" if value is None else str(value),quote=True)


def number(value, places=2):
    if value is None:
        return "—"
    try:
        numeric=float(value)
        return f"{numeric:,.{places}f}" if math.isfinite(numeric) else '—'
    except (ValueError,TypeError,OverflowError):
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


def earnings_basis_label(protocol):
    return EARNINGS_BASES.get(protocol.get('earnings_basis','conservative_minimum'),'未识别口径')


def entry_mode_label(protocol):
    return ENTRY_MODES.get(protocol.get('entry_mode','yield_threshold'),'未识别入场方式')


def freeze_policy_label(protocol):
    return FREEZE_POLICIES.get(protocol.get('freeze_policy'),'未记录')


def freeze_policy_note(protocol):
    policy=protocol.get('freeze_policy')
    note={'portfolio':'有实际持仓待核实时，保持冻结股数及资金、行业占额，暂停组合全部新增买入（含加仓）；其他持仓仍可按规则卖出。',
          'position':'保持冻股原数量及资金、行业占额，其他通过全部资格者可买入；身份或行业未知仍暂停组合新增买入，缺少持仓估值或未核实权益仍阻断回放。'}.get(
          policy,'旧归档未记录冻结政策，不能据此推断已按股冻结。')
    return f'<p id="freeze-policy">历史实验冻结政策：{esc(freeze_policy_label(protocol))}。{esc(note)}日常模拟规则未随此实验改变。</p>'


def capital_ratios(protocol):
    try:
        cfg=protocol['account_config']
        initial=money(cfg['paper_cash'])
        position,sector=(money(cfg[key]) for key in ('paper_max_position','paper_max_sector'))
        return (position/initial,sector/initial) if 0<position<=sector<=initial else None
    except (AppError,KeyError,TypeError):
        return None


def capital_policy_note(protocol, registered=None):
    policy=protocol.get('capital_policy','fixed_amount')
    if policy not in ('fixed_amount','equity_ratio'):
        return f'<p id="capital-policy">资金规则待核实：未识别 {esc(policy)}，不按固定金额解释。</p>'
    if registered and (registered.get('version')!=protocol.get('version') or registered.get('capital_policy','fixed_amount')!=policy):
        return '<p id="capital-policy">资金规则待核实：归档协议与当前摘要不一致。</p>'
    source=registered or protocol; cfg=source.get('account_config',{})
    if policy=='fixed_amount':
        try:
            position,sector=(money(cfg[key]) for key in ('paper_max_position','paper_max_sector'))
            if not 0<position<=sector:
                position=sector=None
        except (AppError,KeyError,TypeError):
            position=sector=None
        return f'<p id="capital-policy">历史资金规则：固定金额上限，单股 ¥{number(position)}、行业 ¥{number(sector)}；额度不随净值变化。未记录或未核实金额显示 —，不采用当前配置补填。</p>'
    ratios=capital_ratios(source)
    if ratios is None:
        return '<p id="capital-policy">历史资金规则：按净值比例；初始本金或额度未核实，暂不显示比例。</p>'
    slots=source.get('rules',{}).get('max_holdings')
    allocation=(f'保留 10% 现金，按 {slots} 个等权槽位分配，每股目标预算 {percent(min(ratios[0],dec(".9")/slots))} 净值；' if type(slots) is int and 1<=slots<=20
                else '等权槽位未记录，目标预算待核实；')
    return f'<p id="capital-policy">历史资金规则：按净值比例，单股上限 {percent(ratios[0])}、行业上限 {percent(ratios[1])}。比例以原始单股/行业金额上限除以初始本金固定，金额随净值涨跌。{allocation}不要求买满，仍受行业、现金及整手约束。冻结股保留原数量；价格被动超限不自动触发日内卖出。</p>'


def valuation_policy_note(protocol, registered=None):
    policy=protocol.get('valuation_policy','historical_gate')
    if policy not in ('historical_gate','historical_context'):
        return f'<p id="valuation-policy">估值规则待核实：未识别 {esc(policy)}。</p>'
    if (registered and (registered.get('version')!=protocol.get('version') or registered.get('valuation_policy','historical_gate')!=policy)
            or policy=='historical_context' and not registered):
        return '<p id="valuation-policy">估值规则待核实：归档协议缺失或与当前摘要不一致。</p>'
    if policy=='historical_gate':
        return ''
    return '<p id="valuation-policy">历史估值实验：自身三年 PE 分位仅作参考，已知分位超过 50% 不单独否决；仍须正 PE≤25、不高于当时行业正 PE 中位数、保守盈利 PE≤25。历史正 PE 至少 250 个样本、同行至少 5 家，资料缺失仍待核实；日常规则不变。</p>'


def exit_policy_note(protocol, registered=None):
    policy=protocol.get('exit_policy','review_freeze')
    if policy not in ('review_freeze','known_failure_exit'):
        return f'<p id="exit-policy">退出规则待核实：未识别 {esc(policy)}。</p>'
    if (registered and (registered.get('version')!=protocol.get('version') or registered.get('exit_policy','review_freeze')!=policy)
            or policy=='known_failure_exit' and (not registered or not protocol.get('version'))):
        return '<p id="exit-policy">退出规则待核实：归档协议缺失或与当前摘要不一致。</p>'
    if policy=='review_freeze':
        return ''
    return '<p id="exit-policy">历史退出实验：仅在月末评估中，已知三年累计 FCF 覆盖不足或短债覆盖不足且资料完整校验通过时，允许持仓按原月度交易规则退出；仍拒绝买入。资料缺失或冲突仍冻结，行业变更或区间未知仍冻结。允许退出不等于已成交，仍受停牌、真实行情、股份权益及成交规则约束；不改变年度选股、预测公式或日常模拟。</p>'


def rebalance_policy_note(protocol, registered=None):
    policy=protocol.get('rebalance_policy','monthly_only')
    if policy not in ('monthly_only','after_confirmed_sale'):
        return f'<p id="rebalance-policy">补单规则待核实：未识别 {esc(policy)}。</p>'
    if (registered and (registered.get('version')!=protocol.get('version') or registered.get('rebalance_policy','monthly_only')!=policy)
            or policy=='after_confirmed_sale' and (not registered or not protocol.get('version'))):
        return '<p id="rebalance-policy">补单规则待核实：归档协议缺失或与当前摘要不一致。</p>'
    if policy=='monthly_only':
        return ''
    return '<p id="rebalance-policy">执行实验：仅对月末因持仓数量上限未创建的原目标买单，在下一交易日卖单真实成交后，收盘按现金与空位补一次，最早再下一交易日执行。限价和股数不超过原目标；不追涨、不在卖出当日开盘回填、不重试未成交买单。筛选、20只上限、行业额度及冻结规则不变；允许补单不等于已经成交。</p>'


def approved_cash_note(protocol, diagnostics):
    if protocol.get('dividend_policy')!='approved_annual_cash':
        return ''
    detail=diagnostics.get('cash_estimates',{})
    stages={'approved':'已批准未实施','implemented':'已实施公告','unresolved':'待核实','not_recorded':'未记录'}
    bases={'commitment':'承诺现金覆盖','implemented_announcements':'已实施公告现金覆盖','not_recorded':'未记录'}
    counts=[]
    for label,values,names in (('方案阶段',detail.get('stages',{}),stages),('现金覆盖口径',detail.get('coverage_bases',{}),bases)):
        if values:
            counts.append(f'<p>{label}：'+ '；'.join(f'{esc(names.get(key,"未识别"))} {esc(value)} 次' for key,value in values.items())+'。</p>')
    return '<div id="dividend-policy"><p>历史实验股息估计规则：已批准年度现金估计。已批准方案只用于前瞻估计和承诺现金覆盖，不是本账户实际到账，不生成现金流水或分红标记。实际权益仍只按已实施事件结算。</p><p>q 为同一财政年度的普通现金估算总额 / 归母利润；支付率取近三年 q 中位数、最新年度 q 与 1 三者最小。最新年度已经实施时也受最新 q 上限约束。普通现金估算不等于可持续分红已获证明。</p>'+''.join(counts)+'<p class="muted">阶段与覆盖次数按全部月度公司评估记录计数，包含退出名单后的跟踪；不是持股、订单、成交或到账次数。待核实和未记录不补零；日常模拟规则未随此实验改变。</p></div>'


def experiment_history_panel(workspace):
    versions={}
    for path in sorted((workspace/'backtests').glob('*/summary.json')):
        summary=optional_report(path)
        registered=optional_report(path.with_name('protocol.json'))
        protocol=registered or summary.get('protocol',{})
        valuation_policy=protocol.get('valuation_policy','historical_gate')
        valuation_mismatch=(valuation_policy not in ('historical_gate','historical_context')
            or summary.get('protocol',{}).get('valuation_policy','historical_gate')!=valuation_policy
            or valuation_policy=='historical_context' and (not registered.get('version')
                or registered.get('version')!=summary.get('protocol',{}).get('version')))
        exit_policy=protocol.get('exit_policy','review_freeze')
        exit_mismatch=(exit_policy not in ('review_freeze','known_failure_exit')
            or summary.get('protocol',{}).get('exit_policy','review_freeze')!=exit_policy
            or registered and registered.get('version')!=summary.get('protocol',{}).get('version')
                and (exit_policy=='known_failure_exit' or 'exit_policy' in registered or 'exit_policy' in summary.get('protocol',{}))
            or exit_policy=='known_failure_exit' and not registered.get('version'))
        rebalance_policy=protocol.get('rebalance_policy','monthly_only')
        rebalance_mismatch=(rebalance_policy not in ('monthly_only','after_confirmed_sale')
            or summary.get('protocol',{}).get('rebalance_policy','monthly_only')!=rebalance_policy
            or rebalance_policy=='after_confirmed_sale' and (not registered.get('version')
                or registered.get('version')!=summary.get('protocol',{}).get('version')))
        version=protocol.get('version',path.parent.name)
        primary_labels=(('股息排序 · 全期','股息排序') if protocol.get('entry_mode')=='yield_rank'
                        else ('5%股息门槛 · 全期','税前股息门槛 5%') if protocol.get('entry_mode','yield_threshold')=='yield_threshold' else ())
        primary=next((run for run in summary.get('runs',[])
                      if run.get('label') in primary_labels), {})
        state=summary.get('status') or primary.get('status') or 'pending'
        metrics=summary.get('primary_metrics',primary.get('metrics',{}))
        if state=='blocked' or primary.get('status')=='blocked' or valuation_mismatch or exit_mismatch or rebalance_mismatch:
            state='blocked'
            metrics={}
        coverage=summary.get('coverage',{})
        cash=summary.get('initial_cash',summary.get('account',{}).get('initial_cash',protocol.get('account_config',{}).get('paper_cash')))
        period=f"¥ {number(cash)} · {coverage.get('start',protocol.get('selection_day','—'))}—{coverage.get('end',summary.get('as_of',protocol.get('replay_end','—')))}"
        cohort=f"{version} · 首次建池 {protocol.get('selection_day','—')} · 累计 {coverage.get('stocks','—')} 只"
        if protocol.get('dividend_policy')=='approved_annual_cash':
            cohort+=' · 已批准年度现金估计'
        if valuation_mismatch:
            cohort+=' · 估值规则待核实：归档缺失、规则未知或与摘要不一致'
        elif valuation_policy=='historical_context':
            cohort+=' · 历史 PE 分位仅作参考'
        if exit_mismatch:
            cohort+=' · 退出规则待核实：归档缺失、规则未知或与摘要不一致'
        elif exit_policy=='known_failure_exit':
            cohort+=' · 已知财务失败月度退出，行业变更仍冻结'
        if rebalance_mismatch:
            cohort+=' · 补单规则待核实：归档缺失、规则未知或与摘要不一致'
        elif rebalance_policy=='after_confirmed_sale':
            cohort+=' · 卖出成交后一次补单'
        versions[version]=[cohort,period,EXPERIMENT_STATES.get(state,state),percent(metrics.get('cagr')),
                           percent(metrics.get('average_exposure')),number(metrics.get('costs')),earnings_basis_label(protocol),entry_mode_label(protocol),freeze_policy_label(protocol)]
    if not versions:
        return ''
    rows=table(['实验版本 / 首次建池 / 累计股票数','本金 / 回放区间','状态','年化收益','平均仓位','费用 / 元','盈利口径','入场方式','冻结政策'],versions.values())
    return f'''<details class="research-results" id="experiment-history"><summary>历次实验对照 · {len(versions)} 个版本</summary>
    <p class="muted">仅列本工作区保存的各版本预设主方案全期结果；固定门槛旧实验取 5% 主方案，股息排序实验只有一个排序方案。保留旧实验，未混入其他本金工作区。输入阻塞时指标为 —，不是 0% 收益。未声明盈利口径的旧归档按“历史及 TTM 取低”解释；口径变化不是收益验证。未声明冻结政策的旧归档显示“未记录”，不能据此推断已按股冻结。建池日期、本金、入场方式、冻结政策和区间不同的结果不能直接视作策略改进。</p>{rows}</details>'''


def comparison_chart(backtest):
    series=backtest.get('comparison_series',[])
    if not series:
        return '<p class="muted">比较曲线待取数，缺少数据时不绘制替代走势。</p>'
    missing_strategy='' if any(line.get('kind')=='strategy' for line in series) else '<p class="notice">本区间策略结果尚不可用。现金与 ETF 只作参照，不能把现金线当作策略收益或已通过验证。</p>'
    valuations=backtest.get('diagnostics',{}).get('suspension_valuations')
    valuation_note=(' 停牌沿用前收盘仅作持仓账面估值，不代表当日成交价或公允价值；'
                    '<a href="#suspension-valuations">查看停牌账面估值与逐日证据</a>。') if isinstance(valuations,dict) and valuations else ''
    encoded=json.dumps(series,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return f'''<section class="comparison-panel"><h3>历史资产走势 · 起点 = 1</h3>{missing_strategy}<div id="comparison-legend" class="comparison-legend" aria-label="显示比较曲线"></div>
    <div id="comparison-chart" class="comparison-chart" tabindex="0" aria-label="历史资产比较；左右方向键查看日期"></div><p id="comparison-tip" class="chart-tip" aria-live="polite"></p>
    <p class="muted">策略净资产、ETF 复权价格和现金分别计算；ETF 不是可成交账本，费用及分红处理口径可能不同。图形只表示历史记录，不外推目标收益。{valuation_note}</p>
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


def forecast_diagnostic_panel(data, backtest=None, registered_protocol=None):
    if not data:
        return ''
    backtest=backtest or {}; protocol=backtest.get('protocol',{})
    engine=backtest.get('source_engine_sha256')
    matching=bool(isinstance(engine,str) and re.fullmatch('[0-9a-f]{64}',engine)
        and data.get('source_engine_sha256')==engine and data.get('source_version'))
    for key,default,allowed in (('dividend_policy','implemented_only',('implemented_only','approved_annual_cash')),
                               ('cashflow_policy','latest_and_three_year',('latest_and_three_year','three_year_coverage')),
                               ('capital_policy','fixed_amount',('fixed_amount','equity_ratio')),
                               ('valuation_policy','historical_gate',('historical_gate','historical_context')),
                               ('exit_policy','review_freeze',('review_freeze','known_failure_exit')),
                               ('rebalance_policy','monthly_only',('monthly_only','after_confirmed_sale'))):
        matching=matching and protocol.get(key,default) in allowed and data.get(key,default)==protocol.get(key,default)
    if registered_protocol or protocol.get('valuation_policy')=='historical_context' or protocol.get('exit_policy')=='known_failure_exit' or protocol.get('rebalance_policy')=='after_confirmed_sale':
        matching=matching and bool(registered_protocol and registered_protocol.get('version')==protocol.get('version')
            and registered_protocol.get('valuation_policy','historical_gate')==protocol.get('valuation_policy','historical_gate')
            and registered_protocol.get('exit_policy','review_freeze')==protocol.get('exit_policy','review_freeze')
            and registered_protocol.get('rebalance_policy','monthly_only')==protocol.get('rebalance_policy','monthly_only')
            and (protocol.get('exit_policy')!='known_failure_exit' and protocol.get('rebalance_policy')!='after_confirmed_sale' or protocol.get('version')))
    title='独立预测诊断' if matching else '其他版本的预测诊断'
    opening=f'<details class="panel research-results" id="forecast-diagnostic"><summary>{title}</summary>'
    if data.get('version') not in ('dividend-forecast-v1','dividend-forecast-v1-approved-cash') or data.get('status')!='diagnostic':
        return opening+f'<p class="muted">来源 {esc(data.get("source_version"))}。诊断记录不可用，未用旧结果或零误差替代。</p></details>'
    coverage=data.get('coverage',{})
    rows=[]
    yearly_coverage=[]
    pending_counts=Counter()
    for year in data.get('by_year',[]):
        yearly_coverage.append(f"FY{year.get('target_year','—')}：{year.get('proxy_scored','—')} / {year.get('candidates','—')}")
        pending_counts.update({state:count for state,count in year.get('status_counts',{}).items()
                               if state not in ('proxy_total_cash','verified_zero_cash') and type(count) is int and count>0})
        for method,label in data.get('methods',{}).items():
            metrics=year.get('metrics',{}).get(method,{})
            count=metrics.get('count',0)
            scored=type(count) is int and count>0
            errors=[]
            for key in ('bias_yield_bp','mae_yield_bp'):
                try:
                    value=metrics.get(key)
                    errors.append(number(value) if scored and math.isfinite(float(value)) else '—')
                except (TypeError,ValueError,OverflowError):
                    errors.append('—')
            state='代理对照' if scored else '待观察' if year.get('status_counts',{}).get('pending_year_incomplete') else '待核实'
            rows.append([f"{year.get('origin','—')} → FY{year.get('target_year','—')}",label,count,*errors,state])
    comparison=table(['预测原点 → 目标年度','估计口径','代理样本数','平均误差 / 基点','平均绝对误差 / 基点','状态'],rows)
    pending_labels={'pending_zero_or_annual_confirmation':'年度 / 零预案待核实','pending_stock_actions':'送转待核实',
                    'pending_year_incomplete':'目标年度未完成','pending_payment':'现金尚未支付','pending_data_conflict':'数据冲突待核实'}
    pending_note='；'.join(f'{pending_labels.get(state,state)} {count} 组' for state,count in pending_counts.items()) or '未记录待核实原因'
    zero_cases=[]
    for case in data.get('verified_zero_cases',[]):
        label=case.get('label',{})
        source=safe_url(label.get('source'))
        source_note=f"原文 · {label.get('available_on','—')} · 第 {label.get('page','—')} 页"
        link=f'<a href="{esc(source)}" target="_blank" rel="noreferrer">{esc(source_note)}</a>' if source else '原文链接未记录'
        cells=[f"{case.get('name','—')} · {case.get('symbol','—')}",f"FY{case.get('target_year','—')} · 原点 {case.get('origin','—')}",
               *[number(case.get('estimates_dps',{}).get(method),3) for method in data.get('methods',{})],number(label.get('observed_cash_dps'),3)]
        zero_cases.append('<tr>'+''.join(f'<td>{esc(cell)}</td>' for cell in cells)+f'<td>{link}</td></tr>')
    zero_panel=''
    if zero_cases:
        headings=['公司','目标年度 / 预测原点',*[f'{label} / 元' for label in data.get('methods',{}).values()],'实际全年每股派息 / 元','核实依据']
        zero_panel='<h4>原文核实的零派息反例</h4><div class="table-scroll"><table><thead><tr>'+''.join(f'<th>{esc(label)}</th>' for label in headings)+'</tr></thead><tbody>'+''.join(zero_cases)+'</tbody></table></div><p class="muted">零派息反例单列，不混入下方未分类现金派息代理误差。</p>'
    notes=''.join(f'<li>{esc(note)}</li>' for note in data.get('limitations',[]))
    return f'''{opening}<p><strong>原文核实全年零派息 {esc(coverage.get('verified_zero',0))} 组；已观察现金派息代理 {esc(coverage.get('proxy_scored'))} 组。</strong>
    共 {esc(coverage.get('predictions'))} 组预测，未评分 {esc(coverage.get('pending'))} 组。每组为一家公司、一个预测原点。</p>
    <p class="muted">来源 {esc(data.get('source_version'))} · 结果资料截至 {esc(data.get('outcome_as_of'))}。{esc(data.get('horizon',''))}</p>
    <p class="muted">年度覆盖（派息代理 / 全部候选）：{esc('；'.join(yearly_coverage))}。</p><p class="muted">未评分原因：{esc(pending_note)}。</p>
    {zero_panel}<p class="muted">仅对资料符合条件的现金派息代理计算误差；其余停派 / 零分红待核实样本仍未纳入，不能据此选出更优规则。</p>{comparison}
    <p class="muted">误差 =（预计每股派息 − 已观察现金派息）÷ 原点股价 × 10,000；正值表示高估。无可评分样本显示 —，不等于零误差。代理对照不证明普通股息预测准确，也不自动改变交易规则。</p><ul>{notes}</ul></details>'''


def data_warnings_panel(warnings):
    if not warnings:
        return ''
    groups={}
    for item in warnings:
        api,field=item.get('api'),item.get('field')
        kind=('shares' if api in (None,'balancesheet') and field=='total_share' else
              'roe' if api=='fina_indicator' and field=='roe' else 'unknown')
        groups.setdefault(kind,[]).append(item)
    blocking_fields='参与计算的其他字段' if 'roe' in groups else '其他字段'
    labels={
        'shares':('未参与计算的股数冲突',f'该季度股数字段未参与本次数值计算；这里只记录接口差异，尚未核对原文。年报股数及{blocking_fields}冲突仍按阻断处理。'),
        'roe':('未参与计算的财务指标 ROE 冲突','该财务指标 ROE 未用于本策略计算；冲突值未知待核实，来源变体保留在研究数据中，尚未核对原文。'),
        'unknown':('数据字段冲突','字段类型未识别，值未知待核实；这里只保留接口差异，不推断其是否参与策略计算。')}
    panels=[]
    for kind,group in groups.items():
        title,note=labels[kind]
        items=''.join(f'<li class="analysis">报告期 {esc(item.get("period"))} · 公告日 {esc(item.get("ann_date"))} · {esc(item.get("api"))}.{esc(item.get("field"))}<br>接口原始值：{esc(" / ".join(str(value) for value in item.get("values",[])))}</li>' for item in group)
        panels.append(f'''<div class="data-warnings"><p><strong>{title} · 原文待核实</strong></p><ul>{items}</ul>
    <p class="muted">{note}</p></div>''')
    return ''.join(panels)


def chart_stock_events(events):
    return [{**event,'source':safe_url(event.get('source'))} for event in (events or [])]


def chart_dividend_notes(events_by_symbol, as_of):
    notes=[]
    for symbol,events in events_by_symbol.items():
        for event in events:
            source=safe_url(event.get('correction_source'))
            if not source or not event.get('correction_available_on'):
                continue
            try:
                available=date_key(event['correction_available_on'])
                if available>as_of:
                    continue
                record,pay=[date_key(event.get(key)) for key in ('record_date','pay_date')]
                for component in event.get('fiscal_components',[event]):
                    if not all(key in component for key in ('ordinary_cash_div_tax','special_cash_div_tax')):
                        continue
                    cash,ordinary,special=[dec(component[key]) for key in ('cash_div_tax','ordinary_cash_div_tax','special_cash_div_tax')]
                    if cash!=ordinary+special:
                        continue
                    notes.append({'symbol':symbol,'end_date':date_key(component['end_date']),
                                  'record_date':record,'pay_date':pay,'cash_div_tax':str(cash),
                                  'ordinary_cash_div_tax':str(ordinary),'special_cash_div_tax':str(special),
                                  'available_on':available,'source':source})
            except (AppError,KeyError,TypeError,ValueError):
                continue
    return notes


def stock_events_panel(events):
    if not events:
        return ''
    rows=[]
    for event in chart_stock_events(events):
        source=f' · <a href="{esc(event["source"])}" target="_blank" rel="noreferrer">实施依据</a>' if event['source'] else ' · 来源未记录'
        rows.append(f'''<li class="analysis">{esc(event.get('symbol'))} · 除权日 {esc(event.get('ex_date'))} · 新增 {esc(event.get('quantity'))} 股<br>
        登记日 {esc(event.get('record_date'))} 持股 {esc(event.get('record_quantity'))} 股；每登记股新增 {esc(event.get('ratio'))} 股；上市日 {esc(event.get('stock_list_date'))}{source}</li>''')
    return f'<details class="stock-events"><summary>送转股数流水 · {len(events)} 笔</summary><p class="muted">送转增加股数，不是买入成交；上市日期不等于除权日期。缺失信息显示 —，不由界面推算。</p><ul>'+''.join(rows)+'</ul></details>'


def dividend_cash_panel(annual):
    if not annual:
        return ''
    if not any(any(key in year for key in ('estimated_dividend_cash','estimated_ordinary_cash','dividend_cash_basis')) for year in annual):
        return '<p class="muted dividend-cash-legacy">旧摘要未记录公司派息总额与逐事件股本依据，未从 DPS 回推金额；需重新生成研究摘要。</p>'
    totals=table(['年度','估算公司派息总额 / 元','剔除已知特别后的估算额 / 元'],[
        [year['period'][:4],number(year.get('estimated_dividend_cash')),number(year.get('estimated_ordinary_cash'))] for year in annual])
    details=[]
    for year in annual:
        events=[]
        for item in year.get('dividend_cash_basis',[]):
            basis={'verified_distribution_shares':'已核实派息股数','year_end_proxy':'年报期末股数代理（未核实派息股数）'}.get(item.get('basis'),'股本口径未记录')
            source=safe_url(item.get('source'))
            link=f'<a href="{esc(source)}" target="_blank" rel="noreferrer">股本依据原文</a>' if source else '来源链接未记录'
            share_period=f" · 代理年报期 {esc(item.get('share_period'))}" if item.get('basis')=='year_end_proxy' else ''
            events.append(f'''<li><strong>{esc(basis)}</strong> · {number(item.get('shares'),0)} 股{share_period} · 依据可用日 {esc(item.get('available_on'))} · {link}
            <p>归属期 {esc(item.get('end_date'))} · 登记日 {esc(item.get('record_date'))} · 除息日 {esc(item.get('ex_date'))}<br>
            每股派息 {esc(item.get('cash_div_tax'))} 元 / 剔除已知特别后 {esc(item.get('ordinary_cash_div_tax'))} 元；本笔公司金额 {number(item.get('estimated_dividend_cash'))} 元 / 剔除已知特别后 {number(item.get('estimated_ordinary_cash'))} 元。</p></li>''')
        empty='已核实全年零现金分红，不产生支付事件或派息股数依据。' if year.get('zero_cash_proof') else '未记录逐事件股本依据。'
        details.append(f'<details><summary>{esc(year["period"][:4])} 年逐事件股本依据</summary>'+('<ul>'+''.join(events)+'</ul>' if events else f'<p class="muted">{empty}</p>')+'</details>')
    return f'''<details class="dividend-cash"><summary>公司派息总额与股本依据</summary>{totals}
    <p class="muted">金额直接采用逐事件汇总；“—”表示未知或旧摘要未记录，不等于零，缺失时不以已知部分代替全年总额。股数核实不代表普通派息可持续；年报期末股数只是代理。个人税前应收仍按登记日实际持股 × 每股派息计算，不按公司总股本计算。</p>{''.join(details)}</details>'''


def zero_cash_proof_panel(annual):
    notes=[]
    for year in annual:
        proof=year.get('zero_cash_proof')
        if not isinstance(proof,dict) or not proof:
            continue
        source=safe_url(proof.get('source'))
        link=f'<a href="{esc(source)}" target="_blank" rel="noreferrer">核实原文</a>' if source else '原文链接不可用'
        notes.append(f'<p class="muted zero-cash-proof">所属报告期 {esc(year.get("period"))}：已核实全年现金分红为 0；公告可用日 {esc(proof.get("available_on"))} · {link}。核实全年零不生成支付事件。</p>')
    return ''.join(notes)


def disclosure_diagnostics_panel(diagnostics):
    panels=[]
    zeros=diagnostics.get('verified_zero_cash',[])
    if zeros:
        rows=[]
        for item in zeros:
            source=safe_url(item.get('source'))
            link=f'<a href="{esc(source)}" target="_blank" rel="noreferrer">核实原文</a>' if source else '原文链接不可用'
            cells=''.join(f'<td>{esc(item.get(key))}</td>' for key in ('symbol','period','available_on','count'))
            rows.append(f'<tr>{cells}<td>{link}<p>{esc(item.get("note"))}</p></td></tr>')
        panels.append('<div id="verified-zero-cash"><p><strong>已核实全年零现金分红</strong></p><p class="muted">这是已知不满足连续现金分红规则，不是数据缺口，也不是现金入账；核实全年零不生成支付事件。</p>'
                      '<div class="table-scroll"><table><thead><tr><th>公司</th><th>所属报告期</th><th>公告可用日</th><th>评估月次数</th><th>核实依据</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div></div>')
    gaps=diagnostics.get('financial_gaps',[])
    if gaps:
        panels.append('<div id="financial-gaps"><p><strong>缺失报表字段</strong></p><p class="muted">缺失报表字段不能补零；未知金额与已核实零分红分开记录。</p>'+table(
            ['公司','报告期','字段','字段说明','公告可用日','评估月次数'],
            [[item.get(key) for key in ('symbol','period','field','label','available_on','count')] for item in gaps])+'</div>')
    if panels:
        panels.append('<p class="muted">以上次数来自保存的公司 / 报告期评估记录，同一公司跨月可能重复计数，不等于独立公司数、订单或现金入账次数。</p>')
    return ''.join(panels)


def holding_freezes_panel(data):
    if not isinstance(data,dict) or not data:
        return ''
    latest=data.get('latest') or {}
    blocked='是' if latest.get('portfolio_blocked') is True else '否' if latest.get('portfolio_blocked') is False else '未记录'
    latest_count=latest.get('held_frozen')
    held_text=f'{esc(latest_count)} 只' if latest_count is not None else '未记录'
    current=(f'最近评估日 {esc(latest.get("day"))}：实际冻结持仓 {held_text}；'
             f'组合暂停买入：{blocked}；买入尝试跳过 {esc(latest.get("buy_skips"))} 次。')
    if latest_count==0:
        current+='本期无实际冻结持仓。'
    rows=[]
    for item in sorted(data.get('rows',[]),key=lambda item:str(item.get('day','')),reverse=True):
        exited='否' if item.get('active') is True else '是' if item.get('active') is False else '未记录'
        rows.append([item.get('day'),item.get('symbol'),item.get('quantity'),number(item.get('market_value')),
                     percent(item.get('weight')),exited,FREEZE_POLICIES.get(item.get('scope'),'未记录'),item.get('reason')])
    details=table(['评估日','公司代码','实际股数','冻结持仓市值 / 元','占当日净值','已退研究池','冻结范围','当时原因'],rows) if rows else '<p class="muted">本记录没有实际冻结持仓明细。</p>'
    return f'''<div id="holding-freezes"><p><strong>历史主方案 · 实际持仓冻结</strong> · 诊断政策：{esc(FREEZE_POLICIES.get(data.get('policy'),'未记录'))}</p>
    <p class="muted">仅列当期冻结清单与当日实际持仓的交集，不包含未持有的研究对象。冻结持仓仍计入净值、持仓席位及行业占额；市值不是被扣除的现金，也不是冻结造成的亏损。</p>
    <p id="holding-freezes-latest">{current}</p>
    <p>月度评估 {esc(data.get('months'))} 次；存在实际冻结持仓 {esc(data.get('frozen_months'))} 个组合月；组合暂停买入 {esc(data.get('portfolio_blocked_months'))} 个组合月；记录买入受阻 {esc(data.get('buy_skip_months'))} 个组合月、{esc(data.get('buy_skips'))} 次买入尝试；冻结明细累计 {esc(data.get('company_months'))} 公司·月。</p>
    <p class="muted">每行是一家公司在一个评估月的实际持仓状态，同月多只冻结持仓仍只算一个组合月。跳过次数是买入尝试，不是订单、成交或亏损，也不分摊为单只股票的独立影响。最新评估日不等于回放最后估值日；缺失资料或待核实事项不等于已知财务不合格。</p>{details}</div>'''


def suspension_valuations_panel(data):
    if not isinstance(data,dict) or not data:
        return ''
    rows=[]
    for item in sorted(data.get('rows') or [],key=lambda item:str(item.get('day','')),reverse=True):
        verified=item.get('method')=='verified_suspension_prior_close' and item.get('tradable') is False
        method='沿用前收盘；禁止成交' if verified else '估值方法或禁止成交标记未核实'
        proofs=[]
        for proof in item.get('proofs') or []:
            source=safe_url(proof.get('source'))
            link=f'<a href="{esc(source)}" target="_blank" rel="noreferrer">停牌原文</a>' if source else '原文链接不可用'
            pages=proof.get('pages')
            if isinstance(pages,list):
                pages='、'.join(str(page) for page in pages)
            status='全日停牌' if proof.get('full_day') is True else '全日停牌未核实'
            proofs.append(f'<p>{esc(proof.get("symbol"))} · 证明日期 {esc(proof.get("day"))} · {status}<br>可获知日 {esc(proof.get("available_on"))} · 页码 {esc(pages)} · {link}</p>')
        evidence=''.join(proofs) or '逐日证据未记录'
        if len(proofs)>1:
            evidence=f'<details><summary>逐日证据 {len(proofs)} 项</summary>{evidence}</details>'
        cells=[item.get('day'),item.get('symbol'),item.get('quantity')]
        first=''.join(f'<td>{esc(value)}</td>' for value in cells)
        last=''.join(f'<td>{esc(value)}</td>' for value in [item.get('price_date'),number(item.get('market_value')),percent(item.get('weight')),item.get('stale_sessions')])
        rows.append(f'<tr>{first}<td>{esc(item.get("price"))}<br><small>{method}</small></td>{last}<td>{evidence}</td></tr>')
    if rows:
        headings=['估值日','公司代码','实际股数','估值价 / 元','原价格日','账面市值 / 元','占当日净值','陈旧交易日数','逐日证据']
        details='<div class="table-scroll" tabindex="0" role="region" aria-label="停牌账面估值，可左右滚动"><table><thead><tr>'+''.join(f'<th>{esc(value)}</th>' for value in headings)+'</tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>'
    elif data.get('rows')==[] and data.get('days')==0 and data.get('company_days')==0:
        details='<p>本版停牌沿用前收盘的持仓估值为 0 次。</p>'
    else:
        details='<p>估值明细未记录或与计数不一致，不能当作 0 次。</p>'
    return f'''<div id="suspension-valuations"><p><strong>历史主方案 · 停牌账面估值</strong></p>
    <p>涉及 {esc(data.get('days'))} 个交易日、{esc(data.get('company_days'))} 公司·日。每行是实际持仓在一个交易日采用的估值；公司·日不等于冻结表的公司·月，也不是订单或成交次数。</p>
    <p class="muted">沿用前收盘仅作持仓账面估值，不代表当日成交价或公允价值；禁止以此价格模拟当日成交。估值方法或禁止成交标记缺失、异常时显示未核实。原价格日是行情发生日期，证明可获知日是证据当时可用的日期，两者不同；不补画停牌日 K 线或买卖标记。</p>{details}</div>'''


def historical_overview(backtest):
    if not backtest:
        return ''
    protocol=backtest.get('protocol',{}); coverage=backtest.get('coverage',{})
    status=backtest.get('status','pending')
    metrics=backtest.get('primary_metrics',{}) if status in ('diagnostic','complete') else {}
    diagnostics=backtest.get('diagnostics',{}); months=diagnostics.get('candidate_months',{})
    latest=diagnostics.get('holding_freezes',{}).get('latest') or {}
    gaps=diagnostics.get('financial_gaps')
    gap_count=str(len(gaps)) if isinstance(gaps,list) else '未记录'
    observations=diagnostics.get('cashflow_policy',{}).get('nonpositive_latest_observations',[])
    warning=(f'<p class="notice" id="cashflow-warning">最新年度自由现金流非正：{len(observations)} 条历史评估记录。'
             '<a href="#cashflow-policy">查看金额与覆盖依据</a>；这是评估次数，不是现金闲置金额或原因分摊。</p>') if observations else ''
    blocked=f'<p class="notice">{esc(backtest.get("message") or "策略结果尚不可用，指标未补零。")}</p>' if status=='blocked' else ''
    return f'''<section class="panel" id="strategy-overview"><div class="section-title"><h2>历史主方案</h2><span class="pill">{esc(EXPERIMENT_STATES.get(status,status))}</span></div>
    <p class="muted">{esc(coverage.get('start',protocol.get('selection_day')))}—{esc(coverage.get('end',backtest.get('as_of')))} · 模拟本金 ¥ {number(backtest.get('initial_cash',protocol.get('account_config',{}).get('paper_cash')))} · 历史模拟，非收益承诺</p>{blocked}
    <div class="research-summary"><div><small>年化收益</small><strong>{percent(metrics.get('cagr'))}</strong><span>累计收益 {percent(metrics.get('total_return'))}</span></div><div><small>最大回撤</small><strong>{percent(metrics.get('max_drawdown'))}</strong><span>累计费用 ¥ {number(metrics.get('costs'))}</span></div><div><small>平均仓位</small><strong>{percent(metrics.get('average_exposure'))}</strong><span>按历史每日净值统计</span></div></div>
    <p id="capital-usage">月末评估 {esc(months.get('months'))} 次，其中 {esc(months.get('empty'))} 次无候选。<a href="#research-diagnostics">查看筛选与交易限制记录</a>；次数不等于闲置现金归因。</p>
    <p id="overview-data-gaps">历史字段缺口 {esc(gap_count)} 条 · 最近评估 {esc(latest.get('day'))}，冻结持仓 {esc(latest.get('held_frozen'))} 只。<a href="#research-diagnostics">查看未解决事项</a></p>{warning}
    <p><a href="#chart-panel">查看 K 线与操作</a> · <a href="#research-lab">当前观察名单</a></p></section>'''


def cashflow_policy_note(protocol, diagnostics):
    policy=protocol.get('cashflow_policy','latest_and_three_year')
    label={'latest_and_three_year':'最新年度自由现金流为正，且三年累计覆盖三年总分红。',
           'three_year_coverage':'仅要求三年累计自由现金流覆盖三年总分红；单年非正保留为风险提醒。'}.get(policy,'未识别现金流规则，须核对协议。')
    rows=diagnostics.get('cashflow_policy',{}).get('nonpositive_latest_observations',[])
    details=table(['评估日','公司','所属年度','最新年度自由现金流 / 元','当时状态','当期候选','三年自由现金流 / 元','三年总分红 / 元','原因'],[
        [row.get('day'),row.get('symbol'),row.get('cashflow_warning',{}).get('period'),number(row.get('cashflow_warning',{}).get('simple_fcf')),
         row.get('state'),'是' if row.get('active') is True else '否' if row.get('active') is False else '—',
         number(row.get('three_year_fcf')),number(row.get('three_year_dividend_cash')),row.get('cashflow_warning',{}).get('reason')]
        for row in rows]) if rows else ''
    return f'<div id="cashflow-policy"><p>现金流规则：{esc(label)}</p>{details}</div>'


def research_panel(research, backtest, chart_symbols=(), history='', registered_protocol=None):
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
    ranked=protocol.get('entry_mode')=='yield_rank'
    diagnoses=''
    if diagnostics or selection:
        states='；'.join(f'{state} {count} 次' for state,count in diagnostics.get('states',{}).items())
        entry=diagnostics.get('entry_funnel',[])
        funnel=''
        if entry:
            funnel='<div id="entry-funnel">'+table(['当期候选顺序筛选','公司·月次数'],[
                ['排序候选' if ranked and item.get('stage')=='全部买入条件通过' else item.get('stage'),item.get('count')] for item in entry])
            funnel+='<p class="muted">仅统计当期候选，排除退池后继续跟踪的公司。各层是按顺序取交集，不表示单项条件的独立因果作用；合格候选次数不是订单或成交笔数。</p></div>'
        months=diagnostics.get('candidate_months',{})
        if months:
            funnel+=f'<p id="candidate-months" class="muted">月度评估 {esc(months.get("months"))} 次，其中 {esc(months.get("empty"))} 次无{"排序" if ranked else "买入"}候选；合格候选累计 {esc(months.get("total_candidates"))} 个公司·月。</p>'
        unused=diagnostics.get('unused_share_conflicts',[])
        indicators=diagnostics.get('unused_indicator_conflicts',[])
        conflicts=''
        if unused:
            conflicts='<div id="unused-share-conflicts"><p><strong>未参与计算的季度股数冲突 · 原文待核实</strong></p>'+table(
                ['公司','冲突报告期','字段','评估月次数','其中当期候选月次数'],
                [[item.get(key) for key in ('symbol','period','field','count','active_count')] for item in unused])
            blocking_fields='参与计算的其他字段' if indicators else '其他字段'
            conflicts+=f'<p class="muted">每行按公司、冲突报告期、评估月去重；同股不同报告期可分别计数，不能当作独立公司数或成交笔数。这些季度股数字段未参与本次数值计算，原文尚未核实；年报股数及{blocking_fields}冲突仍按阻断处理。</p></div>'
        if indicators:
            conflicts+='<div id="unused-indicator-conflicts"><p><strong>未参与计算的财务指标 ROE 冲突 · 原文待核实</strong></p>'+table(
                ['公司','冲突报告期','字段','评估月次数','其中当期候选月次数'],
                [[item.get(key) for key in ('symbol','period','field','count','active_count')] for item in indicators])
            conflicts+='<p class="muted">每行按公司、冲突报告期、评估月去重；同股不同报告期可分别计数，不能当作独立公司数或成交笔数。该财务指标 ROE 未用于本策略计算，冲突值未知待核实；来源变体保留在研究数据中，原文尚未核实。</p></div>'
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
        {funnel}{pool_table}{holding_freezes_panel(diagnostics.get('holding_freezes'))}{suspension_valuations_panel(diagnostics.get('suspension_valuations'))}{conflicts}{disclosure_diagnostics_panel(diagnostics)}<p class="muted">建池淘汰按{'公司 / 建池时点' if rebalances else '公司'}计数，筛选按公司 / 时点计数，执行跳过按事件计数，三者不能相加。数据不足、未达到价格要求和资金不够买一手是不同原因；不能为了产生交易把它们视为通过。</p>{reasons}</details>'''
    limitations=research.get('limitations',[])+backtest.get('limitations',[])
    notes=''.join(f'<li>{esc(note)}</li>' for note in dict.fromkeys(limitations) if note!=backtest.get('protocol',{}).get('monthly_targets'))
    backtest_state=backtest.get('status') or research.get('backtest',{}).get('status') or '待运行'
    backtest_state=EXPERIMENT_STATES.get(backtest_state,backtest_state)
    historical_count=backtest.get('coverage',{}).get('stocks')
    historical_note=f'历史涉及 {historical_count} 只；与当日模拟账户分开记录' if historical_count is not None else '与当日模拟账户分开记录'
    retention=percent(protocol.get('retention_yield'))
    holding_note=f'<span>持有门槛 {retention} / 仅月末检查</span>' if retention!='—' else ''
    earnings_label=earnings_basis_label(protocol)
    earnings_card=f'<span>盈利口径：{esc(earnings_label)}</span>' if backtest else ''
    earnings_formula={'conservative_minimum':'取近三年较低利润中位数、最近年度较低利润、归母 TTM、扣非 TTM 四者最低值。',
                      'ttm':'只取归母 TTM 与扣非 TTM 较低值，不再与历史年度利润取更低值。'}.get(protocol.get('earnings_basis','conservative_minimum'),'协议中的盈利口径无法识别。')
    earnings_note=f'<p id="earnings-basis">历史实验盈利口径：{esc(earnings_label)}。{esc(earnings_formula)}这是机械口径对照，不等同于正常化盈利，也不代表收益已验证。</p>' if backtest else ''
    entry_card=f'<span>入场方式：{esc(entry_mode_label(protocol))}</span>' if backtest else ''
    freeze_card=f'<span>冻结政策：{esc(freeze_policy_label(protocol))}</span>' if backtest else ''
    cash_card='<span>股息估计：已批准年度现金估计</span>' if backtest and protocol.get('dividend_policy')=='approved_annual_cash' else ''
    entry_note=('<p class="muted" id="entry-mode">历史实验按股息排序：每月末对通过财务、估值、流动性及历史 TTM 股息率至少 3% 初筛的候选，按预计股息率降序分配。最多持仓 20 只，继续受资金和行业上限约束。5% 只作情景计算参考，不作为入场条件或买入限价上限；日常模拟规则未随此实验改变。</p>' if ranked else '')
    target_note=f'<p>回放调仓规则：{esc(protocol["monthly_targets"])}</p>' if protocol.get('monthly_targets') else ''
    message=backtest.get('message') or research.get('backtest',{}).get('message') or research.get('message','')
    return f'''<section class="panel research-lab" id="research-lab"><div class="section-title"><h2>策略研究台</h2><span class="pill">手动运行 · 不启用定时任务</span></div>
    <p>当前观察名单 {len(stocks)} / {esc(research.get('target_count',20))} 只 · 研究截至 {esc(research.get('as_of'))}。研究对象不等于持仓数量。</p>
    <div class="filters"><label>搜索全部研究股票<input id="research-search" type="search" placeholder="公司、代码、行业或待核实问题"></label><span class="muted" id="research-count" aria-live="polite"></span></div>
    <p class="research-table-hint muted">表格可左右滑动，右侧查看研究依据与 K 线。</p><div class="table-scroll"><table id="research-table"><thead><tr><th>公司</th><th>估值</th><th>流动性</th><th>情景股息率</th><th>分红 / 综合状态</th><th>查看</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
    <p id="research-empty" class="muted" hidden>没有匹配的研究对象。</p><div class="research-pagination"><button id="research-prev" type="button">上一页</button><span id="research-page" aria-live="polite"></span><button id="research-next" type="button">下一页</button></div>
    {comparison_chart(backtest) if backtest else ''}{history}{coverage_panel(backtest.get('coverage',{}))}{diagnoses}{period_diagnostics_panel(backtest.get('period_diagnostics',{})) if backtest.get('status')!='blocked' else ''}<details class="research-results"><summary>策略规则、来源与完整指标</summary><p>研究目标：年化 ≥ 10%；待验证，不是收益承诺。{esc(backtest_state)} · {esc(historical_note)}</p><p>研究版本 {esc(research.get('version'))} · 历史版本 {esc(protocol.get('version'))}。{esc(research.get('method',''))}</p><p>{esc(message)}</p><p>{holding_note}{earnings_card} {entry_card} {freeze_card} {cash_card}</p>{entry_note}{earnings_note}{freeze_policy_note(protocol) if backtest else ''}{capital_policy_note(protocol,registered_protocol) if backtest else ''}{valuation_policy_note(protocol,registered_protocol) if backtest else ''}{exit_policy_note(protocol,registered_protocol) if backtest else ''}{rebalance_policy_note(protocol,registered_protocol) if backtest else ''}{approved_cash_note(protocol,diagnostics)}{cashflow_policy_note(protocol,diagnostics) if backtest else ''}{target_note}{comparisons}<ul>{notes}</ul><p class="muted">平均仓位指每天投资市值占总资产比例的平均值；现金也计入收益分母。单个区间超过 10% 不代表目标已验证；还需独立区间、交易成本、退市与数据时点检查。历史结果不能保证未来收益。</p></details></section>'''


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
    mode=summary.get('protocol',{}).get('entry_mode','yield_threshold')
    if mode not in ENTRY_MODES or latest['protocol'].get('entry_mode','yield_threshold')!=mode:
        return {}
    dividend_policy=summary.get('protocol',{}).get('dividend_policy','implemented_only')
    if dividend_policy not in ('implemented_only','approved_annual_cash') or latest['protocol'].get('dividend_policy','implemented_only')!=dividend_policy:
        return {}
    cashflow_policy=summary.get('protocol',{}).get('cashflow_policy','latest_and_three_year')
    if cashflow_policy not in ('latest_and_three_year','three_year_coverage') or latest['protocol'].get('cashflow_policy','latest_and_three_year')!=cashflow_policy:
        return {}
    capital_policy=summary.get('protocol',{}).get('capital_policy','fixed_amount')
    registered=record('protocol.json')
    valuation_policy=summary.get('protocol',{}).get('valuation_policy','historical_gate')
    exit_policy=summary.get('protocol',{}).get('exit_policy','review_freeze')
    rebalance_policy=summary.get('protocol',{}).get('rebalance_policy','monthly_only')
    if (rebalance_policy not in ('monthly_only','after_confirmed_sale')
            or latest['protocol'].get('rebalance_policy','monthly_only')!=rebalance_policy
            or registered and (registered.get('version')!=version or registered.get('rebalance_policy','monthly_only')!=rebalance_policy)
            or rebalance_policy=='after_confirmed_sale' and not registered):
        return {}
    if (exit_policy not in ('review_freeze','known_failure_exit')
            or latest['protocol'].get('exit_policy','review_freeze')!=exit_policy
            or registered and (registered.get('version')!=version or registered.get('exit_policy','review_freeze')!=exit_policy)
            or exit_policy=='known_failure_exit' and not registered):
        return {}
    if (valuation_policy not in ('historical_gate','historical_context')
            or latest['protocol'].get('valuation_policy','historical_gate')!=valuation_policy
            or registered and (registered.get('version')!=version or registered.get('valuation_policy','historical_gate')!=valuation_policy)
            or valuation_policy=='historical_context' and not registered):
        return {}
    if (capital_policy not in ('fixed_amount','equity_ratio') or latest['protocol'].get('capital_policy','fixed_amount')!=capital_policy
            or registered and (registered.get('version')!=version or registered.get('capital_policy','fixed_amount')!=capital_policy)):
        return {}
    ratios=capital_ratios(registered) if capital_policy=='equity_ratio' else None
    if capital_policy=='equity_ratio' and ratios is None:
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
    chart_runs=([('rank','股息排序 · 全期','本次历史 股息排序 · 主方案')] if mode=='yield_rank' else
                [(rate,f'{int(rate)}%股息门槛 · 全期',f'本次历史 {int(rate)}%'+(' · 主方案' if rate=='005' else '')) for rate in ('004','005','006')])
    for rate,label,display in chart_runs:
        if not any(run.get('label')==label and run.get('status') in ('diagnostic','complete') for run in summary.get('runs',[])):
            continue
        result=record(f'yield-{rate}-result.json')
        if (result.get('start')!=coverage.get('start') or result.get('end')!=coverage.get('end')
                or not isinstance(result.get('plans'),list) or not isinstance(result.get('trades'),list)):
            continue
        assumptions=result.get('assumptions',{})
        if (not isinstance(assumptions,dict) or assumptions.get('capital_policy','fixed_amount')!=capital_policy
                or assumptions.get('rebalance_policy','monthly_only')!=rebalance_policy):
            continue
        if capital_policy=='equity_ratio':
            try:
                if (dec(result.get('initial_cash'),positive=True)!=money(registered['account_config']['paper_cash'])
                        or any(not isinstance(assumptions.get(key),str) or dec(assumptions[key],positive=True)!=ratio
                               for key,ratio in zip(('position_limit_ratio','sector_limit_ratio'),ratios))):
                    continue
            except AppError:
                continue
        elif any(key in assumptions for key in ('position_limit_ratio','sector_limit_ratio')):
            continue
        runs.append({'id':rate,'label':display,
                     'plans':result['plans'],'fills':result['trades'],'stock_events':chart_stock_events(result.get('stock_events'))})
        if dividend_policy=='approved_annual_cash':
            runs[-1]['label']+=' · 已批准年度现金估计'
        if rebalance_policy=='after_confirmed_sale':
            runs[-1]['label']+=' · 卖出成交后一次补单'
    return {'version':version,'start':coverage.get('start'),'end':coverage.get('end'),'stocks':stocks,'names':names,'runs':runs,
            'dividend_notes':chart_dividend_notes(market.get('dividends',{}),coverage['end'])} if runs else {}


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
    sources='<option value="daily">日常模拟</option>'+''.join(f'<option value="{run["id"]}">{esc("历史主方案" if run["id"]=="rank" else run["label"])}</option>' for run in historical.get('runs',[]))
    payload={'stocks':stocks,'names':names,'as_of':report.get('as_of'),'assessments':assessments,'plans':automatic.get('chart_plans',[]),'fills':automatic.get('chart_fills',[]),'stock_events':chart_stock_events(automatic.get('account',{}).get('stock_events')),'notes':automatic.get('notes',{}),'historical':historical}
    encoded=json.dumps(payload,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return f'''<section class="panel" id="chart-panel"><div class="section-title"><h2>股票 K 线与自动模拟操作</h2><span class="muted">红涨绿跌 · 未复权日线</span></div>
    <div class="filters" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr))"><label>来源<select id="chart-source" style="width:100%;max-width:none">{sources}</select></label><label>股票<select id="chart-symbol">{options}</select></label><label>显示范围<select id="chart-range" style="width:100%;max-width:none"><option value="30">近 30 根</option><option value="60" selected>近 60 根</option><option value="120">近 120 根</option><option value="10000">全部已取数据</option></select></label><label>定位成交 / 送转<select id="chart-trade" style="width:100%;max-width:none"><option value="">最近行情</option></select></label></div><p class="muted" id="chart-context"></p>
    <p class="chart-legend">○ 信：生成订单的信号日　<span class="buy">▲ 买：模拟买入成交</span>　<span class="sell">▼ 卖：模拟卖出成交</span>　<span style="color:#79549b">■ 转：送转新增股</span><span id="chart-reference-legend">　虚线：基准情景参考价（在显示范围内时）</span></p>
    <div id="stock-chart" class="stock-chart" tabindex="0" aria-label="股票日 K 线；使用左右方向键查看各日详情"></div><p id="chart-tip" class="chart-tip" aria-live="polite"></p>
    <div id="chart-explanation" class="chart-explanation"></div><details><summary>数据来源与图表说明</summary><p id="chart-provenance" class="muted"></p><p class="muted">未复权便于核对成交原价，除权除息可能产生缺口；不把它解释为投资损失。图中只标所选来源的订单、成交及已记录送转；送转不是买入，不重复调整原始 K 线。尚无成交时没有买卖箭头。历史模式不套用今天的参考价，历史曾入池名单不等于当日观察名单。</p></details>
    <script type="application/json" id="market-chart-data">{encoded}</script></section>'''


def render(workspace, ledger, report, status=None):
    status = status or {"ok":True,"message":"最新运行完成"}
    source = report.get("source", "未运行")
    demo = source == "DEMO"
    research=optional_report(workspace/'research'/'latest.json') if not demo else {}
    backtest=optional_report(workspace/'backtests'/'latest.json') if not demo else {}
    version=backtest.get('protocol',{}).get('version','')
    registered_protocol=(optional_report(workspace/'backtests'/version/'protocol.json')
                         if isinstance(version,str) and re.fullmatch(r'historical-[a-z0-9]+(?:-[a-z0-9]+)*',version) else {})
    forecast=optional_report(workspace/'forecast-diagnostic.json') if not demo else {}
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
        annual = table(["年度","归母利润 / 万元","扣非利润 / 万元","经营现金 / 万元","资本支出 / 万元","简化 FCF / 万元","已公告现金 DPS / 元","剔除已知特别后 DPS","审计"],
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
        <h3>近年分红与支付能力</h3>{annual}{zero_cash_proof_panel(row.get('annual',[]))}{dividend_cash_panel(row.get('annual',[]))}{corrections}<p class="muted">财务金额按原始披露口径，派息总额估算另列股本依据。已公告现金 DPS 汇总已披露实施方案及核实的全年零分红；实施公告不等于已到账，核实全年零不生成支付事件。同年度中期与年度派息合计，不含未实施预案。只剔除有原文依据的已知特别分红，其余记录仍需核实。DPS 未统一历史股本口径，不能直接比较增速；空白不是零。金融企业不计算工业 FCF。</p>
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
        <h3>交易计划</h3>{plans}<details><summary>成交与现金流水</summary>{fills}{events}{stock_events_panel(data.get('stock_events'))}</details></section>''')
    rows = report.get('rows',[])
    style = (ROOT/'qmt/style.css').read_text(encoding='utf-8')
    chart_script = (ROOT/'qmt/chart.js').read_text(encoding='utf-8')
    research_script = (ROOT/'qmt/research-ui.js').read_text(encoding='utf-8')
    benchmark_script = (ROOT/'qmt/benchmark-ui.js').read_text(encoding='utf-8')
    page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NKG · 红利研究工作台</title><style>{style}</style></head>
    <body><main><header><a class="brand" href="#">NKG<span> / DIVIDEND LAB</span></a><span class="local">● 本地研究 · 人工执行</span></header>
    <section class="hero"><div><p class="eyebrow">DIVIDEND LAB</p><h1>红利策略研究</h1><p>查看历史表现，核对每笔操作。</p></div><div class="date-card"><small>行情截至</small><strong>{esc(report.get('as_of','尚未运行'))}</strong><span>{esc(source)} · 市场快照 {esc(report.get('pool_date','—'))}</span><span>生成于 {esc(report.get('generated_at','—'))}</span></div></section>
    <div class="notice {'error' if not status.get('ok') or demo else ''}">{'虚构演示数据，不可用于真实交易。 ' if demo else ''}{esc(status.get('message',''))}</div>
    {historical_overview(backtest)}
    {chart_panel(workspace,report,report.get('demo_operations',{}) if demo else automatic,research)}
    {research_panel(research,backtest,chart_symbols,experiment_history_panel(workspace) if not demo else '',registered_protocol)}
    {forecast_diagnostic_panel(forecast,backtest,registered_protocol)}
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
