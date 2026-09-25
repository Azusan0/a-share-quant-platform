import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from snapshot_store import SnapshotStore
from technical_diagnosis import diagnose_technical


def history(start: float = 10.0, end: float = 12.0, rows: int = 65) -> pd.DataFrame:
    closes = np.linspace(start, end, rows)
    return pd.DataFrame({
        "date": pd.date_range("2026-04-01", periods=rows, freq="B"),
        "open": closes * 0.995,
        "close": closes,
        "high": closes * 1.01,
        "low": closes * 0.99,
        "volume": np.linspace(1_000_000, 1_300_000, rows),
    })


class TechnicalDiagnosisTest(unittest.TestCase):
    def test_bullish_multi_period_structure(self):
        result = diagnose_technical(history(), {"price": 12.2})
        self.assertEqual(result["trend"], "bullish")
        self.assertGreaterEqual(result["technical_score"], 70)
        self.assertIn("价格站上上行MA20", result["support_evidence"])
        self.assertIsNotNone(result["support_price"])

    def test_bearish_structure_records_opposing_evidence(self):
        result = diagnose_technical(history(12.0, 10.0), {"price": 9.8})
        self.assertEqual(result["trend"], "bearish")
        self.assertIn("两个以上周期处于空头结构", result["opposing_evidence"])
        self.assertLess(result["technical_score"], 50)

    def test_short_history_exposes_missing_data(self):
        result = diagnose_technical(history(rows=10), {"price": 10.5})
        self.assertEqual(result["data_quality"], "missing")
        self.assertIn("至少20根完整日线", result["missing_data"])

    def test_diagnosis_snapshot_is_idempotent_per_symbol(self):
        diagnosis = diagnose_technical(history(), {"price": 12.2})
        rows = [
            {"symbol": "600001", "name": "示例", "direction": "测试", "technical_diagnosis": diagnosis},
            {"symbol": "600001", "name": "示例", "direction": "测试", "technical_diagnosis": diagnosis},
        ]
        with tempfile.TemporaryDirectory() as directory:
            with SnapshotStore(Path(directory) / "snapshots.db") as store:
                store.save_technical_diagnoses("2026-08-04T10:00:00", rows)
                count = store.connection.execute("SELECT COUNT(*) FROM technical_diagnosis_snapshots").fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
