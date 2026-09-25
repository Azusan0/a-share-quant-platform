import tempfile
import unittest
from pathlib import Path

from board_strength import classify_concepts, score_board
from snapshot_store import SnapshotStore


class BoardStrengthTests(unittest.TestCase):
    def test_classification_excludes_industry_region_and_channels(self):
        rows = [{"name": name, "code": f"BK{i}"} for i, name in enumerate(
            ["电子器件", "深圳板块", "融资融券", "MSCI中国", "人工智能", "机器人概念"]
        )]
        result = classify_concepts(rows, "电子器件")
        self.assertEqual([row["name"] for row in result], ["人工智能", "机器人概念"])

    def test_score_rewards_breadth_and_persistent_flow(self):
        members = [{"symbol": "600001", "name": "甲", "change_pct": 2}, {"symbol": "600002", "name": "乙", "change_pct": 1}]
        strong = score_board("人工智能", "BK1", members, {"main_net": 1e9, "main_pct": 4}, {"main_net": 2e9})
        weak = score_board("人工智能", "BK1", members, {"main_net": -1e9, "main_pct": -2}, {"main_net": -2e9})
        self.assertEqual(strong["persistence"], 2)
        self.assertGreater(strong["score"], weak["score"])

    def test_board_snapshot_is_idempotent(self):
        payload = {
            "dimensions": {"industry": [], "concept": [{
                "name": "人工智能", "code": "BK1", "score": 80, "breadth_pct": 70,
                "median_change_pct": 2, "main_net_today": 1e9, "main_net_5d": 2e9,
                "persistence": 2, "member_count": 1, "members": [{"symbol": "600001"}],
            }]},
            "stock_concepts": [{"symbol": "600001", "concept_tags": ["人工智能"]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            with SnapshotStore(Path(directory) / "snapshot.db") as store:
                store.save_board_strength("2026-08-04T10:00:00", payload)
                store.save_board_strength("2026-08-04T10:00:00", payload)
                boards = store.connection.execute("SELECT COUNT(*) FROM board_strength_snapshots").fetchone()[0]
                memberships = store.connection.execute("SELECT COUNT(*) FROM stock_board_memberships").fetchone()[0]
        self.assertEqual((boards, memberships), (1, 1))


if __name__ == "__main__":
    unittest.main()
