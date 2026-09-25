#!/usr/bin/env python3
"""动态池基本面、估值与风险事件快照。"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Callable

import akshare as ak
import pandas as pd

from data_source import fetch_snapshot
from snapshot_store import SnapshotStore


DEFAULT_OUTPUT = Path("/root/.hermes/scripts/a_share_fundamental_snapshot.json")
DEFAULT_POOL = Path("/root/.hermes/scripts/a_share_dynamic_pool.json")
DEFAULT_DB = Path("/root/.hermes/scripts/a_share_market_snapshots.db")


def _num(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, str):
        value = value.strip().replace("%", "").replace(",", "")
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _date_text(value: Any) -> str | None:
    if value in (None, "", "-"):
        return None
    try:
        return pd.Timestamp(value).date().isoformat()
    except Exception:
        return str(value)[:10]


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _pool_members(pool: dict[str, Any]) -> list[dict[str, str]]:
    unique: dict[str, dict[str, str]] = {}
    for sector in pool.get("active_sectors") or []:
        direction = str(sector.get("direction") or sector.get("label") or "未知")
        for member in sector.get("members") or []:
            symbol = str(member.get("symbol") or "")
            if symbol:
                unique[symbol] = {
                    "symbol": symbol,
                    "name": str(member.get("name") or symbol),
                    "sector": direction,
                }
    return list(unique.values())


def parse_financial_frame(frame: pd.DataFrame) -> dict[str, Any]:
    """解析同花顺英文指标明细，避免依赖展示顺序。"""
    if frame is None or frame.empty or not {"report_date", "metric_name", "value"}.issubset(frame.columns):
        return {}
    work = frame.copy()
    work["report_date"] = pd.to_datetime(work["report_date"], errors="coerce")
    work = work.dropna(subset=["report_date"])
    if work.empty:
        return {}
    latest = work["report_date"].max()
    rows = work[work["report_date"] == latest]
    metrics = {str(row["metric_name"]): _num(row.get("value")) for _, row in rows.iterrows()}
    return {
        "report_date": latest.date().isoformat(),
        "revenue": metrics.get("operating_income_total"),
        "revenue_yoy_pct": metrics.get("calculate_operating_income_total_yoy_growth_ratio"),
        "net_profit": metrics.get("parent_holder_net_profit"),
        "profit_yoy_pct": metrics.get("calculate_parent_holder_net_profit_yoy_growth_ratio"),
        "roe_pct": metrics.get("index_weighted_avg_roe") or metrics.get("index_full_diluted_roe"),
        "ocf_per_share": metrics.get("index_per_operating_cash_flow_net"),
        "debt_ratio_pct": metrics.get("assets_debt_ratio"),
        "gross_margin_pct": metrics.get("sale_gross_margin"),
        "net_margin_pct": metrics.get("sale_net_interest_ratio"),
        "provider": "ths_financial_abstract",
    }


def parse_balance_frame(frame: pd.DataFrame) -> dict[str, Any]:
    """提取最新一期商誉、总资产和归母净资产。"""
    if frame is None or frame.empty or not {"report_date", "metric_name", "value"}.issubset(frame.columns):
        return {}
    work = frame.copy()
    work["report_date"] = pd.to_datetime(work["report_date"], errors="coerce")
    work = work.dropna(subset=["report_date"])
    if work.empty:
        return {}
    latest = work["report_date"].max()
    rows = work[work["report_date"] == latest]
    metrics = {str(row["metric_name"]): _num(row.get("value")) for _, row in rows.iterrows()}
    goodwill = metrics.get("goodwill")
    if "goodwill" in metrics and goodwill is None:
        goodwill = 0.0
    net_assets = metrics.get("parent_holder_equity_total") or metrics.get("holder_equity_total")
    total_assets = metrics.get("assets_total")
    return {
        "balance_report_date": latest.date().isoformat(),
        "goodwill": goodwill,
        "net_assets": net_assets,
        "total_assets": total_assets,
        "goodwill_net_assets_ratio_pct": _round(goodwill / net_assets * 100 if goodwill is not None and net_assets else None),
        "goodwill_assets_ratio_pct": _round(goodwill / total_assets * 100 if goodwill is not None and total_assets else None),
        "balance_provider": "ths_balance_sheet",
    }


def _fetch_financial(symbol: str) -> dict[str, Any]:
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        abstract = parse_financial_frame(ak.stock_financial_abstract_new_ths(symbol=symbol))
        balance = parse_balance_frame(ak.stock_financial_debt_new_ths(symbol=symbol))
    return {**abstract, **balance}


def _fetch_notices(symbol: str, start: date, end: date) -> list[dict[str, Any]]:
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        frame = ak.stock_individual_notice_report(
            security=symbol,
            symbol="全部",
            begin_date=start.isoformat(),
            end_date=end.isoformat(),
        )
    if frame is None or frame.empty:
        return []
    results = []
    for _, row in frame.head(120).iterrows():
        results.append({
            "title": str(row.get("公告标题") or ""),
            "type": str(row.get("公告类型") or ""),
            "date": _date_text(row.get("公告日期")),
            "url": str(row.get("网址") or ""),
        })
    return results


def _fetch_parallel(
    symbols: list[str], worker: Callable[[str], Any], max_workers: int = 4,
) -> tuple[dict[str, Any], dict[str, str]]:
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        tasks = {executor.submit(worker, symbol): symbol for symbol in symbols}
        for future in as_completed(tasks):
            symbol = tasks[future]
            try:
                values[symbol] = future.result()
            except Exception as exc:
                errors[symbol] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return values, errors


def _forecast_period(now: datetime) -> str:
    if now.month <= 4:
        return f"{now.year - 1}1231"
    if now.month <= 8:
        return f"{now.year}0630"
    if now.month <= 10:
        return f"{now.year}0930"
    return f"{now.year}1231"


def parse_performance_forecasts(
    frame: pd.DataFrame, symbols: set[str], report_period: str,
) -> dict[str, dict[str, Any]]:
    """解析东财业绩预告，优先选择归母净利润指标。"""
    results: dict[str, dict[str, Any]] = {}
    if frame is None or frame.empty or "股票代码" not in frame.columns:
        return results
    candidates: dict[str, list[dict[str, Any]]] = {}
    for _, row in frame.iterrows():
        symbol = str(row.get("股票代码") or "").zfill(6)
        if symbol not in symbols:
            continue
        metric = str(row.get("预测指标") or "")
        priority = 0 if metric == "归属于上市公司股东的净利润" else 1 if "净利润" in metric and "扣除" not in metric else 2
        candidates.setdefault(symbol, []).append({
            "report_period": f"{report_period[:4]}-{report_period[4:6]}-{report_period[6:]}",
            "metric": metric, "forecast_type": str(row.get("预告类型") or ""),
            "change_pct": _round(_num(row.get("业绩变动幅度"))),
            "forecast_value": _round(_num(row.get("预测数值"))),
            "prior_value": _round(_num(row.get("上年同期值"))),
            "description": str(row.get("业绩变动") or "")[:500],
            "reason": str(row.get("业绩变动原因") or "")[:500],
            "announced_at": _date_text(row.get("公告日期")),
            "provider": "eastmoney_performance_forecast", "_priority": priority,
        })
    for symbol, rows in candidates.items():
        best_priority = min(row["_priority"] for row in rows)
        selected = max((row for row in rows if row["_priority"] == best_priority), key=lambda row: row.get("announced_at") or "")
        selected.pop("_priority", None)
        results[symbol] = selected
    return results


def _fetch_performance_forecasts(
    symbols: set[str], now: datetime,
) -> tuple[dict[str, dict[str, Any]], str, str | None]:
    period = _forecast_period(now)
    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            frame = ak.stock_yjyg_em(date=period)
        return parse_performance_forecasts(frame, symbols, period), period, None
    except Exception as exc:
        return {}, period, f"{type(exc).__name__}: {str(exc)[:160]}"


def _fetch_unlocks(symbols: set[str], now: datetime, lookahead_days: int) -> dict[str, list[dict[str, Any]]]:
    end = now.date() + timedelta(days=lookahead_days)
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        frame = ak.stock_restricted_release_detail_em(
            start_date=now.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
        )
    results = {symbol: [] for symbol in symbols}
    if frame is None or frame.empty:
        return results
    for _, row in frame.iterrows():
        symbol = str(row.get("股票代码") or "").zfill(6)
        if symbol not in results:
            continue
        ratio = _num(row.get("占解禁前流通市值比例"))
        results[symbol].append({
            "date": _date_text(row.get("解禁时间")),
            "type": str(row.get("限售股类型") or ""),
            "shares": _num(row.get("实际解禁数量")),
            "market_value": _num(row.get("实际解禁市值")),
            "float_market_cap_ratio_pct": _round(ratio * 100 if ratio is not None else None, 2),
        })
    return results


def _fetch_pledges(symbols: set[str], now: datetime) -> tuple[dict[str, dict[str, Any]], str | None]:
    for offset in range(0, 10):
        target = now.date() - timedelta(days=offset)
        if target.weekday() >= 5:
            continue
        try:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                frame = ak.stock_gpzy_pledge_ratio_em(date=target.strftime("%Y%m%d"))
            if frame is None or frame.empty:
                continue
            results: dict[str, dict[str, Any]] = {}
            for _, row in frame.iterrows():
                symbol = str(row.get("股票代码") or "").zfill(6)
                if symbol in symbols:
                    results[symbol] = {
                        "pledge_ratio_pct": _num(row.get("质押比例")),
                        "pledge_count": int(_num(row.get("质押笔数"), 0) or 0),
                        "pledge_date": _date_text(row.get("交易日期")) or target.isoformat(),
                    }
            return results, target.isoformat()
        except Exception:
            continue
    return {}, None


NOTICE_RULES = [
    ("立案", "监管立案"), ("行政处罚", "行政处罚"), ("退市风险", "退市风险"),
    ("重大诉讼", "重大诉讼"), ("预亏", "业绩预亏"), ("商誉减值", "商誉减值"),
    ("股份减持", "股东减持"), ("减持计划", "股东减持"), ("被实施ST", "ST风险"),
]

CONTRACT_RULES = [
    ("终止重大合同", "合同终止", "risk"), ("合同终止", "合同终止", "risk"),
    ("终止合同", "合同终止", "risk"), ("解除合同", "合同终止", "risk"),
    ("中标通知", "项目中标", "positive"), ("项目中标", "项目中标", "positive"),
    ("重大合同", "重大合同", "positive"), ("签订合同", "合同签署", "positive"),
    ("合同签署", "合同签署", "positive"), ("战略合作", "战略合作", "neutral"),
]


def _amount_from_title(title: str) -> float | None:
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*(亿元|亿|万元|万)", title)
    if not matches:
        return None
    values = [float(number) * (100_000_000 if unit in {"亿元", "亿"} else 10_000) for number, unit in matches]
    return max(values) if values else None


def _major_contracts(notices: list[dict[str, Any]], revenue: float | None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for notice in notices:
        title = str(notice.get("title") or "")
        for keyword, label, impact in CONTRACT_RULES:
            if keyword not in title:
                continue
            key = (str(notice.get("date") or ""), title)
            if key in seen:
                break
            amount = _amount_from_title(title)
            results.append({
                "label": label, "impact": impact, "date": key[0], "title": title,
                "url": str(notice.get("url") or ""), "amount": _round(amount),
                "revenue_ratio_pct": _round(amount / revenue * 100 if amount is not None and revenue else None),
                "amount_quality": "title_extracted" if amount is not None else "missing",
            })
            seen.add(key)
            break
    return results[:8]


def _notice_risks(notices: list[dict[str, Any]]) -> list[dict[str, str]]:
    risks: list[dict[str, str]] = []
    seen: set[str] = set()
    for notice in notices:
        title = str(notice.get("title") or "")
        for keyword, label in NOTICE_RULES:
            if keyword in title and label not in seen:
                if label == "股东减持" and any(text in title for text in ("不减持", "终止减持", "增持")):
                    continue
                risks.append({"label": label, "date": str(notice.get("date") or ""), "title": title})
                seen.add(label)
    return risks[:5]


def assess_fundamental(
    base: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    financial = base.get("financial") or {}
    valuation = base.get("valuation") or {}
    pledge = base.get("pledge") or {}
    unlocks = sorted(base.get("unlocks") or [], key=lambda row: row.get("date") or "")
    notices = base.get("notices") or []
    performance_forecast = base.get("performance_forecast") or None
    pe_ttm = _num(valuation.get("pe_ttm"))
    pb = _num(valuation.get("pb"))
    revenue_yoy = _num(financial.get("revenue_yoy_pct"))
    profit_yoy = _num(financial.get("profit_yoy_pct"))
    roe = _num(financial.get("roe_pct"))
    ocf = _num(financial.get("ocf_per_share"))
    debt = _num(financial.get("debt_ratio_pct"))
    pledge_ratio = _num(pledge.get("pledge_ratio_pct"))
    notice_risks = _notice_risks(notices)
    contracts = _major_contracts(notices, _num(financial.get("revenue")))
    goodwill = _num(financial.get("goodwill"))
    goodwill_ratio = _num(financial.get("goodwill_net_assets_ratio_pct"))
    upcoming = unlocks[0] if unlocks else None
    unlock_ratio = _num(upcoming.get("float_market_cap_ratio_pct")) if upcoming else None
    days_to_unlock = None
    if upcoming and upcoming.get("date"):
        try:
            days_to_unlock = (date.fromisoformat(upcoming["date"]) - now.date()).days
        except ValueError:
            pass

    supports: list[str] = []
    oppositions: list[str] = []
    missing: list[str] = []
    score = 50.0
    hard_risks: list[str] = []
    if revenue_yoy is None:
        missing.append("营收同比")
    elif revenue_yoy >= 10:
        supports.append(f"营收同比增长{revenue_yoy:.1f}%")
        score += 8
    elif revenue_yoy < 0:
        oppositions.append(f"营收同比下降{abs(revenue_yoy):.1f}%")
        score -= 10
    if profit_yoy is None:
        missing.append("归母净利润同比")
    elif profit_yoy >= 10:
        supports.append(f"归母净利润同比增长{profit_yoy:.1f}%")
        score += 10
    elif profit_yoy < 0:
        oppositions.append(f"归母净利润同比下降{abs(profit_yoy):.1f}%")
        score -= 12
    if roe is None:
        missing.append("ROE")
    elif roe >= 10:
        supports.append(f"ROE {roe:.1f}%")
        score += 8
    elif roe < 5:
        oppositions.append(f"ROE仅{roe:.1f}%")
        score -= 6
    if ocf is None:
        missing.append("每股经营现金流")
    elif ocf > 0:
        supports.append("经营现金流为正")
        score += 6
    else:
        oppositions.append("每股经营现金流为负")
        score -= 10
    if debt is None:
        missing.append("资产负债率")
    elif debt >= 70:
        oppositions.append(f"资产负债率{debt:.1f}%偏高")
        score -= 10
    elif debt <= 50:
        supports.append(f"资产负债率{debt:.1f}%")
        score += 5
    if pe_ttm is None or pe_ttm == 0:
        missing.append("PE(TTM)")
    elif pe_ttm < 0:
        oppositions.append("PE为负，当前处于亏损估值")
        score -= 8
    elif pe_ttm > 80:
        oppositions.append(f"PE(TTM) {pe_ttm:.1f}偏高")
        score -= 6
    if pb is None or pb == 0:
        missing.append("PB")
    elif pb > 8:
        oppositions.append(f"PB {pb:.1f}偏高")
        score -= 5
    if pledge_ratio is None:
        missing.append("股权质押比例")
    elif pledge_ratio >= 30:
        oppositions.append(f"股权质押比例{pledge_ratio:.1f}%")
        score -= 10
        if pledge_ratio >= 50:
            hard_risks.append("高比例股权质押")
    if upcoming and days_to_unlock is not None and days_to_unlock <= 90 and (unlock_ratio or 0) >= 5:
        oppositions.append(f"{days_to_unlock}天后解禁，占流通市值{unlock_ratio:.1f}%")
        score -= 12
    for risk in notice_risks:
        if risk["label"] == "业绩预亏" and performance_forecast:
            continue
        oppositions.append(f"{risk['date']} {risk['label']}")
        score -= 8

    if goodwill is None:
        missing.append("商誉")
    elif goodwill_ratio is None:
        missing.append("商誉净资产比")
    elif goodwill_ratio >= 50:
        oppositions.append(f"商誉占归母净资产{goodwill_ratio:.1f}%过高")
        score -= 18
        hard_risks.append("高商誉")
    elif goodwill_ratio >= 30:
        oppositions.append(f"商誉占归母净资产{goodwill_ratio:.1f}%")
        score -= 10
    elif goodwill_ratio <= 10:
        supports.append(f"商誉占归母净资产{goodwill_ratio:.1f}%")
        score += 2

    for contract in contracts:
        ratio = _num(contract.get("revenue_ratio_pct"))
        if contract["impact"] == "risk":
            oppositions.append(f"{contract['date']} {contract['label']}")
            score -= 12
            hard_risks.append(contract["label"])
        elif contract["impact"] == "positive" and ratio is not None and ratio >= 10:
            supports.append(f"{contract['label']}金额约占营收{ratio:.1f}%")
            score += 4

    if performance_forecast:
        forecast_type = str(performance_forecast.get("forecast_type") or "")
        forecast_change = _num(performance_forecast.get("change_pct"))
        if any(word in forecast_type for word in ("首亏", "续亏", "预亏", "减亏")):
            oppositions.append(f"业绩预告{forecast_type}")
            score -= 15 if "减亏" not in forecast_type else 8
            if "减亏" not in forecast_type:
                hard_risks.append("业绩预亏")
        elif "预减" in forecast_type or (forecast_change is not None and forecast_change <= -30):
            oppositions.append(f"业绩预告{forecast_type or f'同比{forecast_change:.1f}%'}")
            score -= 12
        elif any(word in forecast_type for word in ("扭亏", "预增", "略增")) or (forecast_change or 0) >= 20:
            supports.append(f"业绩预告{forecast_type or f'同比增长{forecast_change:.1f}%'}")
            score += 6

    hard_notice_labels = {"监管立案", "行政处罚", "退市风险", "重大诉讼", "ST风险"}
    hard_risks.extend(risk["label"] for risk in notice_risks if risk["label"] in hard_notice_labels)
    if upcoming and days_to_unlock is not None and days_to_unlock <= 30 and (unlock_ratio or 0) >= 15:
        hard_risks.append("临近大比例解禁")

    score = round(max(0, min(100, score)), 1)
    hard_risks = list(dict.fromkeys(hard_risks))
    risk_level = "high" if hard_risks or score < 35 else "medium" if score < 60 or oppositions or len(missing) >= 4 else "low"
    return {
        "score_version": "fundamental_v2",
        "fundamental_score": score,
        "risk_level": risk_level,
        "report_date": financial.get("report_date"),
        "pe_ttm": _round(pe_ttm), "pb": _round(pb),
        "roe_pct": _round(roe), "revenue_yoy_pct": _round(revenue_yoy),
        "profit_yoy_pct": _round(profit_yoy), "ocf_per_share": _round(ocf, 4),
        "debt_ratio_pct": _round(debt), "gross_margin_pct": _round(_num(financial.get("gross_margin_pct"))),
        "pledge_ratio_pct": _round(pledge_ratio), "pledge_date": pledge.get("pledge_date"),
        "goodwill": _round(goodwill), "net_assets": _round(_num(financial.get("net_assets"))),
        "goodwill_net_assets_ratio_pct": _round(goodwill_ratio),
        "upcoming_unlock": upcoming, "days_to_unlock": days_to_unlock,
        "performance_forecast": performance_forecast,
        "major_contracts": contracts,
        "event_risks": notice_risks,
        "hard_risks": hard_risks,
        "support_evidence": supports,
        "opposing_evidence": oppositions,
        "missing_data": list(dict.fromkeys(missing)),
        "data_quality": "complete" if not missing else "partial" if financial or valuation else "missing",
        "providers": list(dict.fromkeys(filter(None, [financial.get("provider"), financial.get("balance_provider"), valuation.get("provider"), "eastmoney_unlock", "eastmoney_notice", "eastmoney_performance_forecast" if performance_forecast else None]))),
    }


def collect(
    pool: dict[str, Any], now: datetime, *, lookback_days: int = 180, lookahead_days: int = 180,
    max_workers: int = 4,
) -> dict[str, Any]:
    members = _pool_members(pool)
    symbols = [row["symbol"] for row in members]
    snapshots = fetch_snapshot(symbols) if symbols else {}
    financials, financial_errors = _fetch_parallel(symbols, _fetch_financial, max_workers)
    start = now.date() - timedelta(days=lookback_days)
    notices, notice_errors = _fetch_parallel(
        symbols, lambda symbol: _fetch_notices(symbol, start, now.date()), max_workers,
    )
    provider_errors: dict[str, str] = {}
    try:
        unlocks = _fetch_unlocks(set(symbols), now, lookahead_days)
    except Exception as exc:
        unlocks = {symbol: [] for symbol in symbols}
        provider_errors["unlock"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    try:
        pledges, pledge_date = _fetch_pledges(set(symbols), now)
    except Exception as exc:
        pledges, pledge_date = {}, None
        provider_errors["pledge"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    forecasts, forecast_period, forecast_error = _fetch_performance_forecasts(set(symbols), now)
    if forecast_error:
        provider_errors["performance_forecast"] = forecast_error

    rows: list[dict[str, Any]] = []
    for member in members:
        symbol = member["symbol"]
        snapshot = snapshots.get(symbol) or {}
        base = {
            "financial": financials.get(symbol) or {},
            "valuation": {
                "pe_ttm": snapshot.get("pe_ttm"), "pb": snapshot.get("pb"),
                "market_cap": snapshot.get("market_cap"), "float_market_cap": snapshot.get("float_market_cap"),
                "provider": "tencent_quote" if snapshot else None,
            },
            "pledge": pledges.get(symbol) or {},
            "unlocks": unlocks.get(symbol) or [],
            "notices": notices.get(symbol) or [],
            "performance_forecast": forecasts.get(symbol),
        }
        diagnosis = assess_fundamental(base, now)
        rows.append({**member, **diagnosis})
    rows.sort(key=lambda row: (row["risk_level"] == "high", -row["fundamental_score"]), reverse=True)
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "source_scope": "dynamic_pool",
        "pledge_data_date": pledge_date,
        "performance_forecast_period": forecast_period,
        "stocks": rows,
        "summary": {
            "total": len(rows),
            "high_risk": sum(row["risk_level"] == "high" for row in rows),
            "medium_risk": sum(row["risk_level"] == "medium" for row in rows),
            "missing": sum(row["data_quality"] == "missing" for row in rows),
        },
        "source_health": {
            "financial_ok": len(financials), "financial_errors": financial_errors,
            "notice_ok": len(notices), "notice_errors": notice_errors,
            "provider_errors": provider_errors,
            "performance_forecast_ok": len(forecasts),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="动态池基本面与风险事件快照")
    parser.add_argument("--pool", default=str(DEFAULT_POOL))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--max-age-hours", type=float, default=6)
    parser.add_argument("--lookback-days", type=int, default=180)
    parser.add_argument("--lookahead-days", type=int, default=180)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    now = datetime.now()
    pool = _load_json(Path(args.pool), {})
    members = _pool_members(pool)
    expected_symbols = {row["symbol"] for row in members}
    if not expected_symbols:
        return 0
    output = Path(args.output)
    existing = _load_json(output, {})
    if not args.force:
        try:
            generated = datetime.fromisoformat(str(existing.get("generated_at")))
            existing_symbols = {str(row.get("symbol")) for row in existing.get("stocks") or []}
            fresh = generated.date() == now.date() and (now - generated).total_seconds() < args.max_age_hours * 3600
            if fresh and expected_symbols.issubset(existing_symbols):
                return 0
        except Exception:
            pass
    payload = collect(
        pool, now, lookback_days=args.lookback_days, lookahead_days=args.lookahead_days,
        max_workers=max(1, args.max_workers),
    )
    with SnapshotStore(args.db) as store:
        store.save_fundamentals(payload["generated_at"], payload["stocks"])
        store.prune(90)
    _atomic_json(output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
