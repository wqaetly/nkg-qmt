import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from vendor.dividend_stock_research.dividend_price import calculate
from .config import ROOT, AppError, date_key, dec


DISCLOSURES = json.loads((ROOT/'qmt/disclosures.json').read_text(encoding='utf-8'))


def stock_distribution_terms(event, as_of):
    """Return source-verified listing terms only for an exact implementation row."""
    day=date_key(as_of)
    matches=[item for item in DISCLOSURES.get('stock_distributions',[])
             if item['expected']==event and date_key(item['available_on'])<=day]
    if not matches:
        return None
    if len(matches)!=1:
        raise AppError('送转股实施依据重复，不能选择一个版本入账。')
    item=matches[0]
    listed=date_key(item['stock_list_date']); available=date_key(item['available_on'])
    source=urlparse(item['source']); digest=item.get('source_sha256','')
    if (not max(date_key(event['ann_date']),date_key(event['imp_ann_date']))<=available<=date_key(event['ex_date'])
            or not date_key(event['record_date'])<date_key(event['ex_date'])<=listed
            or dec(event['stk_div'])<=0 or source.scheme!='https' or not source.hostname
            or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest)):
        raise AppError('送转股上市日、比例或公告依据无效，暂停入账。')
    return {'stock_list_date':listed,'source':item['source'],'available_on':available}


def disclosed_date(row):
    dates = [row[key] for key in ("ann_date", "f_ann_date") if row.get(key)]
    return max(date_key(day) for day in dates) if dates else "99999999"


def _verified_source(item):
    if not isinstance(item, dict):
        raise AppError('已核实披露依据必须是原文登记对象。')
    source = item.get('source', '')
    digest = item.get('source_sha256', '')
    pages = item.get('pages', '')
    try:
        parsed = urlparse(source) if isinstance(source, str) else None
    except ValueError:
        parsed = None
    if (not parsed or parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or not isinstance(digest, str) or len(digest) != 64
            or any(c not in '0123456789abcdef' for c in digest)
            or not isinstance(pages, str) or not pages.strip()):
        raise AppError('已核实披露缺少有效的原文链接、SHA256 或页码。')
    return {'source': source, 'source_sha256': digest, 'pages': pages,
            'available_on': date_key(item.get('available_on'))}


def _financial_zero_rows(rows, as_of):
    # Registration identity is separate from repeated copies of the same vendor row.
    identity = lambda row: tuple(row.get(k) for k in ('ts_code', 'end_date', 'ann_date', 'f_ann_date', 'report_type'))
    visible = [row for row in rows if disclosed_date(row) <= as_of and row.get('end_date', '99999999') <= as_of]
    balance_rows = [row for row in visible if any(k in row for k in ('st_borr', 'money_cap', 'total_assets', 'total_liab'))]
    if any('verified_zero_fields' in row for row in visible):
        raise AppError('原始财报不能自带已核实补零证据。')
    corrections = {}
    for item in DISCLOSURES.get('financial_zero_fields', []):
        expected = item.get('expected', {})
        if not any(row.get('ts_code') == expected.get('ts_code') for row in balance_rows):
            continue
        available = date_key(item.get('available_on'))
        if available > as_of:
            continue
        proof = _verified_source(item)
        if (item.get('field') != 'st_borr' or item.get('value') != '0'
                or 'st_borr' not in expected or expected['st_borr'] is not None
                or not expected.get('ts_code') or str(expected.get('report_type')) not in {'1', '4'}
                or not date_key(expected.get('end_date')) <= disclosed_date(expected) <= available):
            raise AppError('已核实财务补零登记的字段、原始行或公告时点无效。')
        key = identity(expected)
        if key in corrections:
            raise AppError('已核实财务补零登记重复。')
        corrections[key] = (expected, {**proof, 'field': 'st_borr', 'value': '0',
                                      'original': expected, 'note': item.get('note', '')})
    result = []
    for row in visible:
        correction = corrections.get(identity(row)) if row in balance_rows else None
        if correction:
            expected, proof = correction
            if json.dumps(row, sort_keys=True) != json.dumps(expected, sort_keys=True):
                raise AppError('已核实财务补零的原始行已变化，须重新核对原文。')
            row = {**row, 'st_borr': '0', 'verified_zero_fields': [json.loads(json.dumps(proof))]}
        result.append(row)
    return result


def verified_zero_cash_years(rows, symbol, as_of):
    """Return source-backed whole-FY zero decisions, never payment events."""
    day = date_key(as_of)
    result = {}

    def available(row):
        dates = ('ann_date', 'imp_ann_date') if row.get('div_proc') == '实施' else ('ann_date',)
        known = [date_key(row[k]) for k in dates if row.get(k)]
        if len(known) != len(dates) and (not known or max(known) <= day):
            raise AppError('全年零现金分红的原始行缺少必要公告日期，不能视为未来披露。')
        return max(known)

    for item in DISCLOSURES.get('zero_cash_years', []):
        if item.get('symbol') != symbol or date_key(item.get('available_on')) > day:
            continue
        proof = _verified_source(item)
        period = date_key(item.get('period'))
        if not period.endswith('1231') or period > proof['available_on'] or item.get('cash_dps') != '0':
            raise AppError('已核实全年零现金分红登记的年度、金额或时点无效。')
        if period in result:
            raise AppError('已核实全年零现金分红登记重复。')
        expected = item.get('expected')
        if not isinstance(expected, list) or not expected or any(not isinstance(r, dict) for r in expected):
            raise AppError('已核实全年零现金分红缺少完整原始行。')
        # Future rows cannot change a decision or its fingerprint before disclosure.
        expected = [r for r in expected if available(r) <= day]
        actual = [r for r in rows if r.get('ts_code') == symbol
                  and str(r.get('end_date', ''))[:4] == period[:4] and available(r) <= day]
        serialize = lambda r: json.dumps(r, sort_keys=True)
        if (not expected or not any(r.get('end_date') == period for r in expected)
                or any(r.get('ts_code') != symbol or date_key(r.get('end_date'))[:4] != period[:4] for r in expected)
                or len({serialize(r) for r in expected}) != len(expected)
                or sorted(map(serialize, actual)) != sorted(map(serialize, expected))):
            raise AppError('已核实全年零现金分红的可用原始行缺失、重复或已变化。')
        if any(r.get('cash_div_tax') is not None and dec(r['cash_div_tax']) != 0 for r in actual):
            raise AppError('已核实全年零现金分红与当时可见的现金方案冲突。')
        supports = [_verified_source(source) for source in item.get('supporting_sources', [])]
        if any(source['available_on'] > proof['available_on'] for source in supports):
            raise AppError('全年零现金分红决定不能使用决定日之后的辅证。')
        result[period] = {**proof, 'symbol': symbol, 'period': period, 'cash_dps': '0',
                          'originals': json.loads(json.dumps(sorted(actual, key=serialize))),
                          'supporting_sources': supports, 'note': item.get('note', '')}
    return result


def statements(rows, as_of, *, quarterly_share_conflicts=None):
    versions = {}
    for row in _financial_zero_rows(rows, date_key(as_of)):
        if disclosed_date(row) > as_of or row.get("end_date", "99999999") > as_of:
            continue
        if "report_type" in row and str(row["report_type"]) not in {"1", "4"}:
            continue
        period = date_key(row["end_date"])
        rank = (disclosed_date(row), str(row.get('report_type')) == '4')
        old = versions.get(period)
        if old is None or rank > old[0]:
            versions[period] = (rank, [row])
        elif rank == old[0]:
            old[1].append(row)
    selected = {}
    for period, (rank, candidates) in sorted(versions.items()):
        candidates = sorted(candidates, key=lambda row: json.dumps(row, sort_keys=True))
        merged, conflicts = {}, {}
        for key in sorted({key for row in candidates for key in row}):
            values = []
            for row in candidates:
                value = row.get(key)
                if value is not None and value not in values:
                    values.append(value)
            merged[key] = values[0] if len(values) == 1 else None
            if len(values) > 1:
                conflicts[key] = sorted(values, key=lambda value: json.dumps(value, sort_keys=True))
        # Quarterly balance shares are unused; never select either conflicting value.
        if quarterly_share_conflicts is not None and not period.endswith('1231') and 'total_share' in conflicts:
            quarterly_share_conflicts.append({'api':'balancesheet','period':period,'ann_date':rank[0],
                'field':'total_share','values':conflicts.pop('total_share'),
                'originals':[dict(row) for row in candidates]})
        if conflicts:
            raise AppError(f"{period} 同期财报版本冲突（{', '.join(sorted(conflicts))}），需核对原文。")
        selected[period] = merged
    return selected


def normalize_stock_only(rows, as_of):
    result=[]
    for row in rows:
        correction=next((c for c in DISCLOSURES['stock_only'] if c['available_on']<=as_of and row==c['expected']),None)
        if correction:
            result.append({**row,'cash_div_tax':'0','event_kind':'stock_only',
                           'stock_list_date':correction['stock_list_date'],
                           'correction_source':correction['source'],'correction_source_pages':correction['pages'],
                           'correction_available_on':correction['available_on'],'correction_note':correction['note'],
                           'correction_originals':[dict(row)],
                           'correction_fingerprint':hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest()})
        else:
            result.append(row)
    return result


def normalize_cash_overlaps(rows, as_of):
    for correction in DISCLOSURES['cash_dividend_overlaps']:
        if correction['available_on']>as_of:
            continue
        total=correction['expected'][0]
        event=[r for r in rows if all(r.get(k)==total[k] for k in ('ts_code','record_date','div_proc'))]
        # Only the verified total and its exact component can be merged.
        if (total not in event or len(event) not in (1,2) or any(r not in correction['expected'] for r in event)
                or len({json.dumps(r,sort_keys=True) for r in event})!=len(event)):
            continue
        originals=sorted((dict(r) for r in event),key=lambda r:r['end_date'])
        normalized={**total,'event_kind':'cash_overlap',
                    'ordinary_cash_div_tax':correction['ordinary_cash_div_tax'],
                    'special_cash_div_tax':correction['special_cash_div_tax'],
                    'correction_source':correction['source'],'correction_source_pages':correction['pages'],
                    'correction_available_on':correction['available_on'],'correction_note':correction['note'],
                    'correction_originals':originals,
                    'correction_fingerprint':hashlib.sha256(json.dumps(originals,sort_keys=True).encode()).hexdigest()}
        rows=[r for r in rows if r not in event]+[normalized]
    return rows


def normalize_cash_joint_periods(rows, as_of):
    for correction in DISCLOSURES['cash_joint_periods']:
        if correction['available_on']>as_of:
            continue
        expected=correction['expected']
        event=[r for r in rows if all(r.get(k)==expected[0][k] for k in ('ts_code','record_date','div_proc'))
               and max(r.get('ann_date') or '99999999',r.get('imp_ann_date') or '99999999')<=as_of]
        if not event or (len(event)==1 and event[0].get('event_kind')=='cash_joint_periods'):
            continue
        order=lambda r:(str(r.get('end_date') or ''),json.dumps(r,sort_keys=True))
        originals=sorted((dict(r) for r in event),key=order)
        if originals!=sorted(expected,key=order):
            raise AppError('联合派息原始记录不完整或与已核实公告不一致。')
        components=[dict(c) for c in correction.get('fiscal_components',[
            {'end_date':r['end_date'],'cash_div_tax':str(dec(r['cash_div_tax']))} for r in originals])]
        periods=[date_key(c['end_date']) for c in components]
        if not components or len(set(periods))!=len(periods) or set(periods)!={r['end_date'] for r in originals}:
            raise AppError('联合派息财政组成项缺失或重复。')
        for component in components:
            if 'special_cash_div_tax' in component and 'ordinary_cash_div_tax' not in component:
                raise AppError('已核实特别派息组成项须明确普通部分，不能自行推定分类。')
            cash,_=_dividend_amounts(component)
            if {dec(r['cash_div_tax']) for r in originals if r['end_date']==component['end_date']}!={cash}:
                raise AppError('联合派息财政组成项与原始金额不一致。')
            if 'distribution_shares' in component:
                _distribution_shares(component['distribution_shares'])
        # Keep one cash payment, but never attribute the combined amount to one fiscal period.
        normalized={**originals[-1],'event_kind':'cash_joint_periods',
                    'cash_div_tax':str(sum((dec(c['cash_div_tax']) for c in components),Decimal(0))),
                    'fiscal_components':components,
                    'correction_source':correction['source'],'correction_source_pages':correction['pages'],
                    'correction_available_on':correction['available_on'],'correction_note':correction['note'],
                    'correction_originals':originals,
                    'correction_fingerprint':hashlib.sha256(json.dumps(originals,sort_keys=True).encode()).hexdigest()}
        for key in ('source_sha256','component_sources'):
            if key in correction:
                normalized['correction_'+key]=json.loads(json.dumps(correction[key]))
        rows=[r for r in rows if r not in event]+[normalized]
    return rows


def normalize_disclosed_dividends(rows, as_of):
    """Source-verified events only; unknown conflicts still fail closed."""
    rows=normalize_cash_joint_periods(normalize_cash_overlaps(normalize_stock_only(rows,as_of),as_of),as_of)
    if as_of<'20230707':
        return rows
    event=[r for r in rows if r.get('ts_code')=='600600.SH' and r.get('record_date')=='20230713' and r.get('div_proc')=='实施']
    if not event:
        return rows
    expected={('20221231','20230617',Decimal('1.8')),('20230323','20230323',Decimal('.5'))}
    signatures={(r.get('end_date'),r.get('ann_date'),dec(r.get('cash_div_tax'))) for r in event}
    if (len(event) not in (1,2) or len(signatures)!=len(event) or not signatures<=expected
            or ('20221231','20230617',Decimal('1.8')) not in signatures or any(r.get('ex_date')!='20230714' or r.get('pay_date')!='20230714'
            or r.get('imp_ann_date')!='20230707' or dec(r.get('stk_div'))!=0 for r in event)):
        return rows
    total=next(r for r in event if r['end_date']=='20221231')
    normalized={**total,'ordinary_cash_div_tax':'1.30','special_cash_div_tax':'0.50',
                'correction_source':'https://file.finance.sina.com.cn/hkdata98/hk/hk_realtime_announcement/news/20230706/10809161-0.PDF',
                'correction_note':'公司 2023-022 实施公告：合计 1.80 已含特别股息 0.50；不可重复加总',
                'correction_originals':event}
    return [r for r in rows if r not in event]+[normalized]


def implemented_dividends(rows, as_of, earliest_period=None):
    events = {}
    earliest_period=date_key(earliest_period) if earliest_period else f"{int(as_of[:4])-8}0101"
    for row in normalize_disclosed_dividends(rows,as_of):
        if row.get("div_proc") != "实施":
            continue
        if row.get("end_date", "") < earliest_period:
            continue
        published = max(row.get("ann_date") or "99999999", row.get("imp_ann_date") or "99999999")
        if published > as_of:
            continue
        if 'distribution_shares' in row:
            raise AppError('派息股数只能来自已核实的财政组成项，不能信任原始事件夹带的股数。')
        for field in ("record_date", "ex_date", "end_date"):
            date_key(row.get(field))
        if row.get('event_kind')=='cash_joint_periods' or 'fiscal_components' in row:
            originals=row.get('correction_originals',[])
            if not originals or normalize_cash_joint_periods(originals,as_of)!=[row]:
                raise AppError('联合派息归并不匹配已核实原始记录。')
        if row.get('event_kind')=='cash_overlap':
            originals=row.get('correction_originals',[])
            if not originals or normalize_cash_overlaps(originals,as_of)!=[row]:
                raise AppError('现金分红归并不匹配已核实原始记录。')
        stock_only=row.get('event_kind')=='stock_only'
        if stock_only:
            originals=row.get('correction_originals',[])
            if len(originals)!=1 or normalize_stock_only(originals,as_of)!=[row]:
                raise AppError('纯送转事件修正不匹配已核实原始记录。')
            if (row.get('pay_date') is not None or dec(row.get('cash_div_tax'))!=0 or dec(row.get('stk_div'))<=0
                    or row.get('correction_available_on','99999999')>as_of or not row.get('correction_fingerprint')):
                raise AppError('纯送转事件缺少已核实原文依据，不能补造现金派息。')
            date_key(row.get('stock_list_date'))
            if row['stock_list_date']<row['ex_date']:
                raise AppError('送转股上市日期不得早于除权日期。')
        else:
            date_key(row.get('pay_date'))
        if not row["record_date"] < row["ex_date"] or (not stock_only and row['ex_date']>row['pay_date']):
            raise AppError("分红登记、除息、派息日期关系异常。")
        dec(row.get("cash_div_tax"))
        dec(row.get("stk_div"))
        key = row["record_date"] + ":" + row["ex_date"]
        old = events.get(key)
        if old and max(old.get("ann_date") or "", old.get("imp_ann_date") or "") == published:
            if any(old.get(k) != row.get(k) for k in ('end_date','record_date','ex_date','pay_date','cash_div_tax','stk_div')):
                raise AppError("同一分红事件存在冲突版本。")
            continue
        if not old or published > max(old.get("ann_date") or "", old.get("imp_ann_date") or ""):
            events[key] = row
    return sorted(events.values(), key=lambda row: row["ex_date"])


def signed(value):
    if value is None:
        raise AppError("财务关键字段缺失，不能当成零。")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise AppError('财务关键字段不是有效数字。') from None
    if not result.is_finite():
        raise AppError("财务字段不是有限数。")
    return result


def _distribution_shares(value):
    shares=dec(value,positive=True)
    if shares!=shares.to_integral_value():
        raise AppError('已核实派息股数必须为正整数。')
    return shares


def _dividend_amounts(component):
    cash=dec(component['cash_div_tax'])
    ordinary=dec(component.get('ordinary_cash_div_tax',component['cash_div_tax']))
    special=dec(component['special_cash_div_tax']) if 'special_cash_div_tax' in component and 'ordinary_cash_div_tax' in component else None
    if ordinary<0 or ordinary>cash or (special is not None and ordinary+special!=cash):
        raise AppError('普通、特别与合计每股分红金额不一致。')
    return cash,ordinary


def annual_dividend_cash(events, balances, earliest_period=None):
    """Aggregate validated events into fiscal-year estimates, retaining every component's basis.

    Amounts are unrounded yuan, shares are individual shares. Unknown proxy shares make
    the entire year's cash estimate unknown; all component evidence remains visible.
    """
    earliest_period=date_key(earliest_period) if earliest_period is not None else '19000101'
    years={}
    for event in events:
        if 'distribution_shares' in event:
            raise AppError('未核实的事件级派息股数不能用于估算。')
        if event.get('event_kind')=='cash_joint_periods' or 'fiscal_components' in event:
            originals=event.get('correction_originals',[])
            available=event.get('correction_available_on')
            if not originals or not available or normalize_cash_joint_periods(originals,date_key(available))!=[event]:
                raise AppError('联合派息组成项或股数依据未通过原文登记校验。')
            components=event['fiscal_components']
        else:
            components=[event]
        for component in components:
            end_date=date_key(component['end_date'])
            if end_date<earliest_period:
                continue
            period=end_date[:4]+'1231'
            cash,ordinary=_dividend_amounts(component)
            verified='distribution_shares' in component
            balance=balances.get(period,{})
            if verified:
                shares=_distribution_shares(component['distribution_shares'])
            else:
                try:shares=dec(balance.get('total_share'),positive=True)
                except AppError:shares=None
            basis={'end_date':end_date,'record_date':event['record_date'],'ex_date':event['ex_date'],
                   'cash_div_tax':str(cash),'ordinary_cash_div_tax':str(ordinary),
                   'shares':str(shares) if shares is not None else None,
                   'basis':'verified_distribution_shares' if verified else 'year_end_proxy',
                   'share_period':None if verified else period,
                   'source':event['correction_source'] if verified else None,
                   'available_on':event['correction_available_on'] if verified else
                                  disclosed_date(balance) if balance and disclosed_date(balance)!='99999999' else None,
                   'estimated_dividend_cash':str(cash*shares) if shares is not None else None,
                   'estimated_ordinary_cash':str(ordinary*shares) if shares is not None else None}
            years.setdefault(period,[]).append(basis)
    result={}
    for period,bases in sorted(years.items()):
        total_dps=sum((dec(b['cash_div_tax']) for b in bases),Decimal(0))
        ordinary_dps=sum((dec(b['ordinary_cash_div_tax']) for b in bases),Decimal(0))
        total_cash=ordinary_cash=None
        if all(b['shares'] is not None for b in bases):
            proxy=[b for b in bases if b['basis']=='year_end_proxy']
            verified=[b for b in bases if b['basis']=='verified_distribution_shares']
            # Preserve the original Decimal operation order for companies using only year-end proxies.
            proxy_shares=dec(proxy[0]['shares']) if proxy else Decimal(0)
            total_cash=sum((dec(b['cash_div_tax']) for b in proxy),Decimal(0))*proxy_shares
            ordinary_cash=sum((dec(b['ordinary_cash_div_tax']) for b in proxy),Decimal(0))*proxy_shares
            total_cash+=sum((dec(b['estimated_dividend_cash']) for b in verified),Decimal(0))
            ordinary_cash+=sum((dec(b['estimated_ordinary_cash']) for b in verified),Decimal(0))
        result[period]={'implemented_dps':str(total_dps),'ordinary_dps':str(ordinary_dps),
                        'estimated_dividend_cash':str(total_cash) if total_cash is not None else None,
                        'estimated_ordinary_cash':str(ordinary_cash) if ordinary_cash is not None else None,
                        'dividend_cash_basis':bases}
    return result


def summarize(dossier, basic, quote):
    as_of = dossier["as_of"]
    row = {"symbol": dossier["symbol"], "name": basic.get("name", "身份待核实"),
           "industry": basic.get("industry") or "待核实", "as_of": as_of,
           "price": quote.get("close"), "historic_yield_pct": quote.get("dv_ttm"),
           "share_count": str(dec(quote["total_share"]) * 10000) if quote.get("total_share") else None,
           "status": "待核实", "issues": list(dossier["errors"].values()), "data_warnings": [], "annual": [],
           "latest_period": None, "scenarios": [], "gate": False}
    if not basic or not quote or quote.get("trade_date") != as_of:
        row["issues"].append("身份或当日收盘行情缺失，暂停价格结论")
    if "ST" in row["name"] or "退" in row["name"]:
        row["status"] = "排除"
        row["issues"].append("风险警示/退市标识，不进入首版计划")
    try:
        def checked(api):
            try:
                return statements(dossier['data'].get(api,[]),as_of,
                                  quarterly_share_conflicts=row['data_warnings'] if api=='balancesheet' else None)
            except AppError as exc:
                row['issues'].append(api+'：'+str(exc))
                return {}
        inc,cf,bs,ind,audit = [checked(api) for api in ('income','cashflow','balancesheet','fina_indicator','fina_audit')]
        try:
            events = implemented_dividends(dossier["data"].get("dividend", []), as_of)
        except AppError as exc:
            row['issues'].append('分红记录：'+str(exc))
            events = []
        try:
            zero_cash_years = verified_zero_cash_years(dossier['data'].get('dividend', []), row['symbol'], as_of)
        except AppError as exc:
            row['issues'].append('全年零现金分红核实：'+str(exc))
            zero_cash_years = {}
        row["events"] = events
        latest = inc[max(inc)] if inc else {}
        row["latest_period"] = latest.get("end_date")
        row["latest_profit"] = latest.get("n_income_attr_p")
        row["financial_company"] = str(latest.get("comp_type")) in {"2", "3", "4", "7"}
        if row["financial_company"]:
            row["issues"].append("金融企业：需单独核查监管资本、资产质量及分配限制，不套用工业自由现金流规则")
        if not latest:
            row["issues"].append("缺少合并利润表")
        elif signed(latest.get("n_income_attr_p")) <= 0:
            row["status"] = "排除"
            row["issues"].append("最新累计归母利润非正，普通盈利分红模型不适用")
        if latest:
            period = latest["end_date"]
            if (datetime.strptime(as_of, "%Y%m%d") - datetime.strptime(period, "%Y%m%d")).days > 200:
                row["issues"].append("最新利润表报告期过旧")
            row["latest_announcement"] = disclosed_date(latest)
            if any(period not in records for records in (cf,bs,ind)):
                row['issues'].append('最新报告期的利润、现金流、资产负债或扣非数据未能对齐')
        annual = sorted((p for p in inc if p.endswith("1231")), reverse=True)[:8]
        if len(annual) < 5:
            row["issues"].append("完整年度利润记录不足 5 年")
        # shortcut: unclassified payouts remain ordinary proxies; verified shares do not verify sustainability.
        dividend_years=annual_dividend_cash(events,bs,f"{int(as_of[:4])-8}0101")
        for period, proof in zero_cash_years.items():
            if dec(dividend_years.get(period, {}).get('implemented_dps', '0')) != 0:
                raise AppError('全年零现金分红原文与已实施现金事件冲突。')
            dividend_years[period] = {**dividend_years.get(period, {'dividend_cash_basis': []}),
                                      'implemented_dps': '0', 'ordinary_dps': '0',
                                      'estimated_dividend_cash': '0', 'estimated_ordinary_cash': '0',
                                      'zero_cash_proof': proof}
        for period in annual:
            profit = signed(inc[period].get("n_income_attr_p"))
            cash = cf.get(period, {}).get("n_cashflow_act")
            capex = cf.get(period, {}).get("c_pay_acq_const_fiolta")
            free = None if cash is None or capex is None or row["financial_company"] else str(signed(cash) - signed(capex))
            item = {"period": period, "profit": str(profit), "deducted_profit": ind.get(period, {}).get("profit_dedt"),
                    "operating_cash": cash, "capex": capex, "simple_fcf": free,
                    **dividend_years.get(period,{'implemented_dps':None,'ordinary_dps':None,
                        'estimated_dividend_cash':None,'estimated_ordinary_cash':None,'dividend_cash_basis':[]}),
                    "audit": audit.get(period, {}).get("audit_result"),
                    "ann_date": disclosed_date(inc[period])}
            row["annual"].append(item)
        if any(item["implemented_dps"] is None for item in row["annual"][:5]):
            row["issues"].append("近五年存在未取得已实施分红的年度，须区分停派、尚未实施和数据缺口")
        if any(item.get('zero_cash_proof') for item in row['annual'][:5]):
            row['issues'].append('近五年存在原文核实的全年零现金分红，不满足连续现金分红规则')
        if any(item['implemented_dps'] is not None and dec(item['implemented_dps'])==0
               and not item.get('zero_cash_proof') for item in row['annual'][:5]):
            row['issues'].append('近五年存在仅已实施零现金的年度记录，不能据此确认全年零派息；纯送转不算持续现金股息')
        if row["annual"]:
            newest = row["annual"][0]
            if newest["simple_fcf"] is not None and signed(newest["simple_fcf"]) < 0:
                row["issues"].append("最近完整年度经营现金流减总资本支出为负，需核查分红资金来源")
            if newest["audit"] != "标准无保留意见":
                row["issues"].append("最近完整年度审计意见缺失或非标准，需核查")
            if any(item.get(key) is None for item in row['annual'][:5] for key in ('deducted_profit','operating_cash','capex')):
                row['issues'].append('近五个完整年度的扣非利润或现金流数据不齐')
        latest_bs = bs.get(max(bs), {}) if bs else {}
        row["balance"] = latest_bs
        if not latest_bs or any(latest_bs.get(k) is None for k in ("total_assets", "total_liab", "money_cap")):
            row["issues"].append("现金/债务报表字段不完整")
        # Fingerprint excludes changing prices, but invalidates assumptions after disclosures or share changes.
        evidence = {"income": inc, "cashflow": cf, "balancesheet": bs, "indicator": ind, "audit": audit,
                    "events": events, "shares": row["share_count"]}
        if row['data_warnings']:
            evidence['data_warnings'] = row['data_warnings']
        if zero_cash_years:
            evidence['zero_cash_years'] = zero_cash_years
        row["evidence_hash"] = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
        row["gate"] = not row["issues"] and bool(row["price"]) and bool(row["share_count"])
        if row['status'] != '排除' and any('为负' in issue for issue in row['issues']):
            row['status'] = '观察'
    except (AppError, ValueError, TypeError, KeyError) as exc:
        row["issues"].append(str(exc) if isinstance(exc, AppError) else "财务数据校验失败")
    row["issues"].append("原始公告、正常化盈利、分红政策和现金支付能力仍需研究确认")
    return row


EVIDENCE_KEYS = ("business", "dividend_policy", "earnings", "cash_and_debt", "latest_changes", "share_basis")


def review_template(row):
    return {"symbol": row["symbol"], "approved": False, "reviewed_on": row["as_of"],
            "evidence_hash": row.get("evidence_hash", ""), "status": "待核实",
            "evidence": {key: {"status": "待核实", "reason": "", "url": "", "ann_date": "", "original_read": False} for key in EVIDENCE_KEYS},
            "scenarios": [{"name": name, "eps": None, "payout": None, "haircut": "1", "reason": ""} for name in ("悲观", "基准", "较好")],
            "opposing_reasons": [], "invalidation_events": [], "ordinary_dividend_only": False,
            "note": "EPS 须以正常化归母利润和匹配股数推算；历史派息不能替代预测。核查原文后填写，AI 草稿不自动获批。"}


def evaluate_review(row, review, cfg):
    if not review or review.get("approved") is not True:
        return [], "尚无人工确认的研究情景"
    if review.get("symbol") != row["symbol"] or review.get("status") != "可进一步评估":
        raise AppError("研究状态不支持进入价格复核。")
    if not row.get("gate"):
        raise AppError("结构化数据仍有缺口或风险标记，不能生成可执行参考价。")
    reviewed = date_key(review["reviewed_on"])
    age = (datetime.strptime(row["as_of"], "%Y%m%d") - datetime.strptime(reviewed, "%Y%m%d")).days
    if not 0 <= age <= cfg["review_max_age_days"]:
        raise AppError("研究确认已过期或使用了未来日期。")
    if review.get("evidence_hash") != row.get("evidence_hash"):
        raise AppError("财报/分红/股数已变化，需要重新研究确认。")
    if review.get("ordinary_dividend_only") is not True or not review.get("opposing_reasons") or not review.get("invalidation_events"):
        raise AppError("请区分普通/特别分红，并填写反对理由和结论失效条件。")
    for key in EVIDENCE_KEYS:
        item = review.get("evidence", {}).get(key, {})
        url = urlparse(item.get("url", ""))
        if (item.get("status") != "支持" or not item.get("reason", "").strip() or item.get("original_read") is not True
                or url.scheme != "https" or not url.hostname or url.username or url.password
                or date_key(item.get("ann_date")) > reviewed):
            raise AppError(f"{key} 缺少已阅读的原始披露依据。")
    scenarios = review.get("scenarios", [])
    if len(scenarios) != 3 or {s["name"] for s in scenarios} != {"悲观", "基准", "较好"}:
        raise AppError("需要悲观、基准、较好三个情景。")
    if len({(str(s["eps"]), str(s["payout"])) for s in scenarios}) < 2:
        raise AppError("情景不能只改变保守系数，需要解释盈利或支付率的不同假设。")
    matrix = []
    try:
        for scenario in scenarios:
            if not scenario.get("reason", "").strip():
                raise AppError("每种情景必须解释盈利、支付率和保守系数依据。")
            for required in cfg["required_yields"]:
                calculation = calculate(scenario["eps"], scenario["payout"], scenario["haircut"], required, row["price"])
                matrix.append({"scenario": scenario["name"], "required_yield": required,
                               "meets_yield": dec(row['price']) <= dec(calculation['reference_price_2dp_floor']), **calculation})
    except (ValueError, TypeError) as exc:
        raise AppError(f"情景输入无效：{exc}") from None
    dps = {entry["scenario"]: dec(entry["scenario_dividend_per_share"]) for entry in matrix}
    if not dps["悲观"] <= dps["基准"] <= dps["较好"]:
        raise AppError("情景派息方向不一致，请核对悲观/基准/较好假设。")
    return matrix, "参考价只代表给定假设下的税前股息要求，不是内在价值或买入指令"
