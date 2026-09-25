from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from portfolio_guard import apply_portfolio_guard
from recommendation_consumer import load_executable_recommendations, to_monitor_alert
from recommendation_lifecycle import lifecycle_context, update_lifecycle
from snapshot_store import SnapshotStore


def recommendation(symbol="600001", sector="有色", score=70, state="confirmation"):
    now = datetime(2026, 8, 4, 10, 0)
    return {
        "symbol": symbol, "name": symbol, "direction": sector, "recommendation_status": "recommend",
        "status_label": "推荐", "recommendation_type": state, "state": state, "role": "core",
        "recommendation_score": score, "risk_reward": 2.5, "price": 10.0, "entry_low": 9.9,
        "entry_high": 10.1, "invalid_price": 9.5, "target_price": 11.0,
        "expires_at": (now + timedelta(minutes=15)).isoformat(), "bar_time": now.isoformat(),
        "state_entered_at": now.isoformat(), "blockers": [], "reasons": [], "market_level": "risk_on",
        "lifecycle_status": "entry_reached",
    }


class PortfolioLifecycleTests(unittest.TestCase):
    def test_sector_and_total_limits(self):
        rows = [recommendation("600001", "有色", 80), recommendation("600002", "有色", 75), recommendation("600003", "电子", 70)]
        guarded = apply_portfolio_guard(rows, {"risk_on_max_active": 2, "max_active_total": 2, "max_active_per_sector": 1}, {}, "risk_on")
        self.assertEqual(sum(row["recommendation_status"] == "recommend" for row in guarded), 2)
        self.assertEqual(sum(row["direction"] == "有色" and row["recommendation_status"] == "recommend" for row in guarded), 1)

    def test_lifecycle_is_idempotent_and_tracks_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            with SnapshotStore(Path(temp) / "test.db") as store:
                row = recommendation()
                update_lifecycle(store.connection, [row], datetime(2026, 8, 4, 10, 0))
                update_lifecycle(store.connection, [row], datetime(2026, 8, 4, 10, 5))
                self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM recommendation_lifecycle").fetchone()[0], 1)
                self.assertEqual(store.connection.execute("SELECT status FROM recommendation_lifecycle").fetchone()[0], "entry_reached")
                context = lifecycle_context(store.connection, "2026-08-04")
                self.assertEqual(context["daily_new_count"], 1)

    def test_state_change_reuses_opportunity_and_records_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            with SnapshotStore(Path(temp) / "test.db") as store:
                first = recommendation(state="ignition")
                first["support_evidence"] = ["量价确认"]
                update_lifecycle(store.connection, [first], datetime(2026, 8, 4, 10, 0))
                second = recommendation(state="confirmation")
                second["state_entered_at"] = "2026-08-04T10:10:00"
                second["bar_time"] = "2026-08-04T10:10:00"
                update_lifecycle(store.connection, [second], datetime(2026, 8, 4, 10, 10))
                opportunities = store.connection.execute(
                    "SELECT DISTINCT opportunity_id FROM recommendation_lifecycle"
                ).fetchall()
                self.assertEqual(len(opportunities), 1)
                self.assertTrue(second["opportunity_id"].startswith("opp_"))
                self.assertEqual(
                    store.connection.execute("SELECT COUNT(*) FROM recommendation_evidence").fetchone()[0], 1
                )

    def test_fixed_observation_completes_t1_without_claiming_win_rate(self):
        with tempfile.TemporaryDirectory() as temp:
            with SnapshotStore(Path(temp) / "test.db") as store:
                row = recommendation()
                update_lifecycle(store.connection, [row], datetime(2026, 8, 4, 10, 0))
                later = dict(row, price=10.5)
                update_lifecycle(store.connection, [later], datetime(2026, 8, 5, 15, 0))
                observations = store.connection.execute(
                    "SELECT horizon,completed_at,mfe_pct FROM recommendation_observations ORDER BY horizon"
                ).fetchall()
                self.assertEqual(len(observations), 2)
                t1 = next(item for item in observations if item["horizon"] == "T+1")
                self.assertIsNotNone(t1["completed_at"])
                self.assertEqual(t1["mfe_pct"], 5.0)

    def test_expired_lifecycle_is_not_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            with SnapshotStore(Path(temp) / "test.db") as store:
                row = recommendation()
                update_lifecycle(store.connection, [row], datetime(2026, 8, 4, 10, 0))
                update_lifecycle(store.connection, [row], datetime(2026, 8, 4, 10, 20))
                self.assertEqual(row["lifecycle_status"], "expired")

    def test_consumer_rejects_shadow_and_stale(self):
        now = datetime(2026, 8, 4, 10, 5)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot.json"
            path.write_text('{"generated_at":"2026-08-04T10:00:00","recommendations":[]}', encoding="utf-8")
            shadow = {"recommendation_engine": {"mode": "shadow", "snapshot_file": str(path)}}
            self.assertEqual(load_executable_recommendations(shadow, now), [])
            live = {"recommendation_engine": {"mode": "live", "snapshot_file": str(path), "max_snapshot_age_minutes": 1}}
            self.assertEqual(load_executable_recommendations(live, now), [])

    def test_consumer_returns_fresh_live_recommendation(self):
        now = datetime(2026, 8, 4, 10, 5)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot.json"
            row = recommendation()
            import json
            path.write_text(json.dumps({"generated_at": "2026-08-04T10:00:00", "recommendations": [row]}, ensure_ascii=False), encoding="utf-8")
            config = {"recommendation_engine": {"mode": "live", "snapshot_file": str(path), "max_snapshot_age_minutes": 10}}
            loaded = load_executable_recommendations(config, now)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(to_monitor_alert(loaded[0], now)["strategy"], "recommendation_engine")


if __name__ == "__main__":
    unittest.main()
