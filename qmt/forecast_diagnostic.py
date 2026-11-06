"""Offline, paired next-fiscal-year diagnostics; never changes a trading rule or ledger."""
import hashlib
import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from .automatic import dividend_assessment
from .config import ROOT, AppError, atomic_json, date_key, dec
from .experiment import protocol
from .research import implemented_dividends, normalize_disclosed_dividends, summarize


METHODS = {'conservative_minimum': '保守盈利', 'ttm': 'TTM盈利', 'last_annual': '最近已知年度延续'}
ORIGINS = ('20221230', '20231229', '20241231', '20251231')
VERIFIED_ZERO = json.loads((Path(__file__).with_name('forecast_labels.json')).read_text(encoding='utf8'))
LIMITATIONS = [
    '下一完整财政年度是本诊断新约定；原研究模型是持续分红情景，并未承诺一年期预测。',
    '这些估计现在按历史原点重建，并非声称当年已事前发布；先冻结估计摘要，再读取后来结果标签。',
    '正现金派息的普通/特别分类及全年完整性均未逐项原文核齐；误差仅对已观察税前总现金代理计算。原文核实的全年零派息反例单列，不混入代理误差。',
    '代理需要年度正现金实施行且相关现金已到支付日。未原文核实的零元预案、只有中期实施或无记录均待核实，不补零；这使代理评分样本偏向继续分红者。',
    '代理合计可能含特别分红或未核实的累计重叠；不能把总现金代理当作持续普通分红真值。',
    '检测到原点至最后派息日的送转事件时不评分；未发现送转不等于配股、换股等全部股份权利变化已核齐。',
    '最近已知年度延续使用该年度普通现金估算总额除以原点总股数；保留年末股数代理误差，原点年末通常只能取得上一财政年度数据。',
    '三种估计使用同一原点候选、同一代理样本。候选是当时财务合格者，不按未来收益或未来买入结果选择。',
    '公告日期过滤不证明已取得历史原始版本；当前供应商修订及数据覆盖仍可能带来偏差。',
    '该历史区间已经查看，属于回溯诊断，不是独立未见数据验证；不得据此自动晋级交易规则或声称收益达到目标。',
]


def forecast(stock, dossier, origin, rules):
    """Use only origin-dated inputs; outcome data is deliberately not an argument."""
    row = summarize({**dossier, 'as_of': origin}, stock, stock['selection_quote'])
    expected = stock['selection_quality']['evidence_hash']
    if row.get('evidence_hash') != expected:
        raise AppError(f"{origin} {stock['symbol']} 财务证据与冻结候选不一致。")
    estimates = {}
    for method in ('conservative_minimum', 'ttm'):
        assessment = dividend_assessment(row, {**dossier, 'as_of': origin}, rules, earnings_basis=method)
        if assessment['state'] not in ('等待价格', '满足价格条件'):
            raise AppError(f"{origin} {stock['symbol']} 无法重现已冻结财务资格：{assessment['reason']}")
        estimates[method] = str(dec(assessment['estimated_eps']) * dec(assessment['payout']))
    annual = row['annual'][0]
    estimates['last_annual'] = str(dec(annual['estimated_ordinary_cash']) / dec(row['share_count'], positive=True))
    return {'symbol': stock['symbol'], 'name': stock['name'], 'origin': origin,
            'target_year': int(origin[:4]) + 1, 'price': str(dec(row['price'], positive=True)),
            'known_fiscal_year': int(annual['period'][:4]), 'evidence_hash': expected,
            'estimates_dps': estimates}


def observed_label(raw, origin, target_year, as_of, *, symbol=None):
    """Return an explicitly unverified cash proxy, never infer a zero from missing rows."""
    date_key(origin)
    date_key(as_of)
    target = str(target_year)
    result = {'state': '', 'reason': '', 'observed_cash_dps': None, 'events': [], 'stock_events': [],
              'zero_annual_proposals': [], 'ordinary_verified': False}

    def pending(state, reason):
        result.update(state=state, reason=reason)
        return result

    if as_of <= target + '1231':
        return pending('pending_year_incomplete', '目标财政年度尚未完整，不能把未来派息记作零。')
    try:
        normalized = normalize_disclosed_dividends(raw, as_of)
        # Limit validation to target fiscal cash and intervening stock actions, not unrelated old rows.
        relevant = [event for event in normalized
                    if any(c['end_date'].startswith(target) for c in event.get('fiscal_components', [event]))
                    or (event.get('stk_div') is not None and dec(event['stk_div']) > 0
                        and event.get('ex_date') and origin < event['ex_date'] <= as_of)]
        events = implemented_dividends(relevant, as_of, '19000101')
        components = [{**component, **{key: event.get(key) for key in
                      ('ann_date', 'imp_ann_date', 'record_date', 'ex_date', 'pay_date')}}
                      for event in events for component in event.get('fiscal_components', [event])
                      if component['end_date'].startswith(target) and dec(component['cash_div_tax']) > 0]
        result['events'] = components
        result['zero_annual_proposals'] = [r for r in raw if r.get('end_date') == target + '1231'
            and r.get('ann_date') and r['ann_date'] <= as_of and r.get('cash_div_tax') is not None
            and dec(r['cash_div_tax']) == 0 and r.get('stk_div') == 0]
        verified = [item for item in VERIFIED_ZERO if item['symbol'] == symbol and item['target_year'] == target_year
                    and date_key(item['available_on']) <= as_of]
        if verified:
            if len(verified) != 1:
                raise AppError('同一公司年度存在多条零派息核实登记。')
            proof = verified[0]
            target_rows = sorted((r for r in raw if r.get('end_date', '').startswith(target)), key=lambda r: json.dumps(r, sort_keys=True))
            fingerprint = hashlib.sha256(json.dumps(target_rows, sort_keys=True).encode()).hexdigest()
            url = urlparse(proof['source'])
            if (components or fingerprint != proof['target_rows_sha256'] or dec(proof['cash_dps']) != 0
                    or proof['available_on'] <= target + '1231' or url.scheme != 'https' or not url.hostname
                    or not re.fullmatch('[0-9a-f]{64}', proof['source_sha256'])
                    or type(proof['page']) is not int or proof['page'] < 1):
                raise AppError('零派息原文登记与现金事件、源指纹或日期不一致。')
            result.update(state='verified_zero_cash', observed_cash_dps='0', ordinary_verified=True,
                          source=proof['source'], source_sha256=proof['source_sha256'], page=proof['page'],
                          available_on=proof['available_on'], reason=proof['note'])
            return result
        if not any(c['end_date'] == target + '1231' for c in components):
            return pending('pending_zero_or_annual_confirmation', '未取得年度正现金实施行；零元预案或仅中期记录须核对原文。')
        if any(not c.get('pay_date') or c['pay_date'] > as_of for c in components):
            return pending('pending_payment', '目标年度相关现金仍有尚未到支付日的实施方案。')
        last_payment = max(c['pay_date'] for c in components)
        result['stock_events'] = [e for e in events if dec(e['stk_div']) > 0 and origin < e['ex_date'] <= last_payment]
        if result['stock_events']:
            return pending('pending_stock_actions', '原点至最后派息日存在送转，未直接相加不同股数基础的DPS。')
        result.update(state='proxy_total_cash', observed_cash_dps=str(sum((dec(c['cash_div_tax']) for c in components), Decimal(0))),
                      reason='已观察税前总现金代理：年度完整性、普通/特别分类及其他股份权利变化未原文核齐。')
        return result
    except (AppError, KeyError, ValueError, TypeError, AttributeError) as exc:
        return pending('pending_data_conflict', '分红资料无法安全汇总：' + str(exc))


def yearly_metrics(rows):
    """Equal weight each stock-year; compare all methods on the identical label intersection."""
    output = []
    for origin in ORIGINS:
        cohort = [r for r in rows if r['origin'] == origin]
        scored = [r for r in cohort if r['label']['state'] == 'proxy_total_cash']
        metrics = {}
        for method in METHODS:
            errors = [(dec(r['estimates_dps'][method]) - dec(r['label']['observed_cash_dps']))
                      / dec(r['price'], positive=True) * 10000 for r in scored]
            metrics[method] = {'count': len(errors),
                'bias_yield_bp': str(sum(errors) / len(errors)) if errors else None,
                'mae_yield_bp': str(sum(abs(e) for e in errors) / len(errors)) if errors else None,
                'over': sum(e > 0 for e in errors), 'under': sum(e < 0 for e in errors),
                'exact': sum(e == 0 for e in errors)}
        output.append({'origin': origin, 'target_year': int(origin[:4]) + 1, 'candidates': len(cohort),
                       'proxy_scored': len(scored), 'status_counts': dict(Counter(r['label']['state'] for r in cohort)),
                       'metrics': metrics})
    return output


def run_diagnostic(workspace, version):
    if Path(version).name != version or not version:
        raise AppError('实验版本必须是 backtests 下的单个目录名。')
    base = (workspace / 'backtests' / version).resolve()
    if base.parent != (workspace / 'backtests').resolve():
        raise AppError('实验目录超出当前工作目录。')
    hashes = {}

    def read(path):
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise AppError(f'无法读取冻结输入 {path.name}：{exc}') from None
        if not isinstance(value, dict):
            raise AppError(f'冻结输入 {path.name} 必须为JSON对象。')
        hashes[str(path.relative_to(base)).replace('\\', '/')] = hashlib.sha256(raw).hexdigest()
        return value

    saved = read(base / 'protocol.json')
    if saved.get('version') != version:
        raise AppError('源协议版本与实验目录名不一致。')
    if (saved.get('pool_refresh') != 'year_end' or saved.get('cohort_limit', 20) is not None
            or saved.get('earnings_basis') != 'conservative_minimum'):
        raise AppError('本诊断要求完整年度财务候选的保守口径实验，不能使用截断名单或按另一口径挑选样本。')
    if not isinstance(saved.get('account_config'), dict) or not isinstance(saved.get('rules'), dict):
        raise AppError('源协议缺少有效账户配置或冻结规则。')
    if saved.get('engine_sha256') != protocol(saved['account_config'])['engine_sha256']:
        raise AppError('当前计算引擎与源实验不一致；不能把重新计算冒充旧版冻结预测。')
    as_of = date_key(saved['replay_end'])
    parent = read(base / 'cohort.json')
    rebalances = parent['rebalances']
    if tuple(item['day'] for item in rebalances) != ORIGINS:
        raise AppError('本诊断固定使用2022至2025四个年末原点，不自动改选其他时期。')
    rows = []
    for rebalance in rebalances:
        origin = rebalance['day']
        folder = base / 'cohorts' / origin
        cohort, financials = read(folder / 'cohort.json'), read(folder / 'financials.json')
        codes = [stock['symbol'] for stock in cohort['stocks']]
        if len(codes) != len(set(codes)) or codes != rebalance['symbols'] or cohort['selected_on'] != origin:
            raise AppError('年度候选与父实验不一致或存在重复。')
        for stock in cohort['stocks']:
            rows.append(forecast(stock, financials[stock['symbol']], origin, saved['rules']))
    # Freeze the prediction digest before loading outcome dossiers; future labels cannot select candidates.
    forecast_hash = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    outcomes = read(base / 'financials.json')
    for row in rows:
        row['label'] = observed_label(outcomes[row['symbol']]['data']['dividend'], row['origin'], row['target_year'], as_of, symbol=row['symbol'])
    by_year = yearly_metrics(rows)
    scored = sum(y['proxy_scored'] for y in by_year)
    verified = [row for row in rows if row['label']['state'] == 'verified_zero_cash']
    result = {'version': 'dividend-forecast-v1', 'status': 'diagnostic', 'source_version': version,
              'outcome_as_of': as_of, 'horizon': '固定历史年末原点，估计下一完整财政年度归属的每股现金分红。',
              'methods': METHODS, 'forecast_sha256': forecast_hash, 'input_sha256': hashes,
              'source_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                  ('qmt/forecast_diagnostic.py', 'qmt/forecast_labels.json', 'qmt/research.py', 'qmt/automatic.py', 'qmt/disclosures.json')},
              'coverage': {'predictions': len(rows), 'proxy_scored': scored, 'verified_ordinary': len(verified),
                           'verified_zero': len(verified), 'pending': len(rows) - scored - len(verified)},
              'by_year': by_year, 'rows': rows, 'verified_zero_cases': verified, 'limitations': LIMITATIONS}
    atomic_json(workspace / 'forecast-diagnostic.json', result)
    return result
