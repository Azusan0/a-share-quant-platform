from datetime import datetime
import unittest

from recommendation_engine import build_recommendation, build_snapshot


def candidate(**overrides):
    row = {
        "symbol": "600001", "name": "测试股", "direction": "有色金属", "stage": "starting",
        "score": 72, "price": 10, "low": 9.8, "high": 10.6, "high20": 10.6, "change_pct": 2.1,
        "same_time_volume_ratio": 1.5, "breakout_prev_high_pct": 0.2, "close_position": 0.8,
        "vwap": 9.95, "type": "stock", "bar_time": "2026-07-31T10:00",
        "technical_diagnosis": {"data_quality": "complete", "support_evidence": [], "missing_data": []},
    }
    row.update(overrides)
    return row


class RecommendationTests(unittest.TestCase):
    def test_setup_is_watch_not_recommend(self):
        result = build_recommendation(candidate(stage="watch"), {"direction": "有色金属", "score": 90}, "catch_up", "setup", datetime(2026, 7, 31, 10, 0))
        self.assertEqual(result["recommendation_status"], "watch")
        self.assertIn("分时状态setup不可执行", result["blockers"])

    def test_confirmation_has_executable_band(self):
        result = build_recommendation(candidate(stage="confirmed"), {"direction": "有色金属", "score": 90}, "core", "confirmation", datetime(2026, 7, 31, 10, 0))
        self.assertEqual(result["recommendation_status"], "recommend")
        self.assertGreater(result["entry_high"], result["entry_low"])
        self.assertGreaterEqual(result["risk_reward"], 2)

    def test_limit_up_is_anchor_only(self):
        result = build_recommendation(candidate(change_pct=10, limit_up=True), {"direction": "有色金属", "score": 95}, "leader", "confirmation", datetime(2026, 7, 31, 10, 0))
        self.assertNotEqual(result["recommendation_status"], "recommend")
        self.assertIn("封死涨停仅作方向锚", result["blockers"])

    def test_missing_volume_cannot_recommend(self):
        result = build_recommendation(candidate(same_time_volume_ratio=0, volume_available=False), {"direction": "有色金属", "score": 90}, "core", "confirmation", datetime(2026, 7, 31, 10, 0))
        self.assertNotEqual(result["recommendation_status"], "recommend")
        self.assertIn("同期量能缺失", result["blockers"])

    def test_volume_does_not_hide_missing_technical_quality(self):
        result = build_recommendation(candidate(stage="confirmed",technical_diagnosis={"data_quality":"missing","missing_data":["日线不足"]}),
                                      {"direction":"有色金属","score":90},"core","confirmation",datetime(2026,7,31,10,0))
        self.assertEqual(result["data_quality"],"missing")
        self.assertNotEqual(result["recommendation_status"],"recommend")

    def test_snapshot_uses_active_sectors(self):
        shadow = {"candidates": [candidate(direction="有色金属"), candidate(symbol="600002", direction="弱板块")]}
        pool = {"active_sectors": [{"direction": "有色金属", "score": 80}], "market_sentiment": {"level": "risk_on"}}
        result = build_snapshot(shadow, pool, {}, datetime(2026, 7, 31, 10, 0))
        self.assertEqual({r["direction"] for r in result["recommendations"]}, {"有色金属"})

    def test_weak_global_factor_downgrades_first_ignition(self):
        row = candidate(global_factor={"global_affected": True, "global_sector_score": 20,
                                        "global_impact": "negative", "global_confidence": 80,
                                        "global_sectors": ["半导体"], "global_drivers": ["美国SOXX-4.00%"]})
        result = build_recommendation(row, {"direction": "半导体", "score": 90}, "core", "ignition", datetime(2026, 7, 31, 10, 0))
        self.assertEqual(result["recommendation_status"], "watch")
        self.assertIn("美日韩映射因子显著偏弱，首次启动降为观察", result["blockers"])


if __name__ == "__main__":
    unittest.main()
