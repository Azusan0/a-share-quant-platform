import tempfile
import unittest
import json
import os
from pathlib import Path
from unittest.mock import patch

import dashboard_app
import pandas as pd
from fastapi.testclient import TestClient
from portfolio_store import PortfolioStore


class DashboardPortfolioTests(unittest.TestCase):
    def test_csrf_token_expires_and_is_user_bound(self):
        token = dashboard_app._csrf_token("viewer", now=1000)
        self.assertTrue(dashboard_app._verify_csrf_token("viewer", token, now=1000))
        self.assertFalse(dashboard_app._verify_csrf_token("other", token, now=1000))
        self.assertFalse(dashboard_app._verify_csrf_token("viewer", token, now=1000 + dashboard_app.CSRF_TTL_SECONDS + 1))

    def test_trusted_host_helper_is_opt_in(self):
        self.assertTrue(dashboard_app._is_trusted_host("anything.example", trusted=set()))
        self.assertTrue(dashboard_app._is_trusted_host("dash.example:18765", trusted={"dash.example"}))
        self.assertFalse(dashboard_app._is_trusted_host("evil.example", trusted={"dash.example"}))

    def test_untrusted_host_returns_400_when_configured(self):
        with patch.dict(os.environ, {"DASHBOARD_TRUSTED_HOSTS": "good.example"}):
            response = TestClient(dashboard_app.app).get("/healthz", headers={"host": "bad.example"})
        self.assertEqual(response.status_code, 400)

    def test_auth_failure_rate_limit_window(self):
        dashboard_app._AUTH_FAILURES.clear()
        client = "127.0.0.1"
        for _ in range(dashboard_app.AUTH_RATE_LIMIT_MAX_FAILURES):
            dashboard_app._record_auth_failure(client, now=1000)
        self.assertTrue(dashboard_app._too_many_auth_failures(client, now=1000))
        self.assertFalse(
            dashboard_app._too_many_auth_failures(
                client, now=1000 + dashboard_app.AUTH_RATE_LIMIT_WINDOW_SECONDS + 1
            )
        )

    def test_daily_chart_replaces_nan_before_json_response(self):
        frame = pd.DataFrame([{
            "date": "2026-08-05", "open": 10, "close": 10.2, "high": 10.3,
            "low": 9.9, "volume": 1000, "amount": float("nan"),
        }])
        with patch.object(dashboard_app, "fetch_history", return_value=frame):
            payload = dashboard_app._chart_payload("600001", "1d", 120)
        self.assertIsNone(payload["bars"][0]["amount"])
        json.dumps(payload, allow_nan=False)

    def test_combined_holdings_uses_sqlite_positions_and_watchlist(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "portfolio.db"
            with PortfolioStore(db_path) as store:
                first = store.create_account("账户一", 100000)
                second = store.create_account("账户二", 100000)
                store.record_trade(first["account_id"], "600584", "buy", 20, 100,
                                   idempotency_key="dashboard-position", name="长电科技")
                store.upsert_watchlist(second["account_id"], "688001", "华兴源创")
            with patch.object(dashboard_app, "PORTFOLIO_DB", db_path), \
                 patch.object(dashboard_app, "_portfolio_prices", return_value={"600584": 22, "688001": 30}):
                rows = dashboard_app._combined_portfolio_holdings()
            by_symbol = {row["symbol"]: row for row in rows}
            self.assertEqual(by_symbol["600584"]["status"], "position")
            self.assertEqual(by_symbol["600584"]["quantity"], 100)
            self.assertEqual(by_symbol["688001"]["status"], "watch")
            self.assertEqual(by_symbol["688001"]["accounts"], ["账户二"])


if __name__ == "__main__":
    unittest.main()
