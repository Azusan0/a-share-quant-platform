import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from portfolio_store import PortfolioStore


class PortfolioStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = PortfolioStore(Path(self.temp.name) / "portfolio.db")
        self.account = self.store.create_account("测试账户", 100000, "qqbot:test")

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def test_account_and_watchlist(self):
        row = self.store.upsert_watchlist(self.account["account_id"], "600001", "测试股", status="planned",
                                          entry_low=10, entry_high=10.2, planned_amount=10000, planned_quantity=1000)
        self.assertEqual(row["entry_low"], 10)
        self.assertEqual(len(self.store.list_watchlist(self.account["account_id"])), 1)
        self.store.remove_watchlist(self.account["account_id"], "600001")
        self.assertEqual(self.store.list_watchlist(self.account["account_id"]), [])

    def test_buy_idempotency_and_cash(self):
        key = "same-request"
        first = self.store.record_trade(self.account["account_id"], "600001", "buy", 10, 1000,
                                        fee=5, idempotency_key=key, name="测试股")
        second = self.store.record_trade(self.account["account_id"], "600001", "buy", 10, 1000,
                                         fee=5, idempotency_key=key, name="测试股")
        self.assertEqual(first["trade_id"], second["trade_id"])
        portfolio = self.store.portfolio(self.account["account_id"], {"600001": 11})
        self.assertEqual(portfolio["positions"][0]["quantity"], 1000)
        self.assertEqual(portfolio["summary"]["cash"], 89995)

    def test_buy_promotes_watchlist_to_position(self):
        self.store.upsert_watchlist(self.account["account_id"], "600001", "测试股")
        self.store.record_trade(self.account["account_id"], "600001", "buy", 10, 100,
                                idempotency_key="promote-watch", name="测试股")
        self.assertEqual(self.store.list_watchlist(self.account["account_id"]), [])
        self.assertEqual(self.store.portfolio(self.account["account_id"])["positions"][0]["quantity"], 100)

    def test_t_plus_one_and_sell(self):
        yesterday = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        self.store.record_trade(self.account["account_id"], "600001", "buy", 10, 1000, fee=0,
                                traded_at=yesterday, idempotency_key="buy-yesterday")
        sold = self.store.record_trade(self.account["account_id"], "600001", "sell", 11, 500, fee=5,
                                       idempotency_key="sell-today")
        self.assertEqual(sold["side"], "sell")
        self.assertEqual(self.store.portfolio(self.account["account_id"])["positions"][0]["quantity"], 500)

    def test_same_day_buy_cannot_sell(self):
        self.store.record_trade(self.account["account_id"], "600001", "buy", 10, 1000,
                                idempotency_key="buy-today")
        with self.assertRaisesRegex(ValueError, "可卖数量不足"):
            self.store.record_trade(self.account["account_id"], "600001", "sell", 10, 100,
                                    idempotency_key="sell-today")

    def test_insufficient_cash(self):
        with self.assertRaisesRegex(ValueError, "现金不足"):
            self.store.record_trade(self.account["account_id"], "600001", "buy", 1000, 1000,
                                    idempotency_key="too-expensive")

    def test_correct_position_adjusts_cash_and_keeps_audit(self):
        yesterday = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        self.store.record_trade(self.account["account_id"], "600001", "buy", 10, 1000,
                                traded_at=yesterday, idempotency_key="buy-before-correction")
        self.store.correct_position(self.account["account_id"], "600001", 1200, 9.5, note="录入数量和成本有误")
        portfolio = self.store.portfolio(self.account["account_id"])
        self.assertEqual(portfolio["positions"][0]["quantity"], 1200)
        self.assertEqual(portfolio["positions"][0]["average_cost"], 9.5)
        self.assertEqual(portfolio["summary"]["cash"], 88600)
        self.assertIn("correct", {row["action"] for row in self.store.audit_log(self.account["account_id"])})

    def test_outbox_is_route_isolated_and_deduplicated(self):
        aid=self.account["account_id"]
        self.assertTrue(self.store.enqueue("qqbot:A",aid,"same-key","账户A消息"))
        self.assertFalse(self.store.enqueue("qqbot:A",aid,"same-key","重复消息"))
        self.assertEqual(self.store.emit_route("qqbot:B"),[])
        self.assertEqual(self.store.emit_route("qqbot:A"),["账户A消息"])
        self.assertEqual(self.store.emit_route("qqbot:A"),[])

    def test_advice_snapshot_roundtrip(self):
        row={"account_id":self.account["account_id"],"symbol":"600001","phase":"intraday","action":"hold",
             "action_ratio_pct":0,"trend_score":70,"trend_band":"strong","confidence":60,"tolerance_pct":4.5,
             "reasons":["测试"],"trigger_conditions":[],"invalidation":[],"generated_at":"2026-08-05T10:00:00","expires_at":"2026-08-05T10:05:00"}
        saved=self.store.save_advice(row)
        self.assertEqual(self.store.latest_advice(self.account["account_id"],"600001",1)[0]["advice_id"],saved["advice_id"])


if __name__ == "__main__":
    unittest.main()
