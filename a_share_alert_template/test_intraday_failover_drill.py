from __future__ import annotations

import unittest

from intraday_data import FetchResult
from intraday_failover_drill import compare_results


def result(provider: str, price_offset: float = 0) -> FetchResult:
    bars = []
    for index in range(8):
        price = 10 + index * .01 + price_offset
        bars.append({"symbol": "600001", "time": f"2026-08-04T10:{(index + 1) * 5:02d}",
                     "open": price, "close": price, "high": price + .01, "low": price - .01,
                     "volume_shares": 1000, "amount_estimated": price * 1000, "provider": provider})
    return FetchResult("600001", provider, bars, "2026-08-04T10:41:00", 10, False, int(provider == "sina_m5"))


class IntradayFailoverDrillTests(unittest.TestCase):
    def test_real_fallback_parity_passes(self):
        compared = compare_results(result("tencent_m5"), result("sina_m5", .001))
        self.assertTrue(compared["passed"])
        self.assertEqual(compared["selected_provider"], "sina_m5")
        self.assertEqual(compared["fallback_level"], 1)

    def test_large_price_divergence_fails(self):
        compared = compare_results(result("tencent_m5"), result("sina_m5", 1))
        self.assertFalse(compared["passed"])


if __name__ == "__main__":
    unittest.main()
