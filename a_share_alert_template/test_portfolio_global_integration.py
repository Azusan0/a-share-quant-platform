import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import portfolio_monitor
from portfolio_store import PortfolioStore


class PortfolioGlobalIntegrationTests(unittest.TestCase):
    def test_premarket_advice_contains_bound_global_driver(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "portfolio.db"
            sentiment = root / "sentiment.json"
            dynamic = root / "dynamic.json"
            fundamental = root / "fundamental.json"
            global_market = root / "global.json"
            sentiment.write_text(json.dumps({"score": 55}), encoding="utf-8")
            dynamic.write_text(json.dumps({"active_sectors": []}), encoding="utf-8")
            fundamental.write_text(json.dumps({"stocks": []}), encoding="utf-8")
            global_market.write_text(json.dumps({
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "indices": [{"code": "IXIC"}],
                "stock_bindings": [{
                    "symbol": "600584", "global_affected": True, "global_sector_score": 75,
                    "global_impact": "positive", "global_confidence": 90,
                    "global_sectors": ["半导体"], "global_drivers": ["美国SOXX+3.00%"],
                }],
            }, ensure_ascii=False), encoding="utf-8")
            with PortfolioStore(db_path) as store:
                account = store.create_account("测试账户", 100000, "qqbot:test")
                store.upsert_watchlist(account["account_id"], "600584", "长电科技")

            fake_data_source = types.SimpleNamespace(fetch_snapshot=lambda symbols: {
                "600584": {"symbol": "600584", "name": "长电科技", "price": 30,
                           "change_pct": 0, "provider": "test"}
            })
            with patch.object(portfolio_monitor, "SENTIMENT", sentiment), \
                 patch.object(portfolio_monitor, "DYNAMIC", dynamic), \
                 patch.object(portfolio_monitor, "FUNDAMENTAL", fundamental), \
                 patch.object(portfolio_monitor, "GLOBAL_MARKET", global_market), \
                 patch.object(portfolio_monitor, "SNAPSHOT_DB", root / "missing.db"), \
                 patch.dict(sys.modules, {"data_source": fake_data_source}):
                result = portfolio_monitor.run(db_path, datetime.now(), "premarket")

            self.assertEqual(result["advice"], 1)
            with PortfolioStore(db_path) as store:
                advice = store.latest_advice(account["account_id"], "600584", 1)[0]
            self.assertIn("美国SOXX+3.00%", advice["reasons"])


if __name__ == "__main__":
    unittest.main()
