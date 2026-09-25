import tempfile
import unittest
from pathlib import Path

import pandas as pd

from fundamental_data import assess_fundamental, parse_balance_frame, parse_financial_frame, parse_performance_forecasts
from snapshot_store import SnapshotStore


class FundamentalDataTests(unittest.TestCase):
    def test_parse_latest_financial_report(self):
        frame = pd.DataFrame([
            {"report_date": "2025-12-31", "metric_name": "parent_holder_net_profit", "value": "100", "yoy": ""},
            {"report_date": "2025-12-31", "metric_name": "calculate_parent_holder_net_profit_yoy_growth_ratio", "value": "20", "yoy": ""},
            {"report_date": "2025-12-31", "metric_name": "assets_debt_ratio", "value": "45", "yoy": ""},
            {"report_date": "2025-09-30", "metric_name": "parent_holder_net_profit", "value": "80", "yoy": ""},
        ])
        result = parse_financial_frame(frame)
        self.assertEqual(result["report_date"], "2025-12-31")
        self.assertEqual(result["profit_yoy_pct"], 20)
        self.assertEqual(result["debt_ratio_pct"], 45)

    def test_risk_events_and_quality_are_explained(self):
        result = assess_fundamental({
            "financial": {
                "report_date": "2025-12-31", "revenue_yoy_pct": -8, "profit_yoy_pct": -20,
                "roe_pct": 3, "ocf_per_share": -0.2, "debt_ratio_pct": 76, "provider": "test",
            },
            "valuation": {"pe_ttm": -4, "pb": 2},
            "pledge": {"pledge_ratio_pct": 35, "pledge_date": "2026-07-31"},
            "unlocks": [{"date": "2026-08-30", "float_market_cap_ratio_pct": 8}],
            "notices": [{"date": "2026-08-04", "title": "关于股东减持计划的公告", "type": "持股变动"}],
        }, pd.Timestamp("2026-08-04").to_pydatetime())
        self.assertEqual(result["risk_level"], "high")
        self.assertTrue(any("股东减持" in item for item in result["opposing_evidence"]))
        self.assertTrue(any("解禁" in item for item in result["opposing_evidence"]))
        self.assertTrue(any("经营现金流为负" in item for item in result["opposing_evidence"]))

    def test_balance_goodwill_and_performance_forecast_are_structured(self):
        balance = parse_balance_frame(pd.DataFrame([
            {"report_date": "2026-03-31", "metric_name": "goodwill", "value": "600"},
            {"report_date": "2026-03-31", "metric_name": "parent_holder_equity_total", "value": "1000"},
            {"report_date": "2026-03-31", "metric_name": "assets_total", "value": "3000"},
        ]))
        self.assertEqual(balance["goodwill_net_assets_ratio_pct"], 60)
        forecasts = parse_performance_forecasts(pd.DataFrame([{
            "股票代码": "600001", "预测指标": "归属于上市公司股东的净利润", "预告类型": "首亏",
            "业绩变动幅度": -180, "预测数值": -1000, "上年同期值": 800,
            "业绩变动": "预计亏损", "业绩变动原因": "主营承压", "公告日期": "2026-07-20",
        }]), {"600001"}, "20260630")
        self.assertEqual(forecasts["600001"]["forecast_type"], "首亏")
        no_goodwill = parse_balance_frame(pd.DataFrame([
            {"report_date": "2026-03-31", "metric_name": "goodwill", "value": ""},
            {"report_date": "2026-03-31", "metric_name": "parent_holder_equity_total", "value": "1000"},
        ]))
        self.assertEqual(no_goodwill["goodwill_net_assets_ratio_pct"], 0)

    def test_goodwill_forecast_and_contract_calibrate_hard_risk(self):
        result = assess_fundamental({
            "financial": {"goodwill": 600, "net_assets": 1000, "goodwill_net_assets_ratio_pct": 60,
                          "revenue": 1_000_000_000, "provider": "test"},
            "valuation": {"pe_ttm": 20, "pb": 2}, "pledge": {"pledge_ratio_pct": 5},
            "performance_forecast": {"forecast_type": "首亏", "change_pct": -180},
            "notices": [{"date": "2026-07-20", "title": "关于终止重大合同的公告", "url": "https://example.test"}],
        }, pd.Timestamp("2026-08-04").to_pydatetime())
        self.assertEqual(result["score_version"], "fundamental_v2")
        self.assertEqual(result["risk_level"], "high")
        self.assertIn("高商誉", result["hard_risks"])
        self.assertIn("业绩预亏", result["hard_risks"])
        self.assertTrue(any(row["label"] == "合同终止" for row in result["major_contracts"]))

    def test_fundamental_snapshot_is_idempotent(self):
        row = {
            "symbol": "600001", "name": "示例", "sector": "测试", "fundamental_score": 60,
            "risk_level": "medium", "report_date": "2025-12-31", "data_quality": "complete",
            "support_evidence": ["ROE 12%"], "opposing_evidence": [], "event_risks": [],
            "missing_data": [], "pe_ttm": 20, "pb": 2,
        }
        with tempfile.TemporaryDirectory() as directory:
            with SnapshotStore(Path(directory) / "snapshot.db") as store:
                store.save_fundamentals("2026-08-04T10:00:00", [row, row])
                count = store.connection.execute("SELECT COUNT(*) FROM fundamental_snapshots").fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
