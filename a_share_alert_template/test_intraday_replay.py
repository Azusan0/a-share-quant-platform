from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from intraday_replay import audit_symbol, session_slots
from snapshot_store import SnapshotStore


def bars(trade_date="2026-08-04"):
    result = []
    for index, time in enumerate(session_slots(trade_date)):
        price = 10 + index * .01
        result.append({"symbol": "600001", "time": time, "open": price, "close": price + .01,
                       "high": price + .02, "low": price - .01, "volume_shares": 1000 + index,
                       "amount_estimated": price * (1000 + index), "provider": "tencent_m5"})
    return result


class IntradayReplayTests(unittest.TestCase):
    def test_complete_replay_is_consistent_and_idempotent(self):
        result = audit_symbol("600001", "2026-08-04", bars())
        self.assertEqual(result["quality"], "complete")
        self.assertEqual(result["bars"], 48)
        self.assertTrue(result["fallback_consistent"])
        self.assertTrue(result["idempotent"])

    def test_missing_and_invalid_bars_are_exposed(self):
        sample = bars()[:20]
        sample[2]["high"] = 1
        result = audit_symbol("600001", "2026-08-04", sample)
        self.assertEqual(result["quality"], "invalid")
        self.assertEqual(result["invalid_count"], 1)
        self.assertGreater(result["missing_count"], 20)

    def test_replay_snapshot_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshots.db"
            result = audit_symbol("600001", "2026-08-04", bars())
            with SnapshotStore(path) as store:
                store.save_intraday_replays("2026-08-04T18:10:00", [result])
                store.save_intraday_replays("2026-08-04T18:10:00", [result])
                count = store.connection.execute("SELECT COUNT(*) FROM intraday_replay_runs").fetchone()[0]
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
