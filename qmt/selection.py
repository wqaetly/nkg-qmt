"""Market-wide staged research; affordability is deliberately absent here."""
import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median

from .config import ROOT, AppError, date_key, dec, symbol_key


VERSION = 'value-dividend-v2'


def load_rules():
    rules = json.loads((ROOT/'automatic.json').read_text(encoding='utf-8'))
    if rules['version'] != VERSION or rules['benchmark'] != '515180.SH':
        raise AppError('自动模拟规则版本或比较基准无效。')
    if type(rules['max_holdings']) is not int or not 1 <= rules['max_holdings'] <= 5:
        raise AppError('自动模拟持股数量应为 1—5。')
    for key in ('cash_reserve','entry_yield','exit_yield','max_debt_ratio','order_price_buffer','max_pe_percentile'):
        if not 0 <= dec(rules[key]) < 1:
            raise AppError('自动模拟比例必须在 0 到 1 之间。')
    if not 0 < dec(rules['exit_yield']) < dec(rules['entry_yield']):
        raise AppError('买卖股息率门槛无效。')
    for key in ('min_historic_yield_pct','min_daily_amount','max_normalized_pe'):
        dec(rules[key],positive=True)
    if len(rules['research_watchlist']) > 5 or len(set(rules['research_watchlist'])) != len(rules['research_watchlist']):
        raise AppError('研究观察名单最多 5 只且不得重复。')
    for code in rules['research_watchlist']:
        symbol_key(code)
    return rules


def automatic_path(workspace):
    return workspace/'auto'/VERSION


def mainboard(code):
    return (code.endswith('.SH') and code.startswith(('600','601','603','605'))) or (code.endswith('.SZ') and code.startswith(('000','001','002','003')))


def positive(value):
    try:
        return dec(value,positive=True)
    except AppError:
        return None


def liquidity(amounts, sessions, rules):
    # Missing/suspended sessions stay in the denominator; daily.amount is thousand yuan.
    values = [amounts.get(day,Decimal(0)) for day in sessions]
    active = sum(v > 0 for v in values)
    typical = median(values)
    passed = len(sessions)==20 and active>=18 and values[-1]>0 and typical>=dec(rules['min_daily_amount'])
    return {'state':'通过' if passed else '未通过','median_amount':str(typical),'active_days':active,
            'sessions':len(sessions),'reason':f'近 {len(sessions)} 个交易日成交额中位数 {typical/10000:.0f} 万元，有成交 {active} 日；缺失/停牌日不剔除'}


def valuation(client, code, day, quote, peer_values, rules):
    result={'state':'待核实','pe_ttm':quote.get('pe_ttm'),'pb':quote.get('pb'),'peer_count':len(peer_values),
            'industry_median_pe':str(median(peer_values)) if peer_values else None,'reason':'PE、同行样本或历史估值不足'}
    current=positive(quote.get('pe_ttm'))
    if current is None or len(peer_values)<5:
        return result
    start=(datetime.strptime(day,'%Y%m%d')-timedelta(days=365*3)).strftime('%Y%m%d')
    rows=client.call('daily_basic',{'ts_code':code,'start_date':start,'end_date':day},'ts_code,trade_date,pe_ttm')
    seen=set(); history=[]
    for row in rows:
        stamp=date_key(row['trade_date'])
        if row['ts_code']!=code or stamp in seen or not start<=stamp<=day:
            raise AppError('历史估值代码、日期或重复记录异常。')
        seen.add(stamp)
        value=positive(row.get('pe_ttm'))
        if value is not None and stamp<day:
            history.append(value)
    result['history_samples']=len(history)
    if len(history)<250:
        return result
    percentile=(sum(v<current for v in history)+Decimal('.5')*sum(v==current for v in history))/len(history)
    passed=current<=median(peer_values) and percentile<=dec(rules['max_pe_percentile'])
    result.update(state='通过' if passed else '未通过',percentile=str(percentile),
                  reason=f'PE {current:.2f} / 同行业正 PE 中位数 {median(peer_values):.2f}；近三年正 PE 分位 {percentile:.1%}（{len(history)} 日）')
    return result


def select_universe(client, day, basics, prices, rules, workspace, progress):
    universe=sorted(code for code in basics if mainboard(code))
    if not universe:
        raise AppError('未取得沪深主板股票列表。')
    peers={}
    for code in universe:
        b=basics[code]; value=positive(prices.get(code,{}).get('pe_ttm'))
        if value and b.get('industry') and 'ST' not in b['name'] and '退' not in b['name']:
            peers.setdefault(b['industry'],[]).append(value)
    start=(datetime.strptime(day,'%Y%m%d')-timedelta(days=60)).strftime('%Y%m%d')
    sessions=client.calendar(start,day)[-20:]
    if len(sessions)!=20 or sessions[-1]!=day:
        raise AppError('流动性窗口不足 20 个交易日。')
    amounts={}
    for i,stamp in enumerate(sessions,1):
        rows=client.call('daily',{'trade_date':stamp},'ts_code,trade_date,amount')
        if len(rows)<len(prices)*.8 or len({r['ts_code'] for r in rows})!=len(rows):
            raise AppError('全市场成交额快照不足或重复，暂停筛选。')
        for row in rows:
            if row['trade_date']!=stamp:
                raise AppError('成交额快照日期异常。')
            amounts.setdefault(row['ts_code'],{})[stamp]=dec(row['amount'])*1000
        if i%5==0:
            progress(f'已读取流动性窗口 {i}/20 个交易日',flush=True)
    required=set(rules['research_watchlist'])
    ledger_path=automatic_path(workspace)/'ledger.sqlite3'
    if ledger_path.exists():
        with sqlite3.connect(ledger_path) as db:
            # Preserve every previously traded/targeted name for re-evaluation and corporate actions.
            required.update(r[0] for r in db.execute('SELECT DISTINCT symbol FROM plans'))
            row=db.execute("SELECT value FROM metadata WHERE key='automatic-targets'").fetchone()
            if row:
                required.update(json.loads(row[0])['targets'])
    checks={}; selected=[]; reviewed=0
    for code in sorted(set(universe)|required):
        b=basics.get(code,{}); quote=prices.get(code,{})
        peers_for_code=peers.get(b.get('industry'),[])
        liq=liquidity(amounts.get(code,{}),sessions,rules)
        pe=positive(quote.get('pe_ttm')); dy=positive(quote.get('dv_ttm'))
        coarse=bool(mainboard(code) and b and 'ST' not in b.get('name','') and '退' not in b.get('name','')
                    and pe and dy and dy>=dec(rules['min_historic_yield_pct']) and len(peers_for_code)>=5
                    and pe<=median(peers_for_code) and liq['state']=='通过')
        reason='进入历史估值复核' if coarse else '未通过初筛：主板非风险警示、正 PE 不高于同行中位数、历史股息率与多日流动性须同时满足'
        item={'symbol':code,'name':b.get('name',code),'liquidity':liq,'reason':reason,'coarse_pass':coarse}
        if coarse or code in required:
            reviewed+=1
            try:
                item['valuation']=valuation(client,code,day,quote,peers_for_code,rules)
            except AppError as exc:
                item['valuation']={'state':'待核实','reason':str(exc)}
            if item['valuation']['state']=='通过' or code in required:
                selected.append(code)
            item['reason']=item['valuation']['reason']
            if reviewed%20==0:
                progress(f'已复核历史估值 {reviewed} 家',flush=True)
        checks[code]=item
    progress(f'沪深主板 {len(universe)} 家；初筛通过 {sum(c["coarse_pass"] for c in checks.values())} 家；财报研究批次 {len(selected)} 家（含关注/既有计划）',flush=True)
    return selected,checks,len(universe)
