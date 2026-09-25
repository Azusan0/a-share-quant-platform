import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import portfolio_api
from portfolio_store import PortfolioStore


class PortfolioTradeApiTests(unittest.TestCase):
    def test_trade_name_is_resolved_from_symbol(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "portfolio.db"
            with PortfolioStore(db_path) as store:
                account = store.create_account("接口测试", 10000)

            app = FastAPI()
            app.include_router(
                portfolio_api.create_portfolio_router(
                    db_path, lambda: "tester", lambda: "tester", lambda: {}
                )
            )
            client = TestClient(app)
            with patch.object(portfolio_api, "_resolve_symbol", return_value=("自动补全名称", "stock")):
                lookup = client.get("/api/portfolio/symbols/600001")
                response = client.post(
                    f"/api/portfolio/accounts/{account['account_id']}/trades",
                    headers={"Idempotency-Key": "trade-name-resolve-test"},
                    json={
                        "symbol": "600001",
                        "name": "错误的手工名称",
                        "side": "buy",
                        "price": 10,
                        "quantity": 100,
                    },
                )
            self.assertEqual(lookup.status_code, 200, lookup.text)
            self.assertEqual(lookup.json()["name"], "自动补全名称")
            self.assertEqual(response.status_code, 200, response.text)

            correction = client.patch(
                f"/api/portfolio/accounts/{account['account_id']}/positions/600001",
                json={"quantity": 200, "average_cost": 11},
            )
            self.assertEqual(correction.status_code, 200, correction.text)
            self.assertEqual(correction.json()["positions"][0]["quantity"], 200)

            with PortfolioStore(db_path) as store:
                positions = store.portfolio(account["account_id"])["positions"]
            self.assertEqual(positions[0]["name"], "自动补全名称")


if __name__ == "__main__":
    unittest.main()
