from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from intraday_data import IntradayDataError, fetch_m5_with_fallback, market_code, parse_sina_payload, parse_tencent_payload
from intraday_state import evaluate_state
from snapshot_store import SnapshotStore


class IntradayTests(unittest.TestCase):
    def test_market_routing_and_conflict(self):
        self.assertEqual(market_code("600519"), "sh600519")
        self.assertEqual(market_code("000001"), "sz000001")
        self.assertEqual(market_code("830799"), "bj830799")
        with self.assertRaises(ValueError):
            market_code("sz600519")

    def test_tencent_parse_units_dedup_and_amount(self):
        row = ["202607311000", "10", "10.2", "10.3", "9.9", "123", {}, "999999"]
        payload = {"data": {"sh600001": {"m5": [row, row]}}}
        bars = parse_tencent_payload(payload, "600001", datetime(2026, 7, 31, 10, 1))
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["volume_shares"], 12300)
        self.assertLess(bars[0]["amount_estimated"], 200_000)
        self.assertNotEqual(bars[0]["amount_estimated"], 999999)

    def test_sina_parse_uses_raw_amount(self):
        payload = 'var _data=([{"day":"2026-07-31 10:00:00","open":"10","high":"10.3","low":"9.9","close":"10.2","volume":"12300","amount":"125460"}]);'
        bars = parse_sina_payload(payload, "600001", datetime(2026, 7, 31, 10, 1))
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["volume_shares"], 12300)
        self.assertEqual(bars[0]["amount_estimated"], 125460)
        self.assertEqual(bars[0]["provider"], "sina_m5")

    def test_fallback_returns_sina_when_tencent_fails(self):
        from unittest.mock import patch
        sina = parse_sina_payload('var _data=([{"day":"2026-07-31 10:00:00","open":"10","high":"10.3","low":"9.9","close":"10.2","volume":"12300","amount":"125460"}]);', "600001", datetime(2026, 7, 31, 10, 1))
        from intraday_data import FetchResult
        result = FetchResult("600001", "sina_m5", sina, "2026-07-31T10:01:00", 12, False, 1)
        with patch("intraday_data.fetch_m5", side_effect=IntradayDataError("腾讯超时")), patch("intraday_data.fetch_sina_m5", return_value=result):
            actual = fetch_m5_with_fallback("600001", now=datetime(2026, 7, 31, 10, 1))
        self.assertEqual(actual.provider, "sina_m5")
        self.assertEqual(len(actual.attempts), 2)
        self.assertFalse(actual.attempts[0]["ok"])
        self.assertTrue(actual.attempts[1]["ok"])

    def test_state_transition(self):
        bars = []
        for i in range(8):
            close = 10 + i * 0.01
            bars.append({"time": f"2026-07-31T10:{(i + 1) * 5:02d}", "open": close - .01, "close": close,
                         "high": close + .01, "low": close - .02, "volume_shares": 1000,
                         "amount_estimated": close * 1000})
        bars[-1].update(close=10.2, high=10.21, volume_shares=1800, amount_estimated=10.2 * 1800)
        state = evaluate_state(bars, {"state": "setup", "bars_in_state": 2, "entered_at": bars[-2]["time"]})
        self.assertEqual(state["state"], "ignition")
        self.assertTrue(state["changed"])

        same = evaluate_state(bars, state)
        self.assertEqual(same["bars_in_state"], 1)
        self.assertFalse(same["changed"])

    def test_sqlite_idempotent_bars(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshots.db"
            bar = {"symbol": "600001", "time": "2026-07-31T10:00", "open": 10, "close": 10.1,
                   "high": 10.2, "low": 9.9, "volume_shares": 1000, "amount_estimated": 10050,
                   "provider": "tencent_m5"}
            with SnapshotStore(path) as store:
                store.upsert_bars([bar])
                store.upsert_bars([bar])
                count = store.connection.execute("SELECT COUNT(*) FROM intraday_bars").fetchone()[0]
                mode = store.connection.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(count, 1)
            self.assertEqual(mode.lower(), "wal")

    def test_recommendation_snapshot_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshots.db"
            row = {"symbol": "600001", "name": "测试", "direction": "有色", "recommendation_status": "watch",
                   "recommendation_type": "setup", "state": "setup", "role": "catch_up", "recommendation_score": 60,
                   "entry_low": 10, "entry_high": 10.1, "max_chase_price": 10.1, "invalid_price": 9.8,
                   "target_price": 10.5, "risk_reward": 2, "data_quality": "complete", "blockers": [],
                   "reasons": ["测试"], "expires_at": "2026-07-31T10:30:00"}
            with SnapshotStore(path) as store:
                store.save_recommendations("2026-07-31T10:00:00", [row])
                store.save_recommendations("2026-07-31T10:00:00", [row])
                count = store.connection.execute("SELECT COUNT(*) FROM recommendation_snapshots").fetchone()[0]
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
