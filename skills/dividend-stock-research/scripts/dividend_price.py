"""Compute one hypothetical dividend scenario; no market-data or trade access."""

import argparse
import json
from decimal import Decimal, InvalidOperation, ROUND_DOWN


def number(value, name):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name}: expected a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name}: expected a finite decimal")
    return result


def calculate(eps, payout, haircut, required_yield, price=None):
    eps = number(eps, "eps")
    payout = number(payout, "payout")
    haircut = number(haircut, "haircut")
    required_yield = number(required_yield, "required_yield")
    if eps < 0:
        raise ValueError("negative earnings: this ordinary-dividend model is inapplicable")
    for name, value in (("payout", payout), ("haircut", haircut)):
        if not 0 <= value <= 1:
            raise ValueError(f"{name}: use a decimal between 0 and 1")
    if not 0 < required_yield <= 1:
        raise ValueError("required_yield: use a decimal greater than 0 and at most 1")
    dividend = eps * payout * haircut
    target = dividend / required_yield
    result = {
        "scenario_dividend_per_share": str(dividend),
        "reference_price_unrounded": str(target),
        "reference_price_2dp_floor": str(target.quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
        "basis": "hypothetical, pre-tax; not intrinsic value or a buy instruction",
    }
    if price is not None:
        price = number(price, "price")
        if price <= 0:
            raise ValueError("price must be positive")
        result["scenario_yield_at_price"] = str(dividend / price)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("eps", "payout", "haircut", "required-yield"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--price")
    args = parser.parse_args()
    try:
        result = calculate(args.eps, args.payout, args.haircut, args.required_yield, args.price)
    except (ValueError, InvalidOperation, OverflowError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
