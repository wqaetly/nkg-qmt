import json
import os
import hashlib
import time
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .config import AppError, date_key, dec, atomic_json, now_cn, symbol_key


# Tushare docs 27/32/183/103/79: a full page may be truncated, even from cache.
ROW_LIMITS = {"daily": 6000, "daily_basic": 6000, "stk_limit": 5800,
              "dividend": 2000, "fina_indicator": 100}


class PageLimitError(AppError):
    pass


def year_ranges(start, end):
    start, end = date_key(start), date_key(end)
    if start > end:
        raise AppError("数据开始日期不能晚于结束日期。")
    return [(max(start, f"{year}0101"), min(end, f"{year}1231"))
            for year in range(int(start[:4]), int(end[:4]) + 1)]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Credentials must never follow an upstream redirect to another host.
        return None


def request_json(url, payload=None, headers=None, timeout=25):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise AppError("数据与模型接口必须使用不含账户凭据的 HTTPS 地址。")
    request = Request(url, data=None if payload is None else json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", "User-Agent": "nkg-qmt/0.1", **(headers or {})})
    try:
        with build_opener(NoRedirect).open(request, timeout=timeout) as response:
            body = response.read(8 * 1024 * 1024 + 1)
        if len(body) > 8 * 1024 * 1024:
            raise AppError("接口响应过大，已停止处理。")
        return json.loads(body)
    except HTTPError as exc:
        raise AppError(f"接口返回 HTTP {exc.code}；请检查权限或稍后重试。") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise AppError("接口连接失败或未返回有效 JSON；本次不使用旧数据替代。") from None


class Tushare:
    def __init__(self, cache_dir=None):
        self.cache_dir = cache_dir
        self.last_call = 0

    def call(self, api, params, fields):
        key = hashlib.sha256(json.dumps([api, params, fields], sort_keys=True).encode()).hexdigest()
        path = self.cache_dir / f"{api}-{key}.json" if self.cache_dir else None
        if path and path.exists():
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
                if (not isinstance(rows, list)
                        or any(not isinstance(row, dict) or not set(fields.split(",")).issubset(row) for row in rows)):
                    raise ValueError()
                if len(rows) >= ROW_LIMITS.get(api, 6000):
                    raise PageLimitError(f"Tushare {api} 缓存达到条数上限，可能被截断。")
                return rows
            except (KeyError, TypeError, ValueError):
                raise AppError(f"Tushare {api} 缓存结构异常或可能被截断。") from None
        token = os.environ.get("TUSHARE_TOKEN")
        if not token:
            raise AppError("请先在 .env 配置 TUSHARE_TOKEN。")
        time.sleep(max(0, 0.35 - (time.monotonic() - self.last_call)))
        self.last_call = time.monotonic()
        result = request_json(os.environ.get("TUSHARE_BASE_URL", "https://api.tushare.pro"),
                              {"api_name": api, "token": token, "params": params, "fields": fields})
        if not isinstance(result, dict) or result.get("code") != 0:
            # Do not echo untrusted upstream error bodies (which can contain secrets).
            raise AppError(f"Tushare {api} 调用失败，请检查积分权限、调用频率或服务状态。")
        try:
            data = result["data"]
            names, rows = data["fields"], data["items"]
            if not isinstance(rows, list) or not isinstance(names, list):
                raise ValueError()
            if not set(fields.split(",")).issubset(names) or len(set(names)) != len(names):
                raise ValueError()
            if any(not isinstance(row, list) or len(row) != len(names) for row in rows):
                raise ValueError()
            if len(rows) >= ROW_LIMITS.get(api, 6000):
                raise PageLimitError(f"Tushare {api} 返回达到条数上限，可能被截断。")
            result_rows = [dict(zip(names, row)) for row in rows]
            if path:
                atomic_json(path, {"api": api, "params": params, "fields": fields,
                                   "fetched_at": now_cn().isoformat(), "rows": result_rows})
            return result_rows
        except (KeyError, TypeError, ValueError):
            raise AppError(f"Tushare {api} 数据结构异常或可能被截断。") from None

    def yearly(self, api, symbol, start, end, fields):
        rows = []
        financial = api in {"income", "cashflow", "balancesheet", "fina_indicator", "fina_audit"}
        date_field = "end_date" if api == "fina_indicator" else "ann_date" if financial else "trade_date"
        for first, last in year_ranges(start, end):
            part = self.call(api, {"ts_code": symbol, "start_date": first, "end_date": last}, fields)
            # Other statement docs omit a cap; retain the existing conservative 100-row guard per slice.
            if len(part) >= (100 if financial else ROW_LIMITS.get(api, 6000)):
                raise AppError(f"{api} 分年请求达到条数警戒线，请核实截断。")
            if any(row.get("ts_code") != symbol or not first <= date_key(row.get(date_field)) <= last for row in part):
                raise AppError(f"{api} 分年数据代码或日期超出请求范围。")
            rows.extend(part)
        return rows

    def calendar(self, start, end):
        rows = self.call("trade_cal", {"exchange": "SSE", "start_date": start, "end_date": end}, "cal_date,is_open")
        days = [date_key(row["cal_date"]) for row in rows]
        if len(set(days)) != len(days) or len(days) != (datetime.strptime(end, "%Y%m%d") - datetime.strptime(start, "%Y%m%d")).days + 1:
            raise AppError("交易日历缺失或重复。")
        if any(row["is_open"] not in (0, 1) or not start <= row["cal_date"] <= end for row in rows):
            raise AppError("交易日历字段异常。")
        return sorted(row["cal_date"] for row in rows if row["is_open"] == 1)

    def pool(self, as_of):
        start = (datetime.strptime(as_of, "%Y%m%d") - timedelta(days=100)).strftime("%Y%m%d")
        rows = self.call("index_weight", {"index_code": "000922.CSI", "start_date": start, "end_date": as_of},
                         "index_code,con_code,trade_date,weight")
        if not rows:
            raise AppError("未取得中证红利成分数据，不能用固定股票名单替代。")
        if any(row["index_code"] != "000922.CSI" or date_key(row["trade_date"]) > as_of for row in rows):
            raise AppError("指数成分代码或日期异常。")
        day = max(row["trade_date"] for row in rows)
        members = [row for row in rows if row["trade_date"] == day]
        if len(members) != 100 or len({row["con_code"] for row in members}) != 100:
            raise AppError("中证红利成分不足或不止 100 只，请核对接口快照。")
        if (datetime.strptime(as_of, "%Y%m%d") - datetime.strptime(day, "%Y%m%d")).days > 45:
            raise AppError("最新成分权重快照超过 45 天，暂停初筛。")
        if not Decimal("99") <= sum(dec(row["weight"]) for row in members) <= Decimal("101"):
            raise AppError("指数权重合计异常。")
        return day, sorted(symbol_key(row["con_code"]) for row in members)

    def basics(self):
        rows = self.call("stock_basic", {"list_status": "L"}, "ts_code,name,industry,market,list_date")
        if len({row["ts_code"] for row in rows}) != len(rows):
            raise AppError("股票列表重复。")
        return {row["ts_code"]: row for row in rows}

    def prices(self, as_of):
        rows = self.call("daily_basic", {"trade_date": as_of}, "ts_code,trade_date,close,total_share,pe_ttm,pb,dv_ttm")
        if len({row["ts_code"] for row in rows}) != len(rows) or any(row["trade_date"] != as_of for row in rows):
            raise AppError("日行情重复或日期不符。")
        return {row["ts_code"]: row for row in rows}

    def bars(self, symbol, start, end):
        rows = self.yearly("daily", symbol, start, end, "ts_code,trade_date,open,high,low,close,vol")
        limits = self.yearly("stk_limit", symbol, start, end, "ts_code,trade_date,up_limit,down_limit")
        result, bands = {}, {}
        for row in limits:
            if row["ts_code"] != symbol or row["trade_date"] in bands or not start <= date_key(row["trade_date"]) <= end:
                raise AppError("涨跌停记录异常。")
            bands[row["trade_date"]] = row
        for row in rows:
            day = date_key(row["trade_date"])
            if row["ts_code"] != symbol or day in result or not start <= day <= end:
                raise AppError("交易日线代码、日期或重复记录异常。")
            item = {"date": day, **{key: dec(row[key], positive=key != "vol") for key in ("open", "high", "low", "close", "vol")}}
            if not item["low"] <= min(item["open"], item["close"]) <= max(item["open"], item["close"]) <= item["high"]:
                raise AppError("OHLC 价格关系异常。")
            band = bands.get(day)
            item["up"] = dec(band["up_limit"], positive=True) if band else None
            item["down"] = dec(band["down_limit"], positive=True) if band else None
            result[day] = item
        return result

    def candles(self, symbol, start, end):
        rows = self.yearly('daily',symbol,start,end,'ts_code,trade_date,open,high,low,close,vol')
        result = {}
        for row in rows:
            day = date_key(row['trade_date'])
            if row['ts_code']!=symbol or not start<=day<=end or day in result:
                raise AppError('K 线代码、日期或重复记录异常。')
            values = {key:dec(row[key],positive=key!='vol') for key in ('open','high','low','close','vol')}
            if not values['low']<=min(values['open'],values['close'])<=max(values['open'],values['close'])<=values['high']:
                raise AppError('K 线 OHLC 关系异常。')
            result[day]={'day':day,**{key:float(value) for key,value in values.items()}}
        if not result:
            raise AppError('该股票未返回 K 线，不以其他股票或演示数据替代。')
        return [result[day] for day in sorted(result)]

    def dossier(self, symbol, as_of, start_year=None):
        explicit_history = start_year is not None
        if start_year is None:
            start_year = int(date_key(as_of)[:4]) - 9
        if type(start_year) is not int:
            raise AppError("财报起始年份必须为整数。")
        start = f"{start_year}0101"
        year_ranges(start, as_of)
        definitions = {
            "dividend": "ts_code,end_date,ann_date,div_proc,cash_div_tax,stk_div,record_date,ex_date,pay_date,imp_ann_date",
            "income": "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,revenue,n_income_attr_p,basic_eps",
            "cashflow": "ts_code,ann_date,f_ann_date,end_date,report_type,n_cashflow_act,c_pay_acq_const_fiolta",
            "balancesheet": "ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,total_share,money_cap,st_borr,lt_borr,non_cur_liab_due_1y,total_liab,total_assets",
            "fina_indicator": "ts_code,ann_date,end_date,profit_dedt,roe",
            "fina_audit": "ts_code,ann_date,end_date,audit_result,audit_agency"}
        result = {"symbol": symbol, "as_of": as_of, "start_year": start_year, "data": {}, "errors": {}}
        for api, fields in definitions.items():
            try:
                # Dividend has no documented date-range pagination; a full page must stay blocked.
                if api == "dividend":
                    rows = self.call(api, {"ts_code": symbol}, fields)
                elif explicit_history:
                    rows = self.yearly(api, symbol, start, as_of, fields)
                else:
                    try:
                        rows = self.call(api, {"ts_code": symbol, "start_date": start, "end_date": as_of}, fields)
                        # Preserve the previous conservative statement guard, without multiplying normal daily requests.
                        if len(rows) >= 100:
                            raise PageLimitError(f"{api} 达到财报条数警戒线。")
                    except PageLimitError:
                        rows = self.yearly(api, symbol, start, as_of, fields)
                if api == "dividend" and len(rows) >= ROW_LIMITS[api]:
                    raise AppError(f"{api} 返回达到接口上限，请人工核实截断。")
                if any(row["ts_code"] != symbol for row in rows):
                    raise AppError(f"{api} 股票代码不匹配。")
                if api != "dividend":
                    date_field = "end_date" if api == "fina_indicator" else "ann_date"
                    if any(not start <= date_key(row.get(date_field)) <= as_of for row in rows):
                        raise AppError(f"{api} 财报日期超出请求范围。")
                result["data"][api] = rows
            except AppError as exc:
                result["errors"][api] = str(exc)
                result["data"][api] = []
        return result
