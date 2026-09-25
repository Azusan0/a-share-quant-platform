import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import market_calendar
import portfolio_monitor
from portfolio_store import PortfolioStore


class MarketCalendarTests(unittest.TestCase):
    def test_utc_time_maps_to_shanghai_session(self):
        with patch.object(market_calendar, "load_trade_dates", return_value=None):
            value=datetime(2026,8,5,1,35,tzinfo=timezone.utc)
            self.assertEqual(market_calendar.market_phase(value),"intraday")

    def test_naive_timestamp_is_treated_as_shanghai(self):
        parsed=market_calendar.parse_shanghai("2026-08-05T09:22:00")
        self.assertEqual(parsed.hour,9)
        self.assertIsNotNone(parsed.tzinfo)

    def test_closed_day_does_not_generate_portfolio_advice(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "portfolio.db"
            with PortfolioStore(path) as store:
                account = store.create_account("测试账户", 100000)
                store.upsert_watchlist(account["account_id"], "600001", "测试股票")
            with patch.object(market_calendar, "load_trade_dates", return_value=None):
                result = portfolio_monitor.run(path, now=datetime(2026, 8, 8, 10, 0))
            self.assertEqual(result["phase"], "closed")
            self.assertEqual(result["advice"], 0)
            self.assertEqual(result["queued"], 0)


if __name__ == "__main__":
    unittest.main()
