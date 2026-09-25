import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import global_market_factor
from global_market_factor import (
    factor_for_stock,
    normalize_indices,
    normalize_news,
    parse_proxy_quotes,
    score_sector_factors,
)


class GlobalMarketFactorTests(unittest.TestCase):
    def test_indices_and_proxy_quotes_are_normalized(self):
        indices = normalize_indices({
            "america": [{"code": "IXIC", "qtcode": "s_usIXIC", "name": "纳斯达克", "zxj": "21000", "zdf": "2.5", "state": "close"}],
            "asia": [{"code": "N225", "qtcode": "gzN225", "name": "日经225指数", "zxj": "41000", "zdf": "1.2", "state": "open"}],
        })
        self.assertEqual(indices[0]["country"], "美国")
        self.assertEqual(indices[1]["state"], "open")

        fields = ["200", "半导体ETF-iShares", "SOXX.OQ", "541.71", "507.68", "529.31", "1000"]
        fields.extend([""] * (30 - len(fields)))
        fields.extend(["2026-08-05 04:00:00", "34.03", "6.70"])
        rows = parse_proxy_quotes(f'v_usSOXX="{"~".join(fields)}";')
        self.assertEqual(rows[0]["change_pct"], 6.7)
        self.assertIn("半导体", rows[0]["sectors"])

    def test_semiconductor_factor_combines_us_japan_korea_and_news(self):
        indices = [
            {"code": "IXIC", "display": "纳斯达克", "country": "美国", "change_pct": 2.0},
            {"code": "N225", "display": "日经225", "country": "日本", "change_pct": 1.0},
            {"code": "KS11", "display": "韩国综合", "country": "韩国", "change_pct": 1.5},
        ]
        proxies = [
            {"name": "SOXX", "country": "美国", "change_pct": 3.0, "sectors": {"半导体": 1.0}},
            {"name": "东京电子", "country": "日本", "change_pct": 2.0, "sectors": {"半导体": .9}},
            {"name": "SK海力士", "country": "韩国", "change_pct": 2.5, "sectors": {"半导体": .8, "存储芯片": 1.0}},
        ]
        news = normalize_news([{"title": "英伟达大涨，半导体需求超预期", "time": "2026-08-05 07:30:00", "source": "test"}], datetime(2026, 8, 5, 8))
        factors = score_sector_factors(indices, proxies, news)
        semiconductor = next(row for row in factors if row["sector"] == "半导体")
        self.assertEqual(semiconductor["impact"], "positive")
        self.assertEqual(set(semiconductor["countries"]), {"美国", "日本", "韩国"})
        self.assertGreater(semiconductor["confidence"], 70)

    def test_a_share_stock_is_bound_by_sector_labels(self):
        snapshot = {"sector_factors": [{"sector": "半导体", "score": 72, "impact": "positive", "confidence": 82,
                                          "drivers": ["美国SOXX+2.00%"]}]}
        factor = factor_for_stock("688001", "测试股票", ["半导体", "先进封装"], snapshot)
        self.assertTrue(factor["global_affected"])
        self.assertEqual(factor["global_sector_score"], 72)
        self.assertIn("半导体", factor["global_sectors"])

    def test_source_failure_keeps_previous_snapshot_but_marks_degraded(self):
        previous = {
            "indices": [{"code": "IXIC", "display": "纳斯达克", "country": "美国", "change_pct": 1,
                         "price": 1, "name": "纳斯达克", "state": "close"}],
            "proxies": [{"name": "SOXX", "country": "美国", "change_pct": 2, "sectors": {"半导体": 1}}],
            "news": [{"title": "半导体上涨", "time": "2026-08-05 07:00:00", "source": "cache"}],
        }
        failure = RuntimeError("source down")
        with patch.object(global_market_factor, "fetch_global_indices", side_effect=failure), \
             patch.object(global_market_factor, "fetch_proxy_quotes", side_effect=failure), \
             patch.object(global_market_factor, "fetch_eastmoney_news", side_effect=failure), \
             patch.object(global_market_factor, "fetch_wscn_news", side_effect=failure):
            payload = global_market_factor.collect(
                datetime(2026, 8, 5, 8), previous,
                Path("missing-portfolio.db"), Path("missing-market.db"), Path("missing-pool.json"),
            )
        self.assertEqual(payload["indices"][0]["code"], "IXIC")
        self.assertFalse(payload["source_health"]["ok"])
        self.assertEqual(len(payload["source_health"]["errors"]), 4)


if __name__ == "__main__":
    unittest.main()
