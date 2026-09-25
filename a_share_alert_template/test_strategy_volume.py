from datetime import datetime
import unittest
from unittest.mock import patch

from strategies import _expected_volume_fraction, _same_time_volume_ratio


class StrategyVolumeTests(unittest.TestCase):
    def test_fraction_is_monotonic(self):
        values = [_expected_volume_fraction(datetime(2026, 8, 3, h, m)) for h, m in ((9, 30), (10, 0), (11, 0), (13, 30), (14, 30), (15, 0))]
        self.assertEqual(values, sorted(values))

    def test_early_cumulative_volume_is_same_time_adjusted(self):
        with patch("strategies._expected_volume_fraction", return_value=.2):
            self.assertAlmostEqual(_same_time_volume_ratio(200_000, 1_000_000), 1.0)

    def test_explicit_fraction_is_reproducible(self):
        morning=_same_time_volume_ratio(500_000,1_000_000,now=datetime(2026,8,3,10,0))
        close=_same_time_volume_ratio(500_000,1_000_000,fraction=1.0)
        self.assertGreater(morning,close)
        self.assertEqual(close,0.5)


if __name__ == "__main__":
    unittest.main()
