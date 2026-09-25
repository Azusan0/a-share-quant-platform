import unittest
from datetime import datetime

from auction_analyzer import analyze_auction
from overnight_plan import build_plan
from position_advice import evaluate_position
from position_advice_replay import replay


class PositionAdviceTests(unittest.TestCase):
    def position(self): return {"account_id":"a","symbol":"600001","name":"测试","average_cost":10,"highest_price":12,"stop_price":9.5}

    def test_strong_trend_tolerates_more_drawdown(self):
        strong = evaluate_position(self.position(), {"price":11.6,"high":12}, {"market_score":80,"sector_score":85,"vwap_deviation_pct":1,"relative_strength_pct":1,"intraday_state":"confirmation","below_vwap_bars":2}, now=datetime(2026,8,5,10))
        weak = evaluate_position(self.position(), {"price":11.6,"high":12}, {"market_score":20,"sector_score":20,"vwap_deviation_pct":-2,"relative_strength_pct":-2,"intraday_state":"failed","below_vwap_bars":2,"sector_weak":True}, now=datetime(2026,8,5,10))
        self.assertGreater(strong["tolerance_pct"], weak["tolerance_pct"])
        self.assertEqual(strong["action"], "protect")
        self.assertEqual(weak["action"], "trim")

    def test_correlated_price_signals_count_once(self):
        row = evaluate_position(self.position(), {"price":11.5,"high":12}, {
            "market_score":30,"sector_score":50,"vwap_deviation_pct":-2,
            "intraday_state":"failed","below_vwap_bars":3,"rebound_failed":True,
        }, now=datetime(2026,8,5,10))
        self.assertEqual(row["action"], "protect")
        self.assertEqual(set(row["evidence_groups"]), {"price_risk"})

    def test_price_risk_plus_independent_sector_can_trim(self):
        row = evaluate_position(self.position(), {"price":11.5,"high":12}, {
            "market_score":20,"sector_score":20,"vwap_deviation_pct":-2,
            "intraday_state":"failed","below_vwap_bars":3,"sector_weak":True,
        }, now=datetime(2026,8,5,10))
        self.assertEqual(row["action"], "trim")
        self.assertEqual(set(row["evidence_groups"]), {"price_risk","sector"})

    def test_hard_stop_always_exits(self):
        row = evaluate_position(self.position(), {"price":9.4}, {"market_score":90,"sector_score":90,"intraday_state":"confirmation"})
        self.assertEqual(row["action"], "exit")

    def test_global_factor_is_soft_and_cannot_exit_alone(self):
        row = evaluate_position(self.position(), {"price":11.8,"high":12}, {"market_score":60,"sector_score":60,
            "intraday_state":"confirmation","global_affected":True,"global_sector_score":10,"global_confidence":90})
        self.assertNotEqual(row["action"], "exit")
        self.assertEqual(row["global_sector_score"],10)

    def test_auction_early_only_is_partial(self):
        row = analyze_auction([{"captured_at":"2026-08-05T09:16:00","price":10.2}],10)
        self.assertEqual(row["data_quality"], "partial")
        self.assertIn("未匹配委托量", row["missing_data"])

    def test_auction_parses_milliseconds_and_timezone(self):
        samples=[
            {"captured_at":"2026-08-05T09:16:00","price":10.0},
            {"captured_at":"2026-08-05T09:22:00.500","price":10.1},
            {"captured_at":"2026-08-05T09:24:00+08:00","price":10.2},
        ]
        row=analyze_auction(samples,10)
        self.assertEqual(row["data_quality"],"complete")

    def test_overnight_plan_probabilities_sum_100(self):
        row=build_plan("600001","测试",{"technical_score":80,"sector_score":80,"market_score":70,"support_price":9.8,"resistance_price":10.8})
        self.assertEqual(sum(row["probabilities"].values()),100)

    def test_global_sector_factor_calibrates_premarket_probability(self):
        base={"technical_score":60,"sector_score":60,"market_score":55,"support_price":9.8,"resistance_price":10.8,
              "global_affected":True,"global_confidence":80,"global_sectors":["半导体"]}
        strong=build_plan("600001","测试",{**base,"global_sector_score":80,"global_impact":"positive","global_drivers":["美国SOXX+3.00%"]},"premarket")
        weak=build_plan("600001","测试",{**base,"global_sector_score":20,"global_impact":"negative","global_drivers":["美国SOXX-3.00%"]},"premarket")
        self.assertGreater(strong["probabilities"]["bullish"],weak["probabilities"]["bullish"])
        self.assertEqual(strong["global_factor"]["sectors"],["半导体"])

    def test_replay_can_avoid_fixed_early_exit_in_strong_trend(self):
        bars=[]
        for i,price in enumerate([10,10.5,11,10.75,10.7,11.2,11.5]):
            bars.append({"time":f"2026-08-05T10:{i*5:02d}:00","close":price,"high":max(price,11),"vwap":10.6})
        row=replay(self.position(),bars,85,85)
        self.assertTrue(row["avoided_early_exit"])


if __name__ == "__main__": unittest.main()
