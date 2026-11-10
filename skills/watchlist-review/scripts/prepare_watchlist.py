"""Validate a local snapshot and prepare public security identities, never trades."""

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


def number(value, nonnegative=False):
    if not isinstance(value, str):
        raise ValueError("金额、价格和数量必须使用十进制字符串")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("无效数值") from None
    if not result.is_finite() or (nonnegative and result < 0):
        raise ValueError("数值必须有限，数量和市值不能为负")
    return result


def prepare(snapshot):
    if type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1:
        raise ValueError("不支持的快照版本")
    captured = datetime.fromisoformat(snapshot["captured_at"])
    if captured.tzinfo is None:
        raise ValueError("采集时间必须带时区")
    accounts = {}
    summaries = {}
    for account in snapshot["accounts"]:
        account_id = account["id"]
        if not isinstance(account_id, str) or not account_id or account_id in accounts:
            raise ValueError("账户别名缺失或重复")
        accounts[account_id] = account
        for summary in account.get("summaries", []):
            currency = summary["currency"]
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError("币种须为三位大写代码")
            key = (account_id, currency)
            if key in summaries:
                raise ValueError("账户币种汇总重复")
            for field in ("net_assets", "market_value", "cash_available", "reported_pnl", "daily_pnl"):
                if summary.get(field) is not None:
                    number(summary[field], nonnegative=field == "market_value")
            summaries[key] = summary

    warnings, groups, public, seen = [], {}, {}, set()
    holding_count = watch_count = 0
    patterns = {"CN": r"\d{6}\.(SH|SZ|BJ)", "HK": r"\d{5}\.HK", "US": r"[A-Z][A-Z0-9.\-]{0,15}"}
    for entry in snapshot["entries"]:
        kind, market, symbol = entry["kind"], entry["market"], entry["symbol"]
        currency, status = entry["currency"], entry["identity_status"]
        if kind not in ("holding", "watch") or market not in patterns or status not in ("verified", "unverified"):
            raise ValueError("无效的条目类型、市场或身份状态")
        if not re.fullmatch(r"[A-Z]{3}", currency) or entry["asset_type"] not in ("stock", "etf", "adr"):
            raise ValueError("无效的币种或资产类别")
        if not isinstance(entry["name"], str) or not entry["name"].strip():
            raise ValueError("证券名称不能为空")
        if symbol is not None and (not isinstance(symbol, str) or not re.fullmatch(patterns[market], symbol)):
            raise ValueError("代码格式与市场不符")
        if status == "verified" and symbol is None:
            raise ValueError("已确认的证券必须有代码")
        key = (kind, entry.get("account_id"), market, symbol or entry["name"], currency)
        if key in seen:
            raise ValueError("重复条目，应先核对而非累加")
        seen.add(key)
        if status == "verified":
            identity = {field: entry[field] for field in ("symbol", "name", "market", "currency", "asset_type")}
            security_key = (market, symbol)
            if security_key in public and public[security_key] != identity:
                raise ValueError("同一证券的身份信息冲突")
            public[security_key] = identity
        else:
            warnings.append(f"{market}:{symbol or entry['name']} 身份待确认，未纳入公开研究名单")
        if kind == "watch":
            if entry.get("position") is not None or entry.get("account_id") is not None:
                raise ValueError("关注条目不能夹带账户或持仓")
            watch_count += 1
            continue
        holding_count += 1
        account_id = entry["account_id"]
        if account_id not in accounts:
            raise ValueError("持仓引用了未知账户")
        position = entry["position"]
        quantity = number(position["quantity"], nonnegative=True)
        if quantity <= 0:
            raise ValueError("普通多头持仓数量必须大于零")
        for field in ("available_quantity", "cost_price", "snapshot_price", "market_value", "reported_pnl"):
            if position.get(field) is not None:
                value = number(position[field], nonnegative=field in ("available_quantity", "snapshot_price", "market_value"))
                if field == "available_quantity" and value > quantity:
                    raise ValueError("可用数量不能大于持有数量")
        if position.get("quote_at") is None:
            warnings.append(f"{account_id}/{symbol} 行情时间未知，截图价格不能视为实时价")
        elif datetime.fromisoformat(position["quote_at"]).tzinfo is None:
            raise ValueError("行情时间必须带时区")
        group = groups.setdefault((account_id, currency), [])
        market_value = None if position.get("market_value") is None else number(position["market_value"], nonnegative=True)
        group.append((symbol or entry["name"], market_value))
        if market_value is not None and position.get("snapshot_price") is not None:
            difference = market_value - quantity * number(position["snapshot_price"], nonnegative=True)
            if abs(difference) > Decimal("0.01"):
                warnings.append(f"{account_id}/{symbol} 显示市值与数量乘价格相差 {difference} {currency}，保留原值待核对")

    totals = []
    for (account_id, currency), rows in groups.items():
        complete = all(value is not None for _, value in rows)
        total = sum((value for _, value in rows if value is not None), Decimal(0))
        item = {"account_id": account_id, "currency": currency, "visible_market_value": str(total) if complete else None,
                "weights_basis": "同账户同币种可见持仓市值，不是净资产", "weights": []}
        if complete and total > 0:
            item["weights"] = [{"symbol": symbol, "weight": str(value / total)} for symbol, value in rows]
        if not complete:
            warnings.append(f"{account_id}/{currency} 持仓市值不完整，不计算权重")
        summary = summaries.get((account_id, currency), {})
        if complete and summary.get("market_value") is not None:
            difference = total - number(summary["market_value"], nonnegative=True)
            item["difference_from_reported_market_value"] = str(difference)
            if difference != 0:
                warnings.append(f"{account_id}/{currency} 可见持仓合计与账户汇总相差 {difference}，未自动归因或补仓")
        if summary.get("cash_available") is None:
            warnings.append(f"{account_id}/{currency} 无已确认可用现金，不能用购买力或剩余流动性替代")
        totals.append(item)
    checks = {"status": "prepared_only", "captured_at": snapshot["captured_at"], "holding_count": holding_count,
              "watch_count": watch_count, "groups": totals, "warnings": warnings}
    return {"securities": list(public.values())}, checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data/watchlist/current.json"))
    args = parser.parse_args()
    try:
        snapshot = json.loads(args.input.read_text(encoding="utf-8-sig"))
        public, checks = prepare(snapshot)
        prepared_at = datetime.now(timezone(timedelta(hours=8)))
        checks["prepared_at"] = prepared_at.isoformat()
        output = Path("data/watchlist/prepared") / prepared_at.strftime("%Y%m%d-%H%M%S-%f")
        output.mkdir(parents=True, exist_ok=False)
        for name, content in (("public-research.json", public), ("checks.json", checks)):
            (output / name).write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        parser.exit(1, "快照格式无效或本地文件操作失败；未输出原始私人内容，请核对格式说明。\n")
    print(f"已准备 {checks['holding_count']} 项持仓、{checks['watch_count']} 项关注；核对提示 {len(checks['warnings'])} 项。尚未进行投资研究。")
    print(output.resolve())


if __name__ == "__main__":
    main()
