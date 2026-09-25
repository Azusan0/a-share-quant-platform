#!/usr/bin/env python3
"""多账户、自选、持仓和人工成交流水SQLite存储。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


DEFAULT_DB = Path("/var/lib/a-share-dashboard/portfolios.db")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _money(value: Any) -> int:
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise ValueError("金额必须是数字") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("金额不能为负数")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _price(value: Any) -> int:
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise ValueError("价格必须是数字") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("价格必须大于0")
    return int((amount * 10000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _yuan(cents: int | None) -> float | None:
    return round(cents / 100, 2) if cents is not None else None


def _price_value(value: int | None) -> float | None:
    return round(value / 10000, 4) if value is not None else None


def _nullable_price(value: Any) -> int | None:
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise ValueError("价格必须是数字") from exc
    if not amount.is_finite():
        raise ValueError("价格必须是有限数字")
    return None if amount <= 0 else _price(amount)


def _symbol(value: str) -> str:
    value = str(value).strip()
    if len(value) != 6 or not value.isdigit():
        raise ValueError("股票代码必须是6位数字")
    return value


class PortfolioStore:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=15)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=15000")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "PortfolioStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _migrate(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
          account_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'active',
          initial_capital_cents INTEGER NOT NULL, cash_balance_cents INTEGER NOT NULL,
          risk_profile TEXT NOT NULL DEFAULT 'balanced', max_position_pct REAL NOT NULL DEFAULT 20,
          max_sector_pct REAL NOT NULL DEFAULT 40, notification_route TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watchlist_items (
          account_id TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT NOT NULL, asset_type TEXT NOT NULL DEFAULT 'stock',
          status TEXT NOT NULL DEFAULT 'watch', entry_low_units INTEGER, entry_high_units INTEGER,
          max_chase_units INTEGER, planned_amount_cents INTEGER, planned_quantity INTEGER,
          invalid_units INTEGER, target_units INTEGER, note TEXT, expires_at TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(account_id,symbol), FOREIGN KEY(account_id) REFERENCES accounts(account_id)
        );
        CREATE TABLE IF NOT EXISTS positions (
          position_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT NOT NULL,
          asset_type TEXT NOT NULL DEFAULT 'stock', average_cost_units INTEGER NOT NULL,
          quantity INTEGER NOT NULL, highest_price_units INTEGER, stop_price_units INTEGER,
          target_price_units INTEGER, status TEXT NOT NULL DEFAULT 'active', opened_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, UNIQUE(account_id,symbol),
          FOREIGN KEY(account_id) REFERENCES accounts(account_id)
        );
        CREATE TABLE IF NOT EXISTS trades (
          trade_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, position_id TEXT NOT NULL,
          symbol TEXT NOT NULL, side TEXT NOT NULL, price_units INTEGER NOT NULL, quantity INTEGER NOT NULL,
          fee_cents INTEGER NOT NULL DEFAULT 0, traded_at TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
          note TEXT, created_at TEXT NOT NULL, FOREIGN KEY(account_id) REFERENCES accounts(account_id),
          FOREIGN KEY(position_id) REFERENCES positions(position_id)
        );
        CREATE INDEX IF NOT EXISTS idx_trades_account_time ON trades(account_id,traded_at,symbol);
        CREATE TABLE IF NOT EXISTS portfolio_audit_log (
          audit_id TEXT PRIMARY KEY, account_id TEXT, action TEXT NOT NULL, entity_type TEXT NOT NULL,
          entity_id TEXT, detail_json TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS portfolio_advice (
          advice_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
          phase TEXT NOT NULL, action TEXT NOT NULL, action_ratio_pct REAL NOT NULL,
          trend_score REAL, trend_band TEXT, confidence REAL, tolerance_pct REAL,
          reasons_json TEXT NOT NULL, triggers_json TEXT NOT NULL, invalidation_json TEXT NOT NULL,
          detail_json TEXT NOT NULL, generated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
          FOREIGN KEY(account_id) REFERENCES accounts(account_id)
        );
        CREATE INDEX IF NOT EXISTS idx_portfolio_advice_latest ON portfolio_advice(account_id,symbol,generated_at);
        CREATE TABLE IF NOT EXISTS auction_snapshots (
          trade_date TEXT NOT NULL, captured_at TEXT NOT NULL, symbol TEXT NOT NULL,
          price_units INTEGER NOT NULL, previous_close_units INTEGER, volume INTEGER, amount_cents INTEGER,
          provider TEXT, data_quality TEXT NOT NULL DEFAULT 'partial',
          PRIMARY KEY(trade_date,captured_at,symbol)
        );
        CREATE TABLE IF NOT EXISTS notification_outbox (
          message_id TEXT PRIMARY KEY, notification_route TEXT NOT NULL, account_id TEXT NOT NULL,
          dedupe_key TEXT NOT NULL UNIQUE, message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          created_at TEXT NOT NULL, emitted_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_outbox_route ON notification_outbox(notification_route,status,created_at);
        CREATE TABLE IF NOT EXISTS portfolio_quote_snapshots (
          captured_at TEXT NOT NULL, symbol TEXT NOT NULL, price_units INTEGER NOT NULL,
          volume INTEGER, amount_cents INTEGER, provider TEXT, PRIMARY KEY(captured_at,symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_portfolio_quotes ON portfolio_quote_snapshots(symbol,captured_at);
        """)
        outbox_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(notification_outbox)")}
        if "delivery_status" not in outbox_columns:
            self.connection.execute("ALTER TABLE notification_outbox ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'pending'")
        if "handed_to_hermes_at" not in outbox_columns:
            self.connection.execute("ALTER TABLE notification_outbox ADD COLUMN handed_to_hermes_at TEXT")
        self.connection.commit()

    def _audit(self, account_id: str | None, action: str, entity_type: str, entity_id: str | None, detail: Any, actor: str) -> None:
        self.connection.execute("INSERT INTO portfolio_audit_log VALUES (?,?,?,?,?,?,?,?)",
                                (uuid.uuid4().hex, account_id, action, entity_type, entity_id,
                                 json.dumps(detail, ensure_ascii=False), actor, _now()))

    def list_accounts(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM accounts WHERE status!='deleted' ORDER BY created_at").fetchall()
        return [self._account(dict(row)) for row in rows]

    def create_account(self, name: str, initial_capital: Any = 0, notification_route: str | None = None,
                       risk_profile: str = "balanced", actor: str = "system") -> dict[str, Any]:
        name = str(name).strip()
        if not name or len(name) > 40:
            raise ValueError("账户名称不能为空且不能超过40字")
        if risk_profile not in {"conservative", "balanced", "aggressive"}:
            raise ValueError("风险偏好无效")
        cents, now, account_id = _money(initial_capital), _now(), uuid.uuid4().hex
        try:
            with self.connection:
                self.connection.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                        (account_id, name, "active", cents, cents, risk_profile, 20, 40,
                                         notification_route, now, now))
                self._audit(account_id, "create", "account", account_id, {"name": name, "initial_capital": _yuan(cents)}, actor)
        except sqlite3.IntegrityError as exc:
            raise ValueError("账户名称已存在") from exc
        return self.get_account(account_id)

    def get_account(self, account_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM accounts WHERE account_id=? AND status!='deleted'", (account_id,)).fetchone()
        if not row:
            raise KeyError("账户不存在")
        return self._account(dict(row))

    def update_account(self, account_id: str, *, name: str | None = None, initial_capital: Any | None = None,
                       risk_profile: str | None = None, max_position_pct: float | None = None,
                       max_sector_pct: float | None = None, notification_route: str | None = None,
                       actor: str = "user") -> dict[str, Any]:
        current = self.get_account(account_id)
        values: dict[str, Any] = {}
        if name is not None:
            name = str(name).strip()
            if not name or len(name) > 40:
                raise ValueError("账户名称无效")
            values["name"] = name
        if risk_profile is not None:
            if risk_profile not in {"conservative", "balanced", "aggressive"}:
                raise ValueError("风险偏好无效")
            values["risk_profile"] = risk_profile
        for key, value in (("max_position_pct", max_position_pct), ("max_sector_pct", max_sector_pct)):
            if value is not None:
                value = float(value)
                if not 0 < value <= 100:
                    raise ValueError("仓位上限必须在0到100之间")
                values[key] = value
        if notification_route is not None:
            values["notification_route"] = notification_route
        if initial_capital is not None:
            new_cents = _money(initial_capital)
            delta = new_cents - int(round(current["initial_capital"] * 100))
            if int(round(current["cash_balance"] * 100)) + delta < 0:
                raise ValueError("调整总资金后现金不能为负")
            values["initial_capital_cents"] = new_cents
            values["cash_balance_cents"] = int(round(current["cash_balance"] * 100)) + delta
        if not values:
            return current
        values["updated_at"] = _now()
        with self.connection:
            self.connection.execute(f"UPDATE accounts SET {','.join(f'{key}=?' for key in values)} WHERE account_id=?",
                                    (*values.values(), account_id))
            self._audit(account_id, "update", "account", account_id, values, actor)
        return self.get_account(account_id)

    @staticmethod
    def _account(row: dict[str, Any]) -> dict[str, Any]:
        row["initial_capital"] = _yuan(row.pop("initial_capital_cents"))
        row["cash_balance"] = _yuan(row.pop("cash_balance_cents"))
        return row

    def upsert_watchlist(self, account_id: str, symbol: str, name: str | None = None, *, status: str = "watch",
                         asset_type: str = "stock", entry_low: Any | None = None, entry_high: Any | None = None,
                         max_chase_price: Any | None = None, planned_amount: Any | None = None,
                         planned_quantity: int | None = None, invalid_price: Any | None = None,
                         target_price: Any | None = None, note: str = "", expires_at: str | None = None,
                         actor: str = "user") -> dict[str, Any]:
        self.get_account(account_id)
        symbol = _symbol(symbol)
        if status not in {"watch", "planned"} or asset_type not in {"stock", "etf"}:
            raise ValueError("自选状态或资产类型无效")
        if planned_quantity is not None and (int(planned_quantity) < 0 or int(planned_quantity) % 100):
            raise ValueError("计划买入数量必须是100股的整数倍")
        now = _now()
        values = (account_id, symbol, (name or symbol).strip()[:40], asset_type, status,
                  _price(entry_low) if entry_low is not None else None,
                  _price(entry_high) if entry_high is not None else None,
                  _price(max_chase_price) if max_chase_price is not None else None,
                  _money(planned_amount) if planned_amount is not None else None,
                  int(planned_quantity) if planned_quantity is not None else None,
                  _price(invalid_price) if invalid_price is not None else None,
                  _price(target_price) if target_price is not None else None,
                  str(note)[:500], expires_at, now, now)
        with self.connection:
            self.connection.execute("""
              INSERT INTO watchlist_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(account_id,symbol) DO UPDATE SET name=excluded.name,asset_type=excluded.asset_type,
                status=excluded.status,entry_low_units=excluded.entry_low_units,entry_high_units=excluded.entry_high_units,
                max_chase_units=excluded.max_chase_units,planned_amount_cents=excluded.planned_amount_cents,
                planned_quantity=excluded.planned_quantity,invalid_units=excluded.invalid_units,
                target_units=excluded.target_units,note=excluded.note,expires_at=excluded.expires_at,updated_at=excluded.updated_at
            """, values)
            self._audit(account_id, "upsert", "watchlist", symbol, {"status": status}, actor)
        return next(row for row in self.list_watchlist(account_id) if row["symbol"] == symbol)

    def list_watchlist(self, account_id: str) -> list[dict[str, Any]]:
        self.get_account(account_id)
        rows = self.connection.execute("SELECT * FROM watchlist_items WHERE account_id=? ORDER BY updated_at DESC", (account_id,)).fetchall()
        result = []
        for source in rows:
            row = dict(source)
            for stored, public in (("entry_low_units", "entry_low"), ("entry_high_units", "entry_high"),
                                   ("max_chase_units", "max_chase_price"), ("invalid_units", "invalid_price"),
                                   ("target_units", "target_price")):
                row[public] = _price_value(row.pop(stored))
            row["planned_amount"] = _yuan(row.pop("planned_amount_cents"))
            result.append(row)
        return result

    def remove_watchlist(self, account_id: str, symbol: str, actor: str = "user") -> None:
        symbol = _symbol(symbol)
        with self.connection:
            cursor = self.connection.execute("DELETE FROM watchlist_items WHERE account_id=? AND symbol=?", (account_id, symbol))
            if not cursor.rowcount:
                raise KeyError("自选不存在")
            self._audit(account_id, "remove", "watchlist", symbol, {}, actor)

    def record_trade(self, account_id: str, symbol: str, side: str, price: Any, quantity: int, *,
                     fee: Any = 0, traded_at: str | None = None, name: str | None = None,
                     asset_type: str = "stock", idempotency_key: str, note: str = "", actor: str = "user") -> dict[str, Any]:
        account = self.get_account(account_id)
        symbol, side, quantity = _symbol(symbol), side.lower(), int(quantity)
        if side not in {"buy", "sell"}:
            raise ValueError("成交方向必须是buy或sell")
        if quantity <= 0 or (side == "buy" and quantity % 100):
            raise ValueError("买入数量必须是100股整数倍，卖出数量必须大于0")
        if not idempotency_key or len(idempotency_key) > 100:
            raise ValueError("缺少有效幂等键")
        existing = self.connection.execute("SELECT trade_id,account_id FROM trades WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing:
            if str(existing["account_id"]) != account_id:
                raise ValueError("幂等键已被其他账户使用")
            return self.get_trade(existing[0])
        price_units, fee_cents = _price(price), _money(fee)
        traded_at = traded_at or _now()
        position = self.connection.execute("SELECT * FROM positions WHERE account_id=? AND symbol=?", (account_id, symbol)).fetchone()
        gross_cents = int((Decimal(price_units) * quantity / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        now, trade_id = _now(), uuid.uuid4().hex
        with self.connection:
            if side == "buy":
                total_cost = gross_cents + fee_cents
                cash_cents = int(round(account["cash_balance"] * 100))
                if total_cost > cash_cents:
                    raise ValueError("账户现金不足")
                if position:
                    old_qty, old_cost = int(position["quantity"]), int(position["average_cost_units"])
                    new_qty = old_qty + quantity
                    average = int(round((old_cost * old_qty + price_units * quantity + fee_cents * 100) / new_qty))
                    position_id = str(position["position_id"])
                    self.connection.execute("""UPDATE positions SET average_cost_units=?,quantity=?,status='active',
                      highest_price_units=MAX(COALESCE(highest_price_units,0),?),updated_at=? WHERE position_id=?""",
                                            (average, new_qty, price_units, now, position_id))
                else:
                    position_id = uuid.uuid4().hex
                    self.connection.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                            (position_id, account_id, symbol, (name or symbol)[:40], asset_type,
                                             price_units + int(round(fee_cents * 100 / quantity)), quantity,
                                             price_units, None, None, "active", traded_at, now))
                # 关注项确认买入后提升为真实持仓，避免同一账户重复显示和重复监控。
                self.connection.execute("DELETE FROM watchlist_items WHERE account_id=? AND symbol=?", (account_id, symbol))
                self.connection.execute("UPDATE accounts SET cash_balance_cents=cash_balance_cents-?,updated_at=? WHERE account_id=?",
                                        (total_cost, now, account_id))
            else:
                if not position or int(position["quantity"]) <= 0:
                    raise ValueError("账户没有该持仓")
                available = self._available_quantity(account_id, symbol, int(position["quantity"]), traded_at[:10])
                if quantity > available:
                    raise ValueError(f"可卖数量不足，当前可卖{available}股")
                position_id = str(position["position_id"])
                new_qty = int(position["quantity"]) - quantity
                self.connection.execute("UPDATE positions SET quantity=?,status=?,average_cost_units=?,updated_at=? WHERE position_id=?",
                                        (new_qty, "closed" if new_qty == 0 else "active",
                                         0 if new_qty == 0 else position["average_cost_units"], now, position_id))
                self.connection.execute("UPDATE accounts SET cash_balance_cents=cash_balance_cents+?,updated_at=? WHERE account_id=?",
                                        (gross_cents - fee_cents, now, account_id))
            self.connection.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (trade_id, account_id, position_id, symbol, side, price_units, quantity,
                                     fee_cents, traded_at, idempotency_key, str(note)[:500], now))
            self._audit(account_id, "trade", "position", position_id,
                        {"symbol": symbol, "side": side, "price": _price_value(price_units), "quantity": quantity}, actor)
        return self.get_trade(trade_id)

    def correct_position(self, account_id: str, symbol: str, quantity: int, average_cost: Any, *,
                         stop_price: Any | None = None, target_price: Any | None = None,
                         note: str = "", actor: str = "user") -> dict[str, Any]:
        """人工纠正持仓台账，同时按账面成本差额调整现金并留下审计记录。"""
        account = self.get_account(account_id)
        symbol, quantity = _symbol(symbol), int(quantity)
        if quantity < 0:
            raise ValueError("持仓数不能小于0")
        position = self.connection.execute("SELECT * FROM positions WHERE account_id=? AND symbol=?", (account_id, symbol)).fetchone()
        if not position:
            raise KeyError("账户没有该持仓")
        new_cost_units = _price(average_cost)
        if quantity > 0 and new_cost_units <= 0:
            raise ValueError("持仓成本必须大于0")
        old_qty, old_cost = int(position["quantity"]), int(position["average_cost_units"])
        old_value_cents = int((Decimal(old_cost) * old_qty / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        new_value_cents = int((Decimal(new_cost_units) * quantity / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        cash_cents = int(round(account["cash_balance"] * 100))
        new_cash_cents = cash_cents + old_value_cents - new_value_cents
        if new_cash_cents < 0:
            raise ValueError("纠正后账户现金不能为负")
        stop_units = position["stop_price_units"] if stop_price is None else _nullable_price(stop_price)
        target_units = position["target_price_units"] if target_price is None else _nullable_price(target_price)
        now = _now()
        with self.connection:
            self.connection.execute("""UPDATE positions SET average_cost_units=?,quantity=?,status=?,stop_price_units=?,target_price_units=?,updated_at=? WHERE position_id=?""",
                                    (new_cost_units if quantity else 0, quantity, "active" if quantity else "closed",
                                     stop_units, target_units, now, str(position["position_id"])))
            self.connection.execute("UPDATE accounts SET cash_balance_cents=?,updated_at=? WHERE account_id=?",
                                    (new_cash_cents, now, account_id))
            self._audit(account_id, "correct", "position", str(position["position_id"]), {
                "symbol": symbol, "old_quantity": old_qty, "new_quantity": quantity,
                "old_average_cost": _price_value(old_cost), "new_average_cost": _price_value(new_cost_units),
                "cash_delta": _yuan(new_cash_cents - cash_cents), "note": str(note)[:300],
            }, actor)
        return self.portfolio(account_id)

    def _available_quantity(self, account_id: str, symbol: str, current_quantity: int, trade_date: str | None = None) -> int:
        trade_date = trade_date or date.today().isoformat()
        bought_today = self.connection.execute("""SELECT COALESCE(SUM(quantity),0) FROM trades
          WHERE account_id=? AND symbol=? AND side='buy' AND substr(traded_at,1,10)=?""",
                                               (account_id, symbol, trade_date)).fetchone()[0]
        return max(0, current_quantity - int(bought_today or 0))

    def get_trade(self, trade_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
        if not row:
            raise KeyError("成交不存在")
        result = dict(row)
        result["price"] = _price_value(result.pop("price_units"))
        result["fee"] = _yuan(result.pop("fee_cents"))
        return result

    def portfolio(self, account_id: str, prices: dict[str, float] | None = None) -> dict[str, Any]:
        account, prices = self.get_account(account_id), prices or {}
        rows = self.connection.execute("SELECT * FROM positions WHERE account_id=? AND quantity>0 ORDER BY updated_at DESC", (account_id,)).fetchall()
        positions, market_value_cents, cost_cents = [], 0, 0
        for source in rows:
            row = dict(source)
            quantity, cost = int(row["quantity"]), _price_value(row["average_cost_units"]) or 0
            current = float(prices.get(row["symbol"]) or cost)
            market_value = round(current * quantity, 2)
            position_cost = round(cost * quantity, 2)
            market_value_cents += _money(market_value)
            cost_cents += _money(position_cost)
            row.update({"average_cost": cost, "current_price": current, "market_value": market_value,
                        "cost_value": position_cost, "pnl": round(market_value - position_cost, 2),
                        "pnl_pct": round((current / cost - 1) * 100, 2) if cost else None,
                        "available_quantity": self._available_quantity(account_id, row["symbol"], quantity)})
            for stored, public in (("highest_price_units", "highest_price"), ("stop_price_units", "stop_price"),
                                   ("target_price_units", "target_price")):
                row[public] = _price_value(row.pop(stored))
            row.pop("average_cost_units")
            positions.append(row)
        cash_cents = int(round(account["cash_balance"] * 100))
        total_cents = cash_cents + market_value_cents
        return {"account": account, "positions": positions, "watchlist": self.list_watchlist(account_id),
                "summary": {"total_assets": _yuan(total_cents), "cash": _yuan(cash_cents),
                            "market_value": _yuan(market_value_cents), "position_cost": _yuan(cost_cents),
                            "pnl": _yuan(market_value_cents - cost_cents),
                            "position_pct": round(market_value_cents / total_cents * 100, 2) if total_cents else 0}}

    def audit_log(self, account_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("""SELECT * FROM portfolio_audit_log
          WHERE account_id=? ORDER BY created_at DESC LIMIT ?""", (account_id, min(max(limit, 1), 500))).fetchall()]

    def save_advice(self, row: dict[str, Any]) -> dict[str, Any]:
        advice_id = str(row.get("advice_id") or uuid.uuid4().hex)
        detail = dict(row, advice_id=advice_id)
        with self.connection:
            self.connection.execute("""INSERT OR REPLACE INTO portfolio_advice VALUES
              (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                advice_id, row["account_id"], row["symbol"], row["phase"], row["action"],
                row.get("action_ratio_pct", 0), row.get("trend_score"), row.get("trend_band"),
                row.get("confidence"), row.get("tolerance_pct"),
                json.dumps(row.get("reasons") or [], ensure_ascii=False),
                json.dumps(row.get("trigger_conditions") or [], ensure_ascii=False),
                json.dumps(row.get("invalidation") or [], ensure_ascii=False),
                json.dumps(detail, ensure_ascii=False), row["generated_at"], row["expires_at"],
            ))
        return detail

    def latest_advice(self, account_id: str, symbol: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params: list[Any] = [account_id]
        clause = "account_id=?"
        if symbol:
            clause += " AND symbol=?"; params.append(symbol)
        params.append(min(max(limit, 1), 500))
        rows = self.connection.execute(f"SELECT detail_json FROM portfolio_advice WHERE {clause} ORDER BY generated_at DESC LIMIT ?", params).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_auction_snapshot(self, captured_at: str, symbol: str, price: Any, previous_close: Any | None,
                              volume: int | None, amount: Any | None, provider: str, quality: str = "partial") -> None:
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO auction_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                                    (captured_at[:10], captured_at, _symbol(symbol), _price(price),
                                     _price(previous_close) if previous_close else None, volume,
                                     _money(amount) if amount is not None else None, provider, quality))

    def auction_samples(self, symbol: str, trade_date: str | None = None) -> list[dict[str, Any]]:
        trade_date = trade_date or date.today().isoformat()
        rows = self.connection.execute("SELECT * FROM auction_snapshots WHERE symbol=? AND trade_date=? ORDER BY captured_at",
                                       (_symbol(symbol), trade_date)).fetchall()
        result = []
        for source in rows:
            row = dict(source); row["price"] = _price_value(row.pop("price_units")); row["previous_close"] = _price_value(row.pop("previous_close_units")); row["amount"] = _yuan(row.pop("amount_cents")); result.append(row)
        return result

    def enqueue(self, route: str, account_id: str, dedupe_key: str, message: str) -> bool:
        if not route or not message: return False
        try:
            with self.connection:
                self.connection.execute("""INSERT INTO notification_outbox
                  (message_id,notification_route,account_id,dedupe_key,message,status,created_at,emitted_at,delivery_status,handed_to_hermes_at)
                  VALUES (?,?,?,?,?,'pending',?,NULL,'pending',NULL)""",
                                        (uuid.uuid4().hex, route, account_id, dedupe_key, message, _now()))
            return True
        except sqlite3.IntegrityError:
            return False

    def emit_route(self, route: str, limit: int = 20) -> list[str]:
        rows = self.connection.execute("SELECT message_id,message FROM notification_outbox WHERE notification_route=? AND status='pending' ORDER BY created_at LIMIT ?",
                                       (route, min(max(limit, 1), 100))).fetchall()
        if not rows: return []
        now = _now()
        with self.connection:
            self.connection.executemany("""UPDATE notification_outbox SET status='emitted',emitted_at=?,
              delivery_status='handed_to_hermes',handed_to_hermes_at=? WHERE message_id=?""",
                                        [(now, now, row["message_id"]) for row in rows])
        return [str(row["message"]) for row in rows]

    def save_quote(self, captured_at: str, symbol: str, price: Any, volume: int | None = None,
                   amount: Any | None = None, provider: str = "quote") -> None:
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO portfolio_quote_snapshots VALUES (?,?,?,?,?,?)",
                                    (captured_at, _symbol(symbol), _price(price), volume,
                                     _money(amount) if amount is not None else None, provider))

    def recent_quotes(self, symbol: str, limit: int = 10) -> list[dict[str, Any]]:
        rows=self.connection.execute("SELECT * FROM portfolio_quote_snapshots WHERE symbol=? ORDER BY captured_at DESC LIMIT ?",(_symbol(symbol),min(max(limit,1),100))).fetchall()
        result=[]
        for source in reversed(rows):
            row=dict(source);row["price"]=_price_value(row.pop("price_units"));row["amount"]=_yuan(row.pop("amount_cents"));result.append(row)
        return result

    def prune(self, days: int = 90) -> None:
        cutoff=(datetime.now()-timedelta(days=days)).isoformat(timespec="seconds")
        quote_cutoff=(datetime.now()-timedelta(days=min(days,30))).isoformat(timespec="seconds")
        with self.connection:
            self.connection.execute("DELETE FROM portfolio_quote_snapshots WHERE captured_at<?",(quote_cutoff,))
            self.connection.execute("DELETE FROM auction_snapshots WHERE captured_at<?",(cutoff,))
            self.connection.execute("DELETE FROM portfolio_advice WHERE generated_at<?",(cutoff,))
            self.connection.execute("DELETE FROM notification_outbox WHERE status='emitted' AND emitted_at<?",(cutoff,))
