import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
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


def load_env():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


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
