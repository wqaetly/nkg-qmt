import json
import os
import re
import tempfile
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CN = timezone(timedelta(hours=8))


class AppError(Exception):
    pass


def now_cn():
    return datetime.now(CN)


def date_key(value):
    try:
        parsed = datetime.strptime(value, "%Y%m%d")
        if parsed.strftime("%Y%m%d") != value:
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise AppError("日期必须为 YYYYMMDD 格式。") from None


def dec(value, *, positive=False):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or (positive and result == 0):
            raise InvalidOperation()
        return result
    except (InvalidOperation, ValueError):
        raise AppError("金额/价格必须是有效的非负数，价格必须大于零。") from None


def money(value):
    return dec(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def load_env():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_config():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    if cfg["index_code"] != "000922.CSI":
        raise AppError("首版候选池为中证红利 000922.CSI。")
    for key, low, high in (("review_max_age_days", 1, 90), ("slippage_bps", 0, 1000)):
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise AppError(f"配置 {key} 超出范围。")
    for key in ("paper_cash", "paper_max_position", "paper_max_sector", "commission_rate", "minimum_commission", "sell_stamp_rate"):
        dec(cfg[key], positive=key.startswith("paper_"))
    if dec(cfg["commission_rate"]) > Decimal("0.01"):
        raise AppError("佣金率异常，请以小数形式填写。")
    for value in cfg["required_yields"]:
        if not 0 < dec(value) <= 1:
            raise AppError("情景股息率必须使用 0 到 1 的小数。")
    return cfg


def symbol_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
        raise AppError("代码必须为六位数字加 .SH、.SZ 或 .BJ。")
    return value


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd,name = tempfile.mkstemp(dir=path.parent,suffix='.tmp')
    temporary=Path(name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
