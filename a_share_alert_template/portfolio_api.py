#!/usr/bin/env python3
"""多账户网页API路由。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from portfolio_store import PortfolioStore


def _resolve_symbol(symbol: str) -> tuple[str, str]:
    """网页只输入代码时补全名称和资产类型；数据源失败则保留代码。"""
    name = symbol
    try:
        import sys
        template = "/usr/local/lib/hermes-agent/a_share_alert_template"
        if template not in sys.path:
            sys.path.insert(0, template)
        from data_source import fetch_snapshot
        snapshot = fetch_snapshot([symbol]).get(symbol) or {}
        name = str(snapshot.get("name") or symbol)
    except Exception:
        pass
    return name, "etf" if "ETF" in name.upper() else "stock"


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    initial_capital: float = Field(default=0, ge=0)
    risk_profile: str = "balanced"
    notification_route: str | None = None


class AccountUpdate(BaseModel):
    name: str | None = None
    initial_capital: float | None = Field(default=None, ge=0)
    risk_profile: str | None = None
    max_position_pct: float | None = None
    max_sector_pct: float | None = None
    notification_route: str | None = None


class WatchlistWrite(BaseModel):
    symbol: str
    name: str | None = None
    status: str = "watch"
    asset_type: str = "stock"
    entry_low: float | None = None
    entry_high: float | None = None
    max_chase_price: float | None = None
    planned_amount: float | None = None
    planned_quantity: int | None = None
    invalid_price: float | None = None
    target_price: float | None = None
    note: str = ""
    expires_at: str | None = None


class TradeWrite(BaseModel):
    symbol: str
    name: str | None = None
    side: str
    price: float = Field(gt=0)
    quantity: int = Field(gt=0)
    fee: float = Field(default=0, ge=0)
    traded_at: str | None = None
    asset_type: str = "stock"
    note: str = ""


class PositionEdit(BaseModel):
    quantity: int = Field(ge=0)
    average_cost: float = Field(gt=0)
    stop_price: float | None = Field(default=None, ge=0)
    target_price: float | None = Field(default=None, ge=0)
    note: str = "人工纠正持仓台账"


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc).strip("'"))
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


def create_portfolio_router(db_path: str | Path, read_auth: Callable[..., Any], write_auth: Callable[..., Any],
                            price_loader: Callable[[], dict[str, float]]) -> APIRouter:
    router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

    @router.get("/accounts")
    def accounts(_: str = Depends(read_auth)):
        with PortfolioStore(db_path) as store: return store.list_accounts()

    @router.post("/accounts")
    def create_account(body: AccountCreate, actor: str = Depends(write_auth)):
        try:
            with PortfolioStore(db_path) as store:
                return store.create_account(body.name, body.initial_capital, body.notification_route, body.risk_profile, actor)
        except Exception as exc: raise _error(exc)

    @router.patch("/accounts/{account_id}")
    def update_account(account_id: str, body: AccountUpdate, actor: str = Depends(write_auth)):
        try:
            with PortfolioStore(db_path) as store:
                return store.update_account(account_id, actor=actor, **body.model_dump(exclude_unset=True))
        except Exception as exc: raise _error(exc)

    @router.get("/accounts/{account_id}")
    def portfolio(account_id: str, _: str = Depends(read_auth)):
        try:
            with PortfolioStore(db_path) as store: return store.portfolio(account_id, price_loader())
        except Exception as exc: raise _error(exc)

    @router.post("/accounts/{account_id}/watchlist")
    def add_watchlist(account_id: str, body: WatchlistWrite, actor: str = Depends(write_auth)):
        try:
            values = body.model_dump()
            if not values.get("name"):
                values["name"], values["asset_type"] = _resolve_symbol(values["symbol"])
            # 加入自选只是关注；计划入场只能由后续独立功能显式创建。
            values.update(status="watch", entry_low=None, entry_high=None, max_chase_price=None,
                          planned_amount=None, planned_quantity=None, invalid_price=None,
                          target_price=None, expires_at=None)
            with PortfolioStore(db_path) as store:
                return store.upsert_watchlist(account_id, actor=actor, **values)
        except Exception as exc: raise _error(exc)

    @router.patch("/accounts/{account_id}/watchlist/{symbol}")
    def edit_watchlist(account_id: str, symbol: str, body: WatchlistWrite, actor: str = Depends(write_auth)):
        try:
            values = body.model_dump(); values["symbol"] = symbol
            with PortfolioStore(db_path) as store: return store.upsert_watchlist(account_id, actor=actor, **values)
        except Exception as exc: raise _error(exc)

    @router.delete("/accounts/{account_id}/watchlist/{symbol}")
    def remove_watchlist(account_id: str, symbol: str, actor: str = Depends(write_auth)):
        try:
            with PortfolioStore(db_path) as store: store.remove_watchlist(account_id, symbol, actor)
            return {"ok": True}
        except Exception as exc: raise _error(exc)

    @router.patch("/accounts/{account_id}/positions/{symbol}")
    def edit_position(account_id: str, symbol: str, body: PositionEdit, actor: str = Depends(write_auth)):
        try:
            with PortfolioStore(db_path) as store:
                store.correct_position(account_id, symbol, actor=actor, **body.model_dump())
                return store.portfolio(account_id, price_loader())
        except Exception as exc: raise _error(exc)

    @router.get("/symbols/{symbol}")
    def resolve_symbol(symbol: str, _: str = Depends(read_auth)):
        """按股票代码返回名称和资产类型，供成交录入页面只读展示。"""
        symbol = symbol.strip()
        if not symbol or len(symbol) > 10:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="股票代码无效")
        name, asset_type = _resolve_symbol(symbol)
        return {"symbol": symbol, "name": name, "asset_type": asset_type}

    @router.post("/accounts/{account_id}/trades")
    def trade(account_id: str, body: TradeWrite, idempotency_key: str = Header(alias="Idempotency-Key"),
              actor: str = Depends(write_auth)):
        try:
            # 成交记录名称由股票代码自动解析，忽略前端手工填写，避免代码与名称错配。
            resolved_name, resolved_asset_type = _resolve_symbol(body.symbol)
            values = body.model_dump()
            values["name"] = resolved_name
            values["asset_type"] = resolved_asset_type
            with PortfolioStore(db_path) as store:
                return store.record_trade(account_id, idempotency_key=idempotency_key, actor=actor, **values)
        except Exception as exc: raise _error(exc)

    @router.get("/accounts/{account_id}/advice")
    def advice(account_id: str, _: str = Depends(read_auth)):
        try:
            with PortfolioStore(db_path) as store: return store.latest_advice(account_id)
        except Exception as exc: raise _error(exc)

    @router.get("/accounts/{account_id}/audit-log")
    def audit(account_id: str, _: str = Depends(read_auth)):
        with PortfolioStore(db_path) as store: return store.audit_log(account_id)

    return router
