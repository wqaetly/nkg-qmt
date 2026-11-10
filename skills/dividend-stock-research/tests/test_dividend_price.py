"""Arithmetic and input-boundary checks, not investment performance tests."""

import unittest
from decimal import Decimal

from scripts.dividend_price import calculate


class DividendPriceTests(unittest.TestCase):
    def test_known_scenario(self):
        result = calculate("2", "0.6", "0.8", "0.04", "20")
        self.assertEqual(Decimal(result["scenario_dividend_per_share"]), Decimal("0.96"))
        self.assertEqual(result["reference_price_2dp_floor"], "24.00")
        self.assertEqual(Decimal(result["scenario_yield_at_price"]), Decimal("0.048"))

    def test_total_profit_and_per_share_equivalence(self):
        profit, shares = Decimal("1000000000"), Decimal("500000000")
        result = calculate(profit / shares, "0.6", "0.8", "0.04")
        expected = profit * Decimal("0.6") * Decimal("0.8") / Decimal("0.04") / shares
        self.assertEqual(Decimal(result["reference_price_unrounded"]), expected)

    def test_more_conservative_means_lower_price(self):
        base = Decimal(calculate(2, ".6", ".8", ".04")["reference_price_unrounded"])
        for args in ((1, ".6", ".8", ".04"), (2, ".3", ".8", ".04"),
                     (2, ".6", ".5", ".04"), (2, ".6", ".8", ".06")):
            self.assertLess(Decimal(calculate(*args)["reference_price_unrounded"]), base)

    def test_zero_dividend_and_no_rounding_up(self):
        self.assertEqual(calculate(2, 0, 1, ".04")["reference_price_2dp_floor"], "0.00")
        self.assertEqual(calculate(0, 1, 1, ".04")["reference_price_2dp_floor"], "0.00")
        self.assertEqual(calculate(1, 1, 0, ".04")["reference_price_2dp_floor"], "0.00")
        self.assertEqual(calculate(1, 1, 1, ".03")["reference_price_2dp_floor"], "33.33")

    def test_reject_invalid_inputs(self):
        base = ["2", "0.6", "0.8", "0.04", "20"]
        for index, bad in ((0, "-1"), (0, "NaN"), (0, "Infinity"), (1, "60"),
                           (1, "1.1"), (2, "-0.1"), (2, "1.1"), (3, "0"),
                           (3, "4"), (4, "0"), (4, "-1"), (4, "abc")):
            with self.subTest(index=index, bad=bad):
                args = base.copy()
                args[index] = bad
                with self.assertRaises(ValueError):
                    calculate(*args)


if __name__ == "__main__":
    unittest.main()
