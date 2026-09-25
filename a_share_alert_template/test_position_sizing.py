import unittest

from position_sizing import suggest_position_size


class PositionSizingTests(unittest.TestCase):
    def test_size_respects_cash_position_and_lot(self):
        row = suggest_position_size(entry_price=10, invalid_price=9.5, total_assets=100000, cash=50000,
                                    max_position_pct=20, risk_profile="balanced")
        self.assertTrue(row["actionable"])
        self.assertEqual(row["shares"] % 100, 0)
        self.assertLessEqual(row["position_value"], 20000)
        self.assertLessEqual(row["position_value"], 50000)

    def test_invalid_anchor_has_no_executable_size(self):
        row = suggest_position_size(entry_price=10, invalid_price=10, total_assets=100000, cash=50000)
        self.assertFalse(row["actionable"])
        self.assertEqual(row["shares"], 0)

    def test_existing_position_reduces_capacity(self):
        row = suggest_position_size(entry_price=10, invalid_price=9, total_assets=100000, cash=50000,
                                    max_position_pct=20, current_symbol_value=19500)
        self.assertFalse(row["actionable"])


if __name__ == "__main__":
    unittest.main()
