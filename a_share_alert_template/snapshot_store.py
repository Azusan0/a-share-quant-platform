#!/usr/bin/env python3
"""板块、个股、分钟K和状态迁移的 SQLite 快照库。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


DEFAULT_DB = Path("/root/.hermes/scripts/a_share_market_snapshots.db")


class SnapshotStore:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=15)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=15000")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SnapshotStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _migrate(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS sector_snapshots (
          captured_at TEXT NOT NULL, trade_date TEXT NOT NULL, sector TEXT NOT NULL,
          score REAL, change_pct REAL, breadth_pct REAL, acceleration_pct REAL,
          member_count INTEGER, provider TEXT, stale INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (captured_at, sector)
        );
        CREATE INDEX IF NOT EXISTS idx_sector_date ON sector_snapshots(trade_date, sector, captured_at);
        CREATE TABLE IF NOT EXISTS stock_snapshots (
          captured_at TEXT NOT NULL, trade_date TEXT NOT NULL, symbol TEXT NOT NULL,
          name TEXT, sector TEXT, price REAL, change_pct REAL, state TEXT,
          vwap REAL, vwap_deviation_pct REAL, volume_ratio_5m REAL,
          provider TEXT, stale INTEGER NOT NULL DEFAULT 0, fallback_level INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (captured_at, symbol, sector)
        );
        CREATE INDEX IF NOT EXISTS idx_stock_date ON stock_snapshots(trade_date, symbol, captured_at);
        CREATE TABLE IF NOT EXISTS intraday_bars (
          symbol TEXT NOT NULL, bar_time TEXT NOT NULL, trade_date TEXT NOT NULL,
          open REAL NOT NULL, close REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
          volume_shares INTEGER NOT NULL, amount_estimated REAL NOT NULL, provider TEXT NOT NULL,
          PRIMARY KEY (symbol, bar_time)
        );
        CREATE INDEX IF NOT EXISTS idx_bars_date ON intraday_bars(trade_date, symbol, bar_time);
        CREATE TABLE IF NOT EXISTS intraday_states (
          symbol TEXT PRIMARY KEY, name TEXT, sector TEXT, state TEXT NOT NULL, state_label TEXT,
          entered_at TEXT, bars_in_state INTEGER, reason TEXT, bar_time TEXT,
          metrics_json TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS state_transitions (
          symbol TEXT NOT NULL, bar_time TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL,
          reason TEXT, metrics_json TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY (symbol, bar_time, to_state)
        );
        CREATE INDEX IF NOT EXISTS idx_transitions_date ON state_transitions(bar_time, symbol);
        CREATE TABLE IF NOT EXISTS source_health (
          provider TEXT NOT NULL, checked_at TEXT NOT NULL, symbol TEXT NOT NULL DEFAULT '',
          ok INTEGER NOT NULL, latency_ms INTEGER, stale INTEGER NOT NULL DEFAULT 0,
          fallback_level INTEGER NOT NULL DEFAULT 0, error TEXT,
          PRIMARY KEY (provider, checked_at, symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_health_time ON source_health(checked_at, provider);
        CREATE TABLE IF NOT EXISTS recommendation_snapshots (
          generated_at TEXT NOT NULL, trade_date TEXT NOT NULL, symbol TEXT NOT NULL,
          name TEXT, sector TEXT, status TEXT NOT NULL, recommendation_type TEXT,
          state TEXT, role TEXT, recommendation_score REAL, entry_low REAL, entry_high REAL,
          max_chase_price REAL, invalid_price REAL, target_price REAL, risk_reward REAL,
          data_quality TEXT, blockers_json TEXT NOT NULL, reasons_json TEXT NOT NULL,
          expires_at TEXT, mode TEXT NOT NULL DEFAULT 'shadow',
          PRIMARY KEY (generated_at, symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_date ON recommendation_snapshots(trade_date, status, generated_at);
        CREATE TABLE IF NOT EXISTS recommendation_lifecycle (
          signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, name TEXT, sector TEXT,
          recommendation_type TEXT, state TEXT, role TEXT, status TEXT NOT NULL,
          first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
          entry_low REAL, entry_high REAL, invalid_price REAL, target_price REAL, expires_at TEXT,
          entry_reached_at TEXT, invalidated_at TEXT, target_hit_at TEXT, closed_at TEXT,
          max_price REAL, min_price REAL, mfe_pct REAL, mae_pct REAL, market_level TEXT,
          mode TEXT NOT NULL DEFAULT 'shadow', engine_version TEXT, close_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_lifecycle_status ON recommendation_lifecycle(status, first_seen_at, sector);
        CREATE TABLE IF NOT EXISTS recommendation_events (
          signal_id TEXT NOT NULL, event_at TEXT NOT NULL, event_type TEXT NOT NULL,
          price REAL, detail_json TEXT NOT NULL,
          PRIMARY KEY (signal_id,event_at,event_type)
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_events ON recommendation_events(event_at,event_type);
        CREATE TABLE IF NOT EXISTS recommendation_observations (
          opportunity_id TEXT NOT NULL, signal_id TEXT NOT NULL, symbol TEXT NOT NULL,
          horizon TEXT NOT NULL, due_at TEXT NOT NULL, entry_price REAL NOT NULL,
          last_price REAL, max_price REAL, min_price REAL, mfe_pct REAL, mae_pct REAL,
          target_hit INTEGER NOT NULL DEFAULT 0, invalidated INTEGER NOT NULL DEFAULT 0,
          completed_at TEXT, updated_at TEXT NOT NULL,
          PRIMARY KEY (opportunity_id,horizon)
        );
        CREATE INDEX IF NOT EXISTS idx_observation_due ON recommendation_observations(completed_at,due_at,symbol);
        CREATE TABLE IF NOT EXISTS recommendation_evidence (
          opportunity_id TEXT NOT NULL, captured_at TEXT NOT NULL, symbol TEXT NOT NULL,
          side TEXT NOT NULL, evidence_group TEXT NOT NULL, evidence_text TEXT NOT NULL,
          source TEXT NOT NULL,
          PRIMARY KEY (opportunity_id,captured_at,side,evidence_group,evidence_text)
        );
        CREATE INDEX IF NOT EXISTS idx_evidence_opportunity ON recommendation_evidence(opportunity_id,captured_at);
        CREATE TABLE IF NOT EXISTS technical_diagnosis_snapshots (
          generated_at TEXT NOT NULL, trade_date TEXT NOT NULL, symbol TEXT NOT NULL,
          name TEXT, sector TEXT, technical_score REAL, trend TEXT,
          support_price REAL, resistance_price REAL, data_quality TEXT,
          support_evidence_json TEXT NOT NULL, opposing_evidence_json TEXT NOT NULL,
          missing_data_json TEXT NOT NULL, diagnosis_json TEXT NOT NULL,
          PRIMARY KEY (generated_at,symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_technical_diagnosis_date
          ON technical_diagnosis_snapshots(trade_date,symbol,generated_at);
        CREATE TABLE IF NOT EXISTS fundamental_snapshots (
          generated_at TEXT NOT NULL, trade_date TEXT NOT NULL, symbol TEXT NOT NULL,
          name TEXT, sector TEXT, fundamental_score REAL, risk_level TEXT,
          report_date TEXT, data_quality TEXT,
          support_evidence_json TEXT NOT NULL, opposing_evidence_json TEXT NOT NULL,
          event_risks_json TEXT NOT NULL, missing_data_json TEXT NOT NULL,
          fundamental_json TEXT NOT NULL,
          PRIMARY KEY (generated_at,symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_fundamental_date
          ON fundamental_snapshots(trade_date,symbol,generated_at);
        CREATE TABLE IF NOT EXISTS board_strength_snapshots (
          generated_at TEXT NOT NULL, trade_date TEXT NOT NULL, dimension TEXT NOT NULL,
          board_name TEXT NOT NULL, board_code TEXT, score REAL, breadth_pct REAL,
          median_change_pct REAL, main_net_today REAL, main_net_5d REAL,
          persistence INTEGER, member_count INTEGER, members_json TEXT NOT NULL,
          detail_json TEXT NOT NULL,
          PRIMARY KEY (generated_at,dimension,board_name)
        );
        CREATE INDEX IF NOT EXISTS idx_board_strength_date
          ON board_strength_snapshots(trade_date,dimension,generated_at,score);
        CREATE TABLE IF NOT EXISTS stock_board_memberships (
          generated_at TEXT NOT NULL, trade_date TEXT NOT NULL, symbol TEXT NOT NULL,
          concept_tags_json TEXT NOT NULL,
          PRIMARY KEY (generated_at,symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_stock_board_memberships
          ON stock_board_memberships(trade_date,symbol,generated_at);
        CREATE TABLE IF NOT EXISTS intraday_replay_runs (
          generated_at TEXT NOT NULL, trade_date TEXT NOT NULL, symbol TEXT NOT NULL,
          quality TEXT NOT NULL, bars INTEGER, expected_bars INTEGER, coverage_pct REAL,
          missing_count INTEGER, invalid_count INTEGER, fallback_consistent INTEGER NOT NULL,
          idempotent INTEGER NOT NULL, final_state TEXT, transition_count INTEGER,
          provider_counts_json TEXT NOT NULL, missing_times_json TEXT NOT NULL,
          detail_json TEXT NOT NULL,
          PRIMARY KEY (generated_at,trade_date,symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_intraday_replay_date
          ON intraday_replay_runs(trade_date,quality,generated_at);
        """)
        lifecycle_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(recommendation_lifecycle)")}
        for column, definition in (
            ("opportunity_id", "TEXT"),
            ("observation_until", "TEXT"),
            ("entry_reference_price", "REAL"),
        ):
            if column not in lifecycle_columns:
                self.connection.execute(f"ALTER TABLE recommendation_lifecycle ADD COLUMN {column} {definition}")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_lifecycle_opportunity ON recommendation_lifecycle(opportunity_id,first_seen_at)"
        )
        self.connection.commit()

    def upsert_bars(self, bars: Iterable[dict[str, Any]]) -> int:
        rows = list(bars)
        with self.connection:
            self.connection.executemany("""
              INSERT INTO intraday_bars VALUES (?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(symbol, bar_time) DO UPDATE SET
                open=excluded.open, close=excluded.close, high=excluded.high, low=excluded.low,
                volume_shares=excluded.volume_shares, amount_estimated=excluded.amount_estimated,
                provider=excluded.provider
            """, [(r["symbol"], r["time"], r["time"][:10], r["open"], r["close"], r["high"], r["low"],
                    r["volume_shares"], r["amount_estimated"], r["provider"]) for r in rows])
        return len(rows)

    def get_bars(self, symbol: str, trade_date: str, limit: int = 80) -> list[dict[str, Any]]:
        rows = self.connection.execute("""
          SELECT symbol, bar_time AS time, open, close, high, low, volume_shares,
                 amount_estimated, provider FROM intraday_bars
          WHERE symbol=? AND trade_date=? ORDER BY bar_time DESC LIMIT ?
        """, (symbol, trade_date, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_state(self, symbol: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM intraday_states WHERE symbol=?", (symbol,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result.update(json.loads(result.pop("metrics_json")))
        return result

    def save_state(self, symbol: str, name: str, sector: str, state: dict[str, Any]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        metrics = {k: state[k] for k in ("return_5m_pct", "volume_ratio_5m", "vwap", "vwap_deviation_pct", "breakout_pct", "drawdown_from_high_pct", "current")}
        with self.connection:
            self.connection.execute("""
              INSERT INTO intraday_states VALUES (?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, sector=excluded.sector,
                state=excluded.state, state_label=excluded.state_label, entered_at=excluded.entered_at,
                bars_in_state=excluded.bars_in_state, reason=excluded.reason,
                bar_time=excluded.bar_time, metrics_json=excluded.metrics_json, updated_at=excluded.updated_at
            """, (symbol, name, sector, state["state"], state["state_label"], state["entered_at"],
                  state["bars_in_state"], state["reason"], state["bar_time"], json.dumps(metrics, ensure_ascii=False), now))
            if state.get("changed"):
                self.connection.execute("""
                  INSERT OR IGNORE INTO state_transitions VALUES (?,?,?,?,?,?,?)
                """, (symbol, state["bar_time"], state.get("previous_state"), state["state"], state["reason"],
                      json.dumps(metrics, ensure_ascii=False), now))

    def save_sector_snapshots(self, captured_at: str, sectors: Iterable[dict[str, Any]], provider: str) -> None:
        rows = list(sectors)
        with self.connection:
            self.connection.executemany("""
              INSERT OR REPLACE INTO sector_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)
            """, [(captured_at, captured_at[:10], r.get("direction") or r.get("label"), r.get("score"),
                    r.get("change_pct"), r.get("breadth_pct"), r.get("acceleration_pct"),
                    len(r.get("members") or []), provider, int(bool(r.get("data_stale")))) for r in rows])

    def save_stock_snapshot(self, captured_at: str, symbol: str, name: str, sector: str, state: dict[str, Any], provider: str, stale: bool, fallback_level: int = 0) -> None:
        with self.connection:
            self.connection.execute("""
              INSERT OR REPLACE INTO stock_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (captured_at, captured_at[:10], symbol, name, sector, state.get("current"), None,
                  state["state"], state.get("vwap"), state.get("vwap_deviation_pct"), state.get("volume_ratio_5m"),
                  provider, int(stale), fallback_level))

    def save_health(self, provider: str, checked_at: str, symbol: str, ok: bool, latency_ms: int | None = None, stale: bool = False, fallback_level: int = 0, error: str | None = None) -> None:
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO source_health VALUES (?,?,?,?,?,?,?,?)",
                                    (provider, checked_at, symbol, int(ok), latency_ms, int(stale), fallback_level, error))

    def save_recommendations(self, generated_at: str, rows: Iterable[dict[str, Any]], mode: str = "shadow") -> None:
        values = list(rows)
        with self.connection:
            self.connection.executemany("""
              INSERT OR REPLACE INTO recommendation_snapshots VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, [(
                generated_at, generated_at[:10], row.get("symbol"), row.get("name"), row.get("direction"),
                row.get("recommendation_status"), row.get("recommendation_type"), row.get("state"), row.get("role"),
                row.get("recommendation_score"), row.get("entry_low"), row.get("entry_high"), row.get("max_chase_price"),
                row.get("invalid_price"), row.get("target_price"), row.get("risk_reward"), row.get("data_quality"),
                json.dumps(row.get("blockers") or [], ensure_ascii=False), json.dumps(row.get("reasons") or [], ensure_ascii=False),
                row.get("expires_at"), mode,
            ) for row in values])

    def save_technical_diagnoses(self, generated_at: str, rows: Iterable[dict[str, Any]]) -> None:
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "")
            diagnosis = row.get("technical_diagnosis")
            if symbol and isinstance(diagnosis, dict):
                unique[symbol] = row
        with self.connection:
            self.connection.executemany("""
              INSERT OR REPLACE INTO technical_diagnosis_snapshots VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, [(
                generated_at, generated_at[:10], symbol, row.get("name"), row.get("direction"),
                diagnosis.get("technical_score"), diagnosis.get("trend"),
                diagnosis.get("support_price"), diagnosis.get("resistance_price"), diagnosis.get("data_quality"),
                json.dumps(diagnosis.get("support_evidence") or [], ensure_ascii=False),
                json.dumps(diagnosis.get("opposing_evidence") or [], ensure_ascii=False),
                json.dumps(diagnosis.get("missing_data") or [], ensure_ascii=False),
                json.dumps(diagnosis, ensure_ascii=False),
            ) for symbol, row in unique.items() for diagnosis in [row["technical_diagnosis"]]])

    def save_fundamentals(self, generated_at: str, rows: Iterable[dict[str, Any]]) -> None:
        values = list(rows)
        with self.connection:
            self.connection.executemany("""
              INSERT OR REPLACE INTO fundamental_snapshots VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, [(
                generated_at, generated_at[:10], row.get("symbol"), row.get("name"), row.get("sector"),
                row.get("fundamental_score"), row.get("risk_level"), row.get("report_date"), row.get("data_quality"),
                json.dumps(row.get("support_evidence") or [], ensure_ascii=False),
                json.dumps(row.get("opposing_evidence") or [], ensure_ascii=False),
                json.dumps(row.get("event_risks") or [], ensure_ascii=False),
                json.dumps(row.get("missing_data") or [], ensure_ascii=False),
                json.dumps(row, ensure_ascii=False),
            ) for row in values if row.get("symbol")])

    def save_board_strength(self, generated_at: str, payload: dict[str, Any]) -> None:
        values = []
        stock_concepts = {str(row.get("symbol")): row.get("concept_tags") or [] for row in payload.get("stock_concepts") or []}
        for dimension, rows in (payload.get("dimensions") or {}).items():
            for row in rows:
                detail = dict(row)
                if dimension == "concept":
                    detail["stock_concepts"] = stock_concepts
                values.append((
                    generated_at, generated_at[:10], dimension, row.get("name"), row.get("code"),
                    row.get("score"), row.get("breadth_pct"), row.get("median_change_pct"),
                    row.get("main_net_today"), row.get("main_net_5d"), row.get("persistence"),
                    row.get("member_count"), json.dumps(row.get("members") or [], ensure_ascii=False),
                    json.dumps(detail, ensure_ascii=False),
                ))
        with self.connection:
            self.connection.executemany("""
              INSERT OR REPLACE INTO board_strength_snapshots VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, values)
            self.connection.executemany("""
              INSERT OR REPLACE INTO stock_board_memberships VALUES (?,?,?,?)
            """, [(generated_at, generated_at[:10], symbol, json.dumps(tags, ensure_ascii=False))
                    for symbol, tags in stock_concepts.items()])

    def save_intraday_replays(self, generated_at: str, rows: Iterable[dict[str, Any]]) -> None:
        values = list(rows)
        with self.connection:
            self.connection.executemany("""
              INSERT OR REPLACE INTO intraday_replay_runs VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, [(
                generated_at, row.get("trade_date"), row.get("symbol"), row.get("quality"),
                row.get("bars"), row.get("expected_bars"), row.get("coverage_pct"),
                row.get("missing_count"), row.get("invalid_count"), int(bool(row.get("fallback_consistent"))),
                int(bool(row.get("idempotent"))), row.get("final_state"), row.get("transition_count"),
                json.dumps(row.get("provider_counts") or {}, ensure_ascii=False),
                json.dumps(row.get("missing_times") or [], ensure_ascii=False),
                json.dumps(row, ensure_ascii=False),
            ) for row in values if row.get("symbol") and row.get("trade_date")])

    def prune(self, days: int = 90) -> None:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self.connection:
            for table, field in (("sector_snapshots", "captured_at"), ("stock_snapshots", "captured_at"),
                                 ("intraday_bars", "bar_time"), ("state_transitions", "bar_time"),
                                 ("source_health", "checked_at")):
                self.connection.execute(f"DELETE FROM {table} WHERE {field} < ?", (cutoff,))
            self.connection.execute("DELETE FROM recommendation_snapshots WHERE generated_at < ?", (cutoff,))
            self.connection.execute("DELETE FROM recommendation_events WHERE event_at < ?", (cutoff,))
            self.connection.execute("DELETE FROM recommendation_lifecycle WHERE first_seen_at < ? AND status NOT IN ('active','entry_reached')", (cutoff,))
            self.connection.execute("DELETE FROM technical_diagnosis_snapshots WHERE generated_at < ?", (cutoff,))
            self.connection.execute("DELETE FROM fundamental_snapshots WHERE generated_at < ?", (cutoff,))
            self.connection.execute("DELETE FROM board_strength_snapshots WHERE generated_at < ?", (cutoff,))
            self.connection.execute("DELETE FROM stock_board_memberships WHERE generated_at < ?", (cutoff,))
            self.connection.execute("DELETE FROM intraday_replay_runs WHERE generated_at < ?", (cutoff,))
