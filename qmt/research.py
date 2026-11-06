import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from vendor.dividend_stock_research.dividend_price import calculate
from .config import ROOT, AppError, date_key, dec


DISCLOSURES = json.loads((ROOT/'qmt/disclosures.json').read_text(encoding='utf-8'))


def disclosed_date(row):
    dates = [row[key] for key in ("ann_date", "f_ann_date") if row.get(key)]
    return max(date_key(day) for day in dates) if dates else "99999999"


def statements(rows, as_of):
    selected = {}
    for row in rows:
        if disclosed_date(row) > as_of or row.get("end_date", "99999999") > as_of:
            continue
        if "report_type" in row and str(row["report_type"]) not in {"1", "4"}:
            continue
        period = date_key(row["end_date"])
        old = selected.get(period)
        if old and disclosed_date(old) == disclosed_date(row):
            if str(old.get("report_type")) != str(row.get("report_type")) and {str(old.get("report_type")),str(row.get("report_type"))} == {"1","4"}:
                selected[period] = row if str(row["report_type"]) == "4" else old
                continue
            conflicts = [key for key,value in row.items() if old.get(key) is not None and value is not None and old[key] != value]
            if conflicts:
                raise AppError(f"{period} 同期财报版本冲突（{', '.join(sorted(conflicts))}），需核对原文。")
            selected[period] = {**old, **{k:v for k,v in row.items() if v is not None}}
            continue
        if not old or disclosed_date(row) > disclosed_date(old):
            selected[period] = row
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


def normalize_disclosed_dividends(rows, as_of):
    """Verified provider overlaps only; unknown conflicts still fail closed."""
    rows=normalize_cash_overlaps(normalize_stock_only(rows,as_of),as_of)
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
        for field in ("record_date", "ex_date", "end_date"):
            date_key(row.get(field))
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


def summarize(dossier, basic, quote):
    as_of = dossier["as_of"]
    row = {"symbol": dossier["symbol"], "name": basic.get("name", "身份待核实"),
           "industry": basic.get("industry") or "待核实", "as_of": as_of,
           "price": quote.get("close"), "historic_yield_pct": quote.get("dv_ttm"),
           "share_count": str(dec(quote["total_share"]) * 10000) if quote.get("total_share") else None,
           "status": "待核实", "issues": list(dossier["errors"].values()), "annual": [],
           "latest_period": None, "scenarios": [], "gate": False}
    if not basic or not quote or quote.get("trade_date") != as_of:
        row["issues"].append("身份或当日收盘行情缺失，暂停价格结论")
    if "ST" in row["name"] or "退" in row["name"]:
        row["status"] = "排除"
        row["issues"].append("风险警示/退市标识，不进入首版计划")
    try:
        def checked(api):
            try:
                return statements(dossier['data'].get(api,[]),as_of)
            except AppError as exc:
                row['issues'].append(api+'：'+str(exc))
                return {}
        inc,cf,bs,ind,audit = [checked(api) for api in ('income','cashflow','balancesheet','fina_indicator','fina_audit')]
        try:
            events = implemented_dividends(dossier["data"].get("dividend", []), as_of)
        except AppError as exc:
            row['issues'].append('分红记录：'+str(exc))
            events = []
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
        dividend_years, ordinary_years = {},{}
        for event in events:
            year = event["end_date"][:4]
            dividend_years[year] = dividend_years.get(year, Decimal(0)) + dec(event["cash_div_tax"])
            # shortcut: unclassified payouts remain a proxy; company-policy research must verify them.
            ordinary_years[year] = ordinary_years.get(year,Decimal(0))+dec(event.get('ordinary_cash_div_tax',event['cash_div_tax']))
        for period in annual:
            profit = signed(inc[period].get("n_income_attr_p"))
            cash = cf.get(period, {}).get("n_cashflow_act")
            capex = cf.get(period, {}).get("c_pay_acq_const_fiolta")
            free = None if cash is None or capex is None or row["financial_company"] else str(signed(cash) - signed(capex))
            item = {"period": period, "profit": str(profit), "deducted_profit": ind.get(period, {}).get("profit_dedt"),
                    "operating_cash": cash, "capex": capex, "simple_fcf": free,
                    "implemented_dps": str(dividend_years[period[:4]]) if period[:4] in dividend_years else None,
                    "ordinary_dps": str(ordinary_years[period[:4]]) if period[:4] in ordinary_years else None,
                    "audit": audit.get(period, {}).get("audit_result"),
                    "ann_date": disclosed_date(inc[period])}
            row["annual"].append(item)
        if any(item["implemented_dps"] is None for item in row["annual"][:5]):
            row["issues"].append("近五年存在未取得已实施分红的年度，须区分停派、尚未实施和数据缺口")
        if any(item['implemented_dps'] is not None and dec(item['implemented_dps'])==0 for item in row['annual'][:5]):
            row['issues'].append('近五年存在已核实零现金分红年度；纯送转不算持续现金股息')
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
