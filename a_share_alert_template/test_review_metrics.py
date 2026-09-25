import sqlite3
import tempfile
import unittest
from pathlib import Path

from review_metrics import build_review
from snapshot_store import SnapshotStore


class ReviewMetricsTests(unittest.TestCase):
    def test_review_never_claims_win_rate(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "review.db"
            c = sqlite3.connect(path)
            c.execute("""CREATE TABLE recommendation_lifecycle(
              status TEXT,entry_reached_at TEXT,mfe_pct REAL,mae_pct REAL,close_reason TEXT,first_seen_at TEXT)""")
            c.execute("INSERT INTO recommendation_lifecycle VALUES ('expired','2026-08-05T10:00:00',3.0,-1.0,'expired','2026-08-05T09:40:00')")
            c.commit(); c.close()
            result = build_review(path)
            self.assertEqual(result["samples"], 1)
            self.assertFalse(result["calibration_eligible"])
            self.assertNotIn("win_rate", result)

    def test_fixed_observation_can_only_create_shadow_suggestion(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "review.db"
            with SnapshotStore(path) as store:
                store.connection.execute("""INSERT INTO recommendation_lifecycle
                  (signal_id,symbol,name,sector,recommendation_type,state,role,status,first_seen_at,last_seen_at,
                   entry_reached_at,max_price,min_price,mfe_pct,mae_pct,mode,opportunity_id,entry_reference_price)
                  VALUES ('s1','600001','测试','电子','confirmation','confirmation','core','expired',
                          '2026-08-01T10:00:00','2026-08-05T15:00:00','2026-08-01T10:00:00',10.5,9.8,5,-2,'shadow','opp_1',10)""")
                store.connection.execute("""INSERT INTO recommendation_observations
                  VALUES ('opp_1','s1','600001','T+3','2026-08-05T14:55:00',10,10.4,10.5,9.8,
                          5,-2,1,0,'2026-08-05T15:00:00','2026-08-05T15:00:00')""")
                store.connection.commit()
            result = build_review(path, minimum_samples=1)
            self.assertTrue(result["calibration_eligible"])
            self.assertEqual(result["fixed_observations"]["T+3"]["completed_samples"], 1)
            self.assertEqual(result["shadow_calibration_suggestions"][0]["mode"], "shadow")
            self.assertIn("人工批准", result["shadow_calibration_suggestions"][0]["status"])
            self.assertNotIn("win_rate", result)


if __name__ == "__main__":
    unittest.main()
