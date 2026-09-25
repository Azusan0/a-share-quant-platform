#!/usr/bin/env python3
"""M2-M4中央扫描：行情去重、盘前情景和动态持仓建议入队。"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from auction_analyzer import analyze_auction
from advice_presenter import plan_card, position_card, render_qq
from global_market_factor import factor_for_stock, load_snapshot
from market_calendar import market_phase, now_shanghai
from overnight_plan import build_plan
from portfolio_store import DEFAULT_DB, PortfolioStore
from position_advice import evaluate_position
from position_sizing import suggest_position_size


SNAPSHOT_DB = Path("/root/.hermes/scripts/a_share_market_snapshots.db")
SENTIMENT = Path("/root/.hermes/scripts/a_share_market_sentiment.json")
DYNAMIC = Path("/root/.hermes/scripts/a_share_dynamic_pool.json")
FUNDAMENTAL = Path("/root/.hermes/scripts/a_share_fundamental_snapshot.json")
GLOBAL_MARKET = Path("/root/.hermes/scripts/a_share_global_market_factor.json")


def _direction_matches(direction: str, labels: list[str]) -> bool:
    if not labels:
        return True
    direction = str(direction or "").replace("行业", "").replace("板块", "").replace("概念", "").strip().lower()
    aliases = {
        "煤炭": ("煤炭", "动力煤", "煤化工"), "石油": ("石油", "油气", "天然气", "页岩气", "油服"),
        "化纤": ("化纤", "化学纤维", "玻纤", "玻璃纤维"), "玻璃": ("玻璃", "玻纤"),
        "有色金属": ("有色", "铝", "钨", "小金属", "稀土", "贵金属", "黄金", "白银", "工业金属"),
    }.get(direction, (direction,))
    texts = [str(label).lower() for label in labels if label]
    if any(alias in text for alias in aliases for text in texts):
        return True
    if direction == "煤炭" and any(word in text for text in texts for word in ("医药", "医疗", "生物", "cro")):
        return False
    return bool(direction)


def _board_labels(connection: sqlite3.Connection, symbols: set[str]) -> dict[str, list[str]]:
    labels: dict[str, list[str]] = {}
    for symbol in symbols:
        try:
            row = connection.execute(
                "SELECT concept_tags_json FROM stock_board_memberships WHERE symbol=? ORDER BY generated_at DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            if row:
                labels[symbol] = list(dict.fromkeys(str(item) for item in json.loads(row[0] or "[]") if item))
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            continue
    return labels


def _load(path: Path, default: Any) -> Any:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return default


def phase_at(now: datetime) -> str:
    phase = market_phase(now)
    if phase in {"auction_cancelable", "auction_locked"}:
        return "auction"
    if phase in {"premarket", "opening", "overnight", "intraday"}:
        return phase
    if phase in {"closed"}:
        return "closed"
    if phase in {"intraday_break", "after_close"}:
        return "overnight"
    return "intraday"


def _evidence(symbols: set[str], snapshots: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    result = {symbol: {} for symbol in symbols}
    snapshots = snapshots or {}
    market = _load(SENTIMENT, {}); market_score = market.get("score", 50)
    pool = _load(DYNAMIC, {}); fundamentals = {str(row.get("symbol")): row for row in _load(FUNDAMENTAL, {}).get("stocks", [])}
    global_market = load_snapshot(GLOBAL_MARKET, max_age_minutes=20)
    global_available = bool(global_market.get("indices")) and not global_market.get("stale", True)
    bindings = {str(row.get("symbol")): row for row in global_market.get("stock_bindings") or []}
    board_labels: dict[str, list[str]] = {}
    if SNAPSHOT_DB.exists():
        try:
            conn = sqlite3.connect(f"file:{SNAPSHOT_DB}?mode=ro", uri=True, timeout=3)
            board_labels = _board_labels(conn, symbols)
            conn.close()
        except sqlite3.Error:
            board_labels = {}
    for symbol, labels in board_labels.items():
        result[symbol]["stock_labels"] = labels[:30]
        result[symbol]["sector_name"] = labels[0] if labels else None
    for sector in pool.get("active_sectors") or []:
        for member in sector.get("members") or []:
            symbol = str(member.get("symbol"));
            if symbol in result:
                direction = str(sector.get("direction") or sector.get("label") or "")
                labels = board_labels.get(symbol, [])
                if _direction_matches(direction, labels):
                    result[symbol].update(sector_score=sector.get("score", 50), sector_weak=float(sector.get("score") or 0)<35,
                                          sector_name=direction, stock_labels=list(dict.fromkeys([*labels, direction])) if direction else labels)
                elif direction:
                    result[symbol].setdefault("sector_mapping_conflict", []).append(direction)
    if SNAPSHOT_DB.exists():
        try:
            c=sqlite3.connect(f"file:{SNAPSHOT_DB}?mode=ro&immutable=1",uri=True); c.row_factory=sqlite3.Row
            for symbol in symbols:
                state=c.execute("SELECT state,bars_in_state,metrics_json,sector FROM intraday_states WHERE symbol=?",(symbol,)).fetchone()
                diag=c.execute("SELECT diagnosis_json FROM technical_diagnosis_snapshots WHERE symbol=? ORDER BY generated_at DESC LIMIT 1",(symbol,)).fetchone()
                if state:
                    metrics=json.loads(state["metrics_json"]); labels=result[symbol].setdefault("stock_labels",[])
                    # intraday_states.sector 可能来自旧动态池，不能覆盖权威板块标签。
                    if not labels and state["sector"]:
                        result[symbol]["sector_mapping_conflict"] = [str(state["sector"])]
                    vol_ratio = metrics.get("volume_ratio_5m")
                    ret5 = metrics.get("return_5m_pct")
                    try:
                        volume_down = bool(vol_ratio is not None and ret5 is not None and float(vol_ratio) >= 1.2 and float(ret5) < 0)
                    except (TypeError, ValueError):
                        volume_down = None
                    result[symbol].update(intraday_state=state["state"],vwap=metrics.get("vwap"),vwap_deviation_pct=metrics.get("vwap_deviation_pct"),below_vwap_bars=state["bars_in_state"] if float(metrics.get("vwap_deviation_pct") or 0)<0 else 0,volume_down=volume_down,rebound_failed=None)
                if diag:
                    row=json.loads(diag[0]); result[symbol].update(technical_score=row.get("technical_score"),support_price=row.get("support_price"),resistance_price=row.get("resistance_price"),missing_data=row.get("missing_data") or [])
                try:
                    membership=c.execute("SELECT concept_tags_json FROM stock_board_memberships WHERE symbol=? ORDER BY generated_at DESC LIMIT 1",(symbol,)).fetchone()
                    if membership and not result[symbol].get("stock_labels"):
                        labels=result[symbol].setdefault("stock_labels",[])
                        labels.extend(label for label in json.loads(membership[0]) if label not in labels)
                except (sqlite3.Error,json.JSONDecodeError):pass
            c.close()
        except Exception: pass
    for symbol in symbols:
        fundamental=fundamentals.get(symbol) or {}
        domestic_drivers = list((fundamental.get("opposing_evidence") or [])[:2]) + list((fundamental.get("support_evidence") or [])[:2])
        result[symbol].update(market_score=market_score,hard_risks=fundamental.get("hard_risks") or [],
                              domestic_drivers=domestic_drivers[:4],global_market_available=global_available)
        binding=bindings.get(symbol)
        binding_labels = [str(item) for item in (binding or {}).get("labels") or []]
        binding_trusted = bool(binding) and (not board_labels.get(symbol) or any(
            str(label).lower() in " ".join(board_labels.get(symbol, [])).lower()
            or str(board_label).lower() in " ".join(binding_labels).lower()
            for label in binding_labels for board_label in board_labels.get(symbol, [])
        ))
        if binding_trusted and not result[symbol].get("sector_mapping_conflict"):
            factor={key:binding.get(key) for key in ("global_affected","global_sector_score","global_impact","global_confidence","global_sectors","global_drivers")}
        else:
            snap=snapshots.get(symbol) or {}
            factor=factor_for_stock(symbol,str(snap.get("name") or symbol),result[symbol].get("stock_labels") or [],global_market)
        if global_available:
            result[symbol].update(factor)
        elif factor.get("global_affected"):
            result[symbol].update(factor,global_sector_score=50,global_impact="neutral",global_confidence=0,global_drivers=[])
            result[symbol]["missing_data"]=list(dict.fromkeys([*(result[symbol].get("missing_data") or []),"美日韩跨市场快照过期或缺失"]))
    return result


def run(db_path: Path = DEFAULT_DB, now: datetime | None = None, forced_phase: str | None = None) -> dict[str, Any]:
    now=now or now_shanghai().replace(tzinfo=None); phase=forced_phase or phase_at(now)
    with PortfolioStore(db_path) as store:
        accounts=store.list_accounts(); portfolios={a["account_id"]:store.portfolio(a["account_id"]) for a in accounts}
        if phase == "closed":
            return {"phase":"closed","accounts":len(accounts),"symbols":0,"advice":0,"queued":0}
        positions=[p for aid,data in portfolios.items() for p in data["positions"]]
        watch_items=[p for aid,data in portfolios.items() for p in data["watchlist"]]
        symbols={p["symbol"] for p in positions+watch_items}
        if not symbols: return {"phase":phase,"accounts":len(accounts),"symbols":0,"advice":0,"queued":0}
        from data_source import fetch_snapshot
        snapshots=fetch_snapshot(sorted(symbols)); evidence=_evidence(symbols,snapshots); queued=advice_count=0
        captured_at=now.isoformat(timespec="seconds")
        for symbol,snap in snapshots.items():
            price=float(snap.get("price") or 0)
            if not price:continue
            store.save_quote(captured_at,symbol,price,int(snap.get("volume") or 0),snap.get("amount"),str(snap.get("provider") or "quote"))
            quotes=store.recent_quotes(symbol,5);vwap=float((evidence.get(symbol) or {}).get("vwap") or 0)
            if vwap:
                consecutive=0
                for quote in reversed(quotes):
                    if float(quote["price"])<vwap:consecutive+=1
                    else:break
                evidence[symbol]["below_vwap_bars"]=max(int(evidence[symbol].get("below_vwap_bars") or 0),consecutive)
            if len(quotes)>=3:
                prices=[float(row["price"]) for row in quotes[-3:]];evidence[symbol]["rebound_failed"]=prices[-1]<prices[-2] and prices[-1]<max(prices)
        for account in accounts:
            route=account.get("notification_route")
            account_portfolio=portfolios[account["account_id"]]
            held_symbols={row["symbol"] for row in account_portfolio["positions"]}
            plan_items=account_portfolio["positions"]+[row for row in account_portfolio["watchlist"] if row["symbol"] not in held_symbols]
            if phase in {"overnight","premarket","auction","opening"}:
                for item in plan_items:
                    symbol=item["symbol"];snap=snapshots.get(symbol) or {};ev=dict(evidence.get(symbol) or {})
                    if phase in {"overnight","premarket"} and not ev.get("global_market_available"):
                        ev["missing_data"]=list(dict.fromkeys([*(ev.get("missing_data") or []),"隔夜外部市场/A50稳定数据源"]))
                    if phase=="auction":
                        price=float(snap.get("price") or 0);change=float(snap.get("change_pct") or 0);prev=price/(1+change/100) if price and change>-99 else 0
                        if price:store.save_auction_snapshot(now.isoformat(timespec="seconds"),symbol,price,prev,int(snap.get("volume") or 0),snap.get("amount"),str(snap.get("provider") or "quote"))
                        ev["auction"]=analyze_auction(store.auction_samples(symbol),prev,ev.get("sector_breadth_pct")) if prev else {}
                    plan=build_plan(symbol,item.get("name") or symbol,ev,phase,now)
                    global_factor=plan.get("global_factor") or {}
                    if phase=="auction" and now.hour*60+now.minute<9*60+25:continue
                    current_value=next((float(p.get("market_value") or 0) for p in account_portfolio["positions"] if p["symbol"]==symbol),0)
                    entry_ref=item.get("entry_high") or item.get("entry_low") or snap.get("price")
                    invalid_ref=item.get("invalid_price") or plan.get("support_price")
                    sizing=suggest_position_size(entry_price=entry_ref,invalid_price=invalid_ref,
                        total_assets=account_portfolio["summary"].get("total_assets"),cash=account_portfolio["summary"].get("cash"),
                        risk_profile=account.get("risk_profile","balanced"),max_position_pct=account.get("max_position_pct",20),
                        current_symbol_value=current_value)
                    message=render_qq(plan_card(account["name"],item,plan,sizing))
                    store.save_advice({**plan,"account_id":account["account_id"],"action":"watch","action_ratio_pct":0,
                                       "trend_score":None,"trend_band":plan["bias"],"tolerance_pct":None,
                                       "reasons":[*(global_factor.get("drivers") or [])[:3],*[scenario["trigger"] for scenario in plan["scenarios"]]],
                                       "trigger_conditions":[scenario["trigger"] for scenario in plan["scenarios"]],
                                       "invalidation":["数据更新后重新计算"]})
                    key=f"{account['account_id']}:{symbol}:{phase}:{now.date()}"
                    queued+=int(store.enqueue(route,account["account_id"],key,message));advice_count+=1
            for position in account_portfolio["positions"]:
                symbol=position["symbol"]; snap=snapshots.get(symbol) or {}; ev=dict(evidence.get(symbol) or {})
                if phase not in {"overnight","premarket","auction","opening"}:
                    previous=(store.latest_advice(account["account_id"],symbol,1) or [None])[0]
                    advice=evaluate_position(position,snap,ev,account.get("risk_profile","balanced"),previous,now,phase)
                    store.save_advice(advice); advice_count+=1
                    if advice["action"] in {"add", "protect", "trim", "exit"} and (not previous or previous.get("action")!=advice["action"] or previous.get("trend_band")!=advice["trend_band"]):
                        message=render_qq(position_card(account["name"],advice))
                        key=f"{account['account_id']}:{symbol}:{advice['action']}:{advice['trend_band']}:{now.strftime('%Y%m%d%H%M')[:-1]}"
                        queued+=int(store.enqueue(route,account["account_id"],key,message))
            if phase=="intraday":
                for item in account_portfolio["watchlist"]:
                    if item.get("status")!="planned":continue
                    snap=snapshots.get(item["symbol"]) or {};price=float(snap.get("price") or 0);low=item.get("entry_low");high=item.get("entry_high")
                    if price and low is not None and high is not None and float(low)<=price<=float(high):
                        key=f"{account['account_id']}:{item['symbol']}:entry-zone:{now.strftime('%Y%m%d%H')}"
                        sizing=suggest_position_size(entry_price=price,invalid_price=item.get("invalid_price"),
                            total_assets=account_portfolio["summary"].get("total_assets"),cash=account_portfolio["summary"].get("cash"),
                            risk_profile=account.get("risk_profile","balanced"),max_position_pct=account.get("max_position_pct",20))
                        card={"title":f"{account['name']} · 计划入场","name":item["name"],"symbol":item["symbol"],
                              "action":"价格进入计划区间","entry_low":low,"entry_high":high,
                              "invalid_price":item.get("invalid_price"),"sizing":sizing,
                              "confidence_band":"中","data_quality":"partial",
                              "reasons":["已进入人工设置的计划区间"],"expires_at":item.get("expires_at")}
                        message=render_qq(card)
                        queued+=int(store.enqueue(route,account["account_id"],key,message))
        store.prune(90)
        return {"phase":phase,"accounts":len(accounts),"symbols":len(symbols),"advice":advice_count,"queued":queued}


def main()->int:
    p=argparse.ArgumentParser();p.add_argument("--db",default=str(DEFAULT_DB));p.add_argument("--phase",choices=["overnight","premarket","auction","opening","intraday"]);a=p.parse_args();print(json.dumps(run(Path(a.db),forced_phase=a.phase),ensure_ascii=False));return 0
if __name__=="__main__":raise SystemExit(main())
