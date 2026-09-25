from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from data_source import fetch_histories, fetch_snapshot
from delivery import emit_alert
from strategies import evaluate_exit_signal, evaluate_signal

# 以下四个模块为「增强能力」，任一缺失或出错都不应拖垮监控主流程，故全部 try 导入并降级为 no-op。
try:
    from market_regime import evaluate_market_regime  # #3 大盘择时
except Exception:  # pragma: no cover
    evaluate_market_regime = None
try:
    from risk_veto import assess_symbol as assess_risk  # #2 利空否决
except Exception:  # pragma: no cover
    assess_risk = None
try:
    from signal_journal import record_push  # #4 自评估
except Exception:  # pragma: no cover
    record_push = None
try:
    from recommendation_consumer import load_executable_recommendations, to_monitor_alert
except Exception:  # pragma: no cover
    load_executable_recommendations = None
    to_monitor_alert = None

MARKET_DB = Path("/root/.hermes/scripts/a_share_market_snapshots.db")


def _direction_matches_board(direction: str, labels: list[str]) -> bool:
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


def _validate_pool_board_mapping(pool: list[dict[str, Any]], market_db: Path = MARKET_DB) -> list[dict[str, Any]]:
    """防止动态/静态池的方向字段污染个股真实行业，尤其避免错误推送。"""
    if not market_db.exists():
        return pool
    labels: dict[str, list[str]] = {}
    try:
        conn = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True, timeout=3)
        for item in pool:
            for member in item.get("members") or []:
                symbol = str(member.get("symbol") or "")
                row = conn.execute("SELECT concept_tags_json FROM stock_board_memberships WHERE symbol=? ORDER BY generated_at DESC LIMIT 1", (symbol,)).fetchone()
                if row:
                    try:
                        labels[symbol] = [str(x) for x in json.loads(row[0] or "[]") if x]
                    except (TypeError, json.JSONDecodeError):
                        pass
        conn.close()
    except sqlite3.Error:
        return pool
    validated = []
    for item in pool:
        direction = str(item.get("direction") or "")
        members = [member for member in item.get("members") or [] if _direction_matches_board(direction, labels.get(str(member.get("symbol") or ""), []))]
        if members:
            validated.append({**item, "members": members})
    return validated


def _sortable_score(value: Any) -> float:
    """把用于排序的分数规整成实数：None/NaN/inf 一律视为最低分。

    策略层量能缺失时 score 可能为 NaN，若直接排序会出现未定义行为、
    甚至把指标缺失的标的排到最前误当「最佳」推送。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return float("-inf")
    if math.isnan(num) or math.isinf(num):
        return float("-inf")
    return num


def _round_or_none(price: Any, pct: Any) -> float | None:
    """按 price*(1+pct/100) 算出止损/目标价；price 不可用则返回 None。"""
    try:
        p = float(price)
        r = float(pct)
    except (TypeError, ValueError):
        return None
    if math.isnan(p) or math.isinf(p) or p <= 0:
        return None
    return round(p * (1 + r / 100.0), 3)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _regime_knobs(regime: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """把 regime 的 level 翻译成 build_direction_alert 用的两个调节旋钮。

    risk_off（大盘偏弱）：抬高入场门槛 + 强制启用过热熔断，减少逆势追单。
    阈值可在 config.market_regime 覆盖。旋钮直接并入 regime dict 返回。
    """
    cfg = config.get("market_regime", {}) if isinstance(config, dict) else {}
    off_bonus = float(cfg.get("risk_off_min_score_bonus", 8.0))
    neutral_bonus = float(cfg.get("neutral_min_score_bonus", 0.0))
    level = regime.get("level", "neutral")
    if level == "risk_off":
        regime["min_score_bonus"] = off_bonus
        regime["force_suppress_overheated"] = True
    elif level == "neutral":
        regime["min_score_bonus"] = neutral_bonus
        regime["force_suppress_overheated"] = bool(cfg.get("neutral_force_suppress", False))
    else:  # risk_on
        regime["min_score_bonus"] = 0.0
        regime["force_suppress_overheated"] = False
    return regime


def _assess_regime(config: dict[str, Any]) -> dict[str, Any]:
    """#3 大盘择时：评估当前大盘环境。模块缺失/出错时返回中性档，不影响主流程。"""
    if evaluate_market_regime is None:
        return _regime_knobs({"level": "neutral", "score": 0, "reasons": ["择时模块未加载"]}, config)
    try:
        return _regime_knobs(evaluate_market_regime(config), config)
    except Exception as exc:  # pragma: no cover
        print(f"market regime assess failed, fallback neutral: {exc}", file=sys.stderr)
        return _regime_knobs({"level": "neutral", "score": 0, "reasons": [f"择时失败:{exc}"]}, config)


def _apply_risk_veto(alert: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """#2 利空否决：命中入场信号后做一次利空校验，命中确定性利空则降级为观望。

    模块缺失/全部数据源与模型都不可用时，返回原 alert（放行），绝不因风控层故障漏发或误杀。
    """
    if assess_risk is None:
        return alert
    try:
        verdict = assess_risk(
            alert.get("symbol", ""),
            alert.get("name", ""),
            config,
        )
    except Exception as exc:  # pragma: no cover
        print(f"risk veto failed, pass through: {exc}", file=sys.stderr)
        return alert
    if not verdict or not verdict.get("veto"):
        return alert
    # 命中利空：降级为「利空观望」提醒，不再发入场。
    reasons = verdict.get("reasons", [])
    alert = dict(alert)
    alert["signal_type"] = "watch"
    alert["strategy_label"] = "利空观望"
    alert["veto_reasons"] = reasons
    alert["veto_source"] = verdict.get("source", "unknown")
    alert["message"] = (
        f"{alert.get('name', alert.get('symbol', ''))} 技术面触发信号，"
        f"但检测到潜在利空，建议回避：{'、'.join(str(r) for r in reasons)}"
    )
    return alert


def _record_push_to_journal(alert: dict[str, Any], config: dict[str, Any]) -> None:
    """#4 自评估：把已投递的提醒登记进流水，供事后 T+1/T+3 复盘。出错不影响主流程。"""
    if record_push is None:
        return
    try:
        record_push(config, alert)
    except Exception as exc:  # pragma: no cover
        print(f"signal journal record failed: {exc}", file=sys.stderr)


def _load_portfolio_holdings(config: dict[str, Any]) -> list[dict[str, Any]]:
    """从多账户数据库加载真实持仓，作为旧主监控唯一的持仓来源。

    关注项不会进入离场策略。相同股票存在于多个账户时按持股数聚合成本，
    避免同一次扫描重复抓取行情和重复发送通用离场提醒。
    """
    db_path = Path(config.get("portfolio_db", "/var/lib/a-share-dashboard/portfolios.db"))
    if not db_path.exists():
        print(f"portfolio database missing, holdings disabled: {db_path}", file=sys.stderr)
        return []

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT p.symbol,p.name,p.asset_type,p.average_cost_units,p.quantity,
                   p.highest_price_units,p.opened_at
              FROM positions p
              JOIN accounts a ON a.account_id=p.account_id
             WHERE p.quantity>0 AND p.status='active' AND a.status='active'
             ORDER BY p.symbol,p.opened_at
            """
        ).fetchall()
    except (sqlite3.Error, OSError) as exc:  # pragma: no cover - 生产降级路径
        print(f"portfolio holdings load failed, holdings disabled: {exc}", file=sys.stderr)
        return []
    finally:
        if connection is not None:
            connection.close()

    aggregated: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row["symbol"])
        quantity = int(row["quantity"] or 0)
        if quantity <= 0:
            continue
        cost = float(row["average_cost_units"] or 0) / 10000
        highest = float(row["highest_price_units"] or row["average_cost_units"] or 0) / 10000
        item = aggregated.setdefault(
            symbol,
            {
                "symbol": symbol,
                "name": str(row["name"] or symbol),
                "type": str(row["asset_type"] or "stock"),
                "quantity": 0,
                "cost_value": 0.0,
                "buy_date": str(row["opened_at"] or "")[:10],
                "highest_price": 0.0,
            },
        )
        item["quantity"] += quantity
        item["cost_value"] += cost * quantity
        item["highest_price"] = max(float(item["highest_price"]), highest)
        opened = str(row["opened_at"] or "")[:10]
        if opened and (not item["buy_date"] or opened < item["buy_date"]):
            item["buy_date"] = opened

    result: list[dict[str, Any]] = []
    for item in aggregated.values():
        quantity = int(item.pop("quantity"))
        cost_value = float(item.pop("cost_value"))
        item["buy_price"] = round(cost_value / quantity, 4) if quantity else 0.0
        result.append(item)
    return result


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


_TRADE_DATES_CACHE: set[str] | None = None


def _load_trade_dates() -> set[str] | None:
    """获取 A 股交易日历（含节假日休市）。失败返回 None，由调用方回退到仅排除周末。

    结果按进程缓存，避免每轮扫描都联网。
    """
    global _TRADE_DATES_CACHE
    if _TRADE_DATES_CACHE is not None:
        return _TRADE_DATES_CACHE
    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        dates = {str(d) for d in df["trade_date"].astype(str)}
        _TRADE_DATES_CACHE = dates
        return dates
    except Exception as exc:
        print(f"trade calendar unavailable, fallback to weekday-only: {exc}", file=sys.stderr)
        return None


def _is_trading_day(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    trade_dates = _load_trade_dates()
    if trade_dates is None:
        return True  # 拿不到日历时保持原行为：只排除周末
    return now.strftime("%Y-%m-%d") in trade_dates


def is_in_trading_session() -> bool:
    now = datetime.now()
    if not _is_trading_day(now):
        return False
    minute_of_day = now.hour * 60 + now.minute
    morning = 9 * 60 + 30 <= minute_of_day <= 11 * 60 + 30
    afternoon = 13 * 60 <= minute_of_day <= 15 * 60
    return morning or afternoon


def should_suppress(key: str, state: dict[str, Any], cooldown_minutes: int) -> bool:
    last_alert_at = parse_time(state.get("alerts", {}).get(key, {}).get("last_alert_at"))
    if not last_alert_at:
        return False
    return datetime.now() - last_alert_at < timedelta(minutes=cooldown_minutes)


def should_suppress_exit(key: str, state: dict[str, Any], signal: dict[str, Any], config: dict[str, Any]) -> bool:
    """离场信号按理由去重，避免同一条件每 30 分钟重复轰炸。"""
    payload = state.get("alerts", {}).get(key, {})
    last_alert_at = parse_time(payload.get("last_alert_at"))
    if not last_alert_at:
        return False
    last_signal = payload.get("last_signal") or {}
    previous_reasons = sorted(str(x) for x in last_signal.get("exit_reasons", []))
    current_reasons = sorted(str(x) for x in signal.get("exit_reasons", []))
    repeat_minutes = int(config.get("exit_repeat_minutes", 240))
    if previous_reasons == current_reasons:
        return datetime.now() - last_alert_at < timedelta(minutes=repeat_minutes)
    return datetime.now() - last_alert_at < timedelta(minutes=int(config.get("cooldown_minutes", 30)))


def is_meaningful_reentry(key: str, state: dict[str, Any], signal: dict[str, Any], config: dict[str, Any]) -> bool:
    last_signal = state.get("alerts", {}).get(key, {}).get("last_signal") or {}
    if not last_signal:
        return True

    if signal.get("signal_type") != "entry":
        return True

    reentry_cfg = config.get("reentry", {})
    allow_same_day_second_push = bool(reentry_cfg.get("allow_same_day_second_push", False))
    force_score_delta = float(reentry_cfg.get("force_second_push_score_delta", 12.0))
    force_price_breakout_pct = float(reentry_cfg.get("force_second_push_price_breakout_pct", 3.5))
    min_score_delta = float(reentry_cfg.get("min_score_delta", 8.0))
    min_price_breakout_pct = float(reentry_cfg.get("min_price_breakout_pct", 2.0))
    min_minutes_between_same_symbol = int(reentry_cfg.get("min_minutes_between_same_symbol", 120))

    last_alert_at = parse_time(state.get("alerts", {}).get(key, {}).get("last_alert_at"))
    now = datetime.now()

    curr_score = float(signal.get("score", 0) or 0)
    prev_score = float(last_signal.get("score", 0) or 0)
    curr_price = float(signal.get("price", 0) or 0)
    prev_price = float(last_signal.get("price", 0) or 0)
    score_delta = curr_score - prev_score
    breakout_pct = (curr_price - prev_price) / prev_price * 100 if prev_price > 0 else 0.0
    strategy_label = str(signal.get("strategy_label") or signal.get("strategy") or "")

    same_day = bool(last_alert_at and now.date() == last_alert_at.date())
    minutes_since_last = (now - last_alert_at).total_seconds() / 60 if last_alert_at else None

    relaxed_labels = {"回踩低吸", "回踩反包"}
    is_relaxed_setup = strategy_label in relaxed_labels

    same_day_score_delta = force_score_delta
    same_day_breakout_pct = force_price_breakout_pct
    cross_day_score_delta = min_score_delta
    cross_day_breakout_pct = min_price_breakout_pct

    if is_relaxed_setup:
        same_day_score_delta = min(same_day_score_delta, 4.0)
        same_day_breakout_pct = min(same_day_breakout_pct, 1.2)
        cross_day_score_delta = min(cross_day_score_delta, 3.0)
        cross_day_breakout_pct = min(cross_day_breakout_pct, 1.0)

    if minutes_since_last is not None:
        if minutes_since_last >= 180:
            same_day_score_delta = min(same_day_score_delta, 3.0)
            same_day_breakout_pct = min(same_day_breakout_pct, 1.0)
        elif minutes_since_last >= 120:
            same_day_score_delta = min(same_day_score_delta, 4.0)
            same_day_breakout_pct = min(same_day_breakout_pct, 1.2)
        elif minutes_since_last >= 60:
            same_day_score_delta = min(same_day_score_delta, 6.0)
            same_day_breakout_pct = min(same_day_breakout_pct, 1.5)

    if same_day:
        if not allow_same_day_second_push:
            return False
        if now - last_alert_at < timedelta(minutes=min_minutes_between_same_symbol):
            return False
        if score_delta >= same_day_score_delta:
            signal["reentry_reason"] = f"同票二次机会：评分较上次提升{score_delta:.1f}"
            signal["reentry_type"] = "second_chance"
            return True
        if breakout_pct >= same_day_breakout_pct:
            signal["reentry_reason"] = f"同票二次机会：价格较上次突破{breakout_pct:.2f}%"
            signal["reentry_type"] = "second_chance"
            return True
        return False

    if score_delta >= cross_day_score_delta:
        signal["reentry_reason"] = f"评分较上次提升{score_delta:.1f}"
        signal["reentry_type"] = "reentry"
        return True

    if breakout_pct >= cross_day_breakout_pct:
        signal["reentry_reason"] = f"价格较上次突破{breakout_pct:.2f}%"
        signal["reentry_type"] = "reentry"
        return True

    return False


def mark_alerted(key: str, state: dict[str, Any], signal: dict[str, Any]) -> None:
    state.setdefault("alerts", {})[key] = {
        "last_alert_at": now_iso(),
        "last_signal": signal,
    }


def _bump_counter(bucket: dict[str, Any], key: str, field: str = "count", delta: int = 1) -> None:
    item = bucket.setdefault(key, {})
    item[field] = int(item.get(field, 0)) + delta


def record_observation(state: dict[str, Any], signal: dict[str, Any], *, emitted: bool) -> None:
    obs = state.setdefault("observation", {})
    obs["last_seen_at"] = now_iso()
    obs["total_seen"] = int(obs.get("total_seen", 0)) + 1
    if emitted:
        obs["total_emitted"] = int(obs.get("total_emitted", 0)) + 1

    strategy_label = signal.get("strategy_label") or signal.get("strategy") or "unknown"
    direction = signal.get("direction") or "unknown"
    symbol = signal.get("symbol") or "unknown"
    name = signal.get("name") or symbol

    strategy_stats = obs.setdefault("strategy_stats", {})
    strategy_item = strategy_stats.setdefault(strategy_label, {"seen": 0, "emitted": 0})
    strategy_item["seen"] += 1
    if emitted:
        strategy_item["emitted"] += 1

    direction_stats = obs.setdefault("direction_stats", {})
    direction_item = direction_stats.setdefault(direction, {"seen": 0, "emitted": 0})
    direction_item["seen"] += 1
    if emitted:
        direction_item["emitted"] += 1

    symbol_stats = obs.setdefault("symbol_stats", {})
    symbol_item = symbol_stats.setdefault(symbol, {
        "name": name,
        "direction": direction,
        "seen": 0,
        "emitted": 0,
        "strategies": {},
        "last_seen_at": "",
        "last_emitted_at": "",
    })
    symbol_item["name"] = name
    symbol_item["direction"] = direction
    symbol_item["seen"] += 1
    symbol_item["last_seen_at"] = obs["last_seen_at"]
    _bump_counter(symbol_item.setdefault("strategies", {}), strategy_label)
    if emitted:
        symbol_item["emitted"] += 1
        symbol_item["last_emitted_at"] = obs["last_seen_at"]

    obs["last_signal_sample"] = {
        "time": signal.get("time") or obs["last_seen_at"],
        "direction": direction,
        "symbol": symbol,
        "name": name,
        "strategy_label": strategy_label,
        "score": signal.get("score"),
        "emitted": emitted,
    }


def normalize_pool(config: dict[str, Any]) -> list[dict[str, Any]]:
    dynamic_cfg = config.get("dynamic_pool", {}) if isinstance(config, dict) else {}
    dynamic_path = Path(dynamic_cfg.get("file", "/root/.hermes/scripts/a_share_dynamic_pool.json"))
    if dynamic_cfg.get("enabled", False) and dynamic_cfg.get("replace_static_pool", True):
        try:
            payload = json.loads(dynamic_path.read_text(encoding="utf-8"))
            generated = parse_time(payload.get("generated_at"))
            max_age = int(dynamic_cfg.get("max_age_minutes", 360))
            active = payload.get("active_sectors") or []
            fresh = generated is not None and (datetime.now() - generated).total_seconds() <= max_age * 60
            if fresh and active:
                return [
                    {
                        "direction": str(item.get("direction") or item.get("label") or "未知板块"),
                        "members": [
                            {
                                "symbol": str(member.get("symbol")),
                                "name": member.get("name") or member.get("symbol"),
                                "type": member.get("type", "stock"),
                            }
                            for member in item.get("members", [])
                            if member.get("symbol")
                        ],
                    }
                    for item in active
                    if item.get("members")
                ]
        except Exception as exc:
            print(f"dynamic pool load failed, fallback static: {exc}", file=sys.stderr)
    merged_pool = []
    source_items = []
    if config.get("direction_pool"):
        source_items.extend(config["direction_pool"])
    if config.get("message_focus_pool"):
        source_items.extend(config["message_focus_pool"])

    if source_items:
        normalized: list[dict[str, Any]] = []
        for item in source_items:
            members = item.get("members")
            if members:
                normalized_members = []
                for member in members:
                    normalized_members.append(
                        {
                            "symbol": member["symbol"],
                            "name": member.get("name") or member["symbol"],
                            "type": member.get("type", "etf"),
                        }
                    )
                normalized.append(
                    {
                        "direction": item["direction"],
                        "members": normalized_members,
                    }
                )
            else:
                normalized.append(
                    {
                        "direction": item.get("direction") or item.get("name") or item["symbol"],
                        "members": [
                            {
                                "symbol": item["symbol"],
                                "name": item.get("name") or item["symbol"],
                                "type": item.get("type", "etf"),
                            }
                        ],
                    }
                )

        merged: dict[str, dict[str, Any]] = {}
        for item in normalized:
            bucket = merged.setdefault(item["direction"], {"direction": item["direction"], "members": []})
            seen = {member["symbol"] for member in bucket["members"]}
            for member in item["members"]:
                if member["symbol"] not in seen:
                    bucket["members"].append(member)
                    seen.add(member["symbol"])
        merged_pool = list(merged.values())
        return merged_pool
    if config.get("watchlist"):
        return [
            {
                "direction": item.get("name") or item["symbol"],
                "members": [
                    {
                        "symbol": item["symbol"],
                        "name": item.get("name") or item["symbol"],
                        "type": item.get("type", "etf"),
                    }
                ],
            }
            for item in config["watchlist"]
        ]
    return []


def normalize_holding_pool(config: dict[str, Any]) -> list[dict[str, Any]]:
    holdings = config.get("holding_pool", []) or config.get("positions", [])
    normalized: list[dict[str, Any]] = []
    for item in holdings:
        if not item.get("symbol"):
            continue
        normalized.append(
            {
                "symbol": item["symbol"],
                "name": item.get("name") or item["symbol"],
                "buy_price": item.get("buy_price", 0),
                "buy_date": item.get("buy_date", ""),
                "type": item.get("type", "stock"),
                "highest_price": item.get("highest_price", item.get("buy_price", 0)),
            }
        )
    return normalized


def build_direction_alert(
    candidates: list[dict[str, Any]],
    strategy: dict[str, Any],
    regime: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    top_n = int(strategy.get("top_n", 3))
    min_score = float(strategy.get("min_score", 60))
    suppress_overheated = bool(strategy.get("suppress_overheated_entry", True))
    # #3 大盘择时：弱市抬高入场门槛、强制启用过热熔断，减少逆势追单。
    if regime:
        min_score += float(regime.get("min_score_bonus", 0) or 0)
        if regime.get("force_suppress_overheated"):
            suppress_overheated = True
    ranked = [item for item in candidates if _sortable_score(item.get("best_member", {}).get("score")) >= min_score]
    ranked.sort(key=lambda item: _sortable_score(item.get("best_member", {}).get("score")), reverse=True)
    ranked = ranked[:top_n]
    if not ranked:
        return None

    actionable = [item for item in ranked if item.get("best_member", {}).get("entry_triggered")]
    if not actionable:
        return None

    # 过热熔断：右侧突破型若已过热（RSI偏高/离均线过远/贴日高），追进去最容易隔天回落。
    # 回踩低吸/回踩反包本身就是等回落后的买点，不受此限。涨停锁死是最强走势，放行。
    def _is_chase_overheated(item: dict[str, Any]) -> bool:
        best = item.get("best_member", {})
        if not best.get("overheated"):
            return False
        if best.get("limit_up"):
            return False
        return best.get("strategy") == "direction_rotation"

    hot_items = [item for item in actionable if _is_chase_overheated(item)]
    healthy = [item for item in actionable if not _is_chase_overheated(item)]

    if suppress_overheated and healthy:
        # 有健康标的时，优先推健康的，过热的降级到备注里。
        actionable = healthy
    elif suppress_overheated and not healthy:
        # 全部过热：不发入场提醒，改发一条「过热观望」提醒，避免高位追单。
        top = hot_items[0]
        best = top["best_member"]
        hot_summary = "；".join(
            f"{it['direction']}/{it['best_member']['name']}({it['best_member']['symbol']}) "
            f"评分{_sortable_score(it['best_member'].get('score')):.1f}"
            f"（{'、'.join(it['best_member'].get('overheat_reasons', []))}）"
            for it in hot_items
        )
        return {
            "time": now_iso(),
            "signal_type": "watch",
            "direction": top["direction"],
            "symbol": best["symbol"],
            "name": best["name"],
            "type": best.get("type", "unknown"),
            "strategy": best.get("strategy") or "direction_rotation",
            "strategy_label": "过热观望",
            "score": best.get("score"),
            "overheat_reasons": best.get("overheat_reasons", []),
            "message": f"方向偏强但已过热，建议等回踩不宜追高：{hot_summary}",
            "best_member": best,
            "etf_anchor": top.get("etf_anchor"),
            "backup_members": top.get("backup_members", []),
            "directions": hot_items,
        }

    top = actionable[0]
    best = top["best_member"]
    best_type = best.get("type", "unknown")
    etf_anchor = top.get("etf_anchor")
    backup_members = top.get("backup_members", [])
    summary = "；".join(
        f"{item['direction']} -> {item['best_member']['name']}({item['best_member']['symbol']}) 评分{_sortable_score(item['best_member'].get('score')):.1f}"
        for item in actionable
    )
    reasons = "、".join(best.get("entry_reasons", [])) or "方向强度与入场条件同时满足"
    anchor_text = f"；方向锚：{etf_anchor['name']}({etf_anchor['symbol']})" if etf_anchor else ""
    backup_text = (
        "；备选：" + ", ".join(f"{item['name']}({item['symbol']})" for item in backup_members)
        if backup_members
        else ""
    )
    return {
        "time": now_iso(),
        "signal_type": "entry",
        "direction": top["direction"],
        "symbol": best["symbol"],
        "name": best["name"],
        "type": best_type,
        "strategy": best.get("strategy") or "direction_rotation",
        "strategy_label": best.get("strategy_label") or "右侧突破",
        "score": best["score"],
        "entry_reasons": best.get("entry_reasons", []),
        "reentry_reason": best.get("reentry_reason") or top.get("reentry_reason"),
        "reentry_type": best.get("reentry_type") or top.get("reentry_type"),
        "stop_loss": _round_or_none(best.get("price"), strategy.get("stop_loss_pct", -5.0)),
        "take_profit": _round_or_none(best.get("price"), strategy.get("take_profit_pct", 15.0)),
        "message": f"可考虑入场方向：{summary}。优先观察 {top['direction']} / {best['name']}[{best_type}]，理由：{reasons}{anchor_text}{backup_text}",
        "best_member": best,
        "etf_anchor": etf_anchor,
        "backup_members": backup_members,
        "directions": actionable,
    }


def build_exit_alert(signal: dict[str, Any], holding: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": now_iso(),
        "signal_type": "exit",
        "symbol": holding["symbol"],
        "name": holding.get("name") or holding["symbol"],
        "type": holding.get("type", "stock"),
        **signal,
    }


def run_once(config: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    pool = normalize_pool(config)
    # 多账户数据库是唯一持仓真相源；关注股票不能冒充持仓进入离场策略。
    holdings = _load_portfolio_holdings(config)
    strategy = config["strategy"]
    delivery = config.get("delivery", {"mode": "stdout"})
    cooldown_minutes = int(config.get("cooldown_minutes", 30))
    candidate_score_threshold = float(strategy.get("candidate_score_threshold", 45))
    all_symbols = [member["symbol"] for item in pool for member in item["members"]]
    all_symbols.extend(item["symbol"] for item in holdings)
    unique_symbols = sorted(set(all_symbols))
    snapshots = fetch_snapshot(unique_symbols)
    pool = _validate_pool_board_mapping(pool)
    history_bars = int(strategy.get("history_bars", 60))

    # Pre-fetch all histories concurrently — single batch to minimize wall-clock time
    histories: dict = {}
    if unique_symbols:
        histories = fetch_histories(unique_symbols, history_bars, max_workers=4)
        if None in histories:
            del histories[None]

    # #3 大盘择时：环境差时全局降级/收紧入场；失败回退中性（不拦截）。
    regime = _assess_regime(config)

    alerts: list[dict[str, Any]] = []
    recommendation_mode = str((config.get("recommendation_engine", {}) or {}).get("mode", "shadow"))
    use_recommendation_engine = recommendation_mode == "live" and load_executable_recommendations is not None and to_monitor_alert is not None

    if use_recommendation_engine:
        for recommendation in load_executable_recommendations(config):
            alert = to_monitor_alert(recommendation)
            alert = _apply_risk_veto(alert, config)
            suppress_key = f"{alert.get('signal_type', 'entry')}::engine::{alert.get('signal_id') or alert['symbol']}"
            emitted = False
            if not should_suppress(suppress_key, state, cooldown_minutes) and is_meaningful_reentry(suppress_key, state, alert, config):
                mark_alerted(suppress_key, state, alert)
                emit_alert(alert, delivery)
                alerts.append(alert)
                emitted = True
                _record_push_to_journal(alert, config)
            record_observation(state, alert, emitted=emitted)

    if strategy.get("name") == "direction_rotation" and not use_recommendation_engine:
        direction_candidates: list[dict[str, Any]] = []
        for item in pool:
            direction = item["direction"]
            members_ranked: list[dict[str, Any]] = []
            for member in item["members"]:
                symbol = member["symbol"]
                snapshot = snapshots.get(symbol)
                if not snapshot:
                    print(f"missing snapshot for {symbol}", file=sys.stderr)
                    continue

                price = float(snapshot.get("price", 0) or 0)
                open_price = float(snapshot.get("open", 0) or 0)
                high = float(snapshot.get("high", 0) or 0)
                low = float(snapshot.get("low", 0) or 0)
                change_pct = float(snapshot.get("change_pct", 0) or 0)
                amount = float(snapshot.get("amount", 0) or 0)
                high_low_range = high - low
                close_to_high_ratio = (price - low) / high_low_range if high_low_range > 0 else 0.0
                body_to_range_ratio = abs(price - open_price) / high_low_range if high_low_range > 0 else 0.0
                candidate_score = 0.0
                candidate_score += min(max(change_pct, -1.0) + 1.0, 3.0) * 10
                candidate_score += min(close_to_high_ratio * 20, 20)
                # amount/price 是当日累计成交量，不是量比；不能用它给午后股票固定加满分。
                # 正确的同期量比由分钟管线提供，正式链缺失时不额外加分。
                candidate_score += min(max(float(snapshot.get("same_time_volume_ratio", 0) or 0), 0.0), 2.0) * 10
                if body_to_range_ratio >= 0.2:
                    candidate_score += 10

                if candidate_score < candidate_score_threshold:
                    continue
                history = histories.get(symbol)
                if history is None:
                    print(f"history fetch failed or empty for {symbol}", file=sys.stderr)
                    continue
                signal = evaluate_signal(history, snapshot, strategy)
                if not signal:
                    continue
                members_ranked.append(
                    {
                        "direction": direction,
                        "symbol": symbol,
                        "name": member.get("name") or snapshot.get("name") or symbol,
                        "type": member.get("type", "etf"),
                        **signal,
                    }
                )

            # 剔除分数不可用（NaN/None）的成员，避免它们污染排序或被当作最佳标的。
            members_ranked = [m for m in members_ranked if _sortable_score(m.get("score")) != float("-inf")]
            if not members_ranked:
                continue

            members_ranked.sort(key=lambda item: _sortable_score(item.get("score")), reverse=True)
            etf_anchor = next((member for member in members_ranked if member.get("type") == "etf"), None)
            direction_candidates.append(
                {
                    "direction": direction,
                    "best_member": members_ranked[0],
                    "etf_anchor": etf_anchor,
                    "backup_members": members_ranked[1:4],
                }
            )

        alert = build_direction_alert(direction_candidates, strategy, regime=regime)
        if alert:
            # #2 利空否决：仅对「入场」类提醒做校验，命中确定性利空则拦截或降级。
            #    watch（过热观望）本身就是提示别追，不必再否决。
            if alert.get("signal_type") == "entry":
                alert = _apply_risk_veto(alert, config)
            suppress_key = f"{alert.get('signal_type', 'entry')}::{alert['direction']}::{alert['symbol']}"
            emitted = False
            if not should_suppress(suppress_key, state, cooldown_minutes) and is_meaningful_reentry(suppress_key, state, alert, config):
                mark_alerted(suppress_key, state, alert)
                emit_alert(alert, delivery)
                alerts.append(alert)
                emitted = True
                # #4 自评估：登记本次推送，供事后 T+1/T+3 复盘命中率。
                _record_push_to_journal(alert, config)
            record_observation(state, alert, emitted=emitted)

    for holding in holdings:
        symbol = holding["symbol"]
        snapshot = snapshots.get(symbol)
        if not snapshot:
            print(f"missing snapshot for holding {symbol}", file=sys.stderr)
            continue
        try:
            history = histories.get(symbol)
            if history is None:
                raise RuntimeError("history fetch failed")
        except Exception as exc:
            print(f"history fetch failed for holding {symbol}: {exc}", file=sys.stderr)
            continue
        exit_signal = evaluate_exit_signal(history, snapshot, strategy, holding)
        if not exit_signal:
            continue

        latest_high = max(float(holding.get("highest_price", holding.get("buy_price", 0))), float(snapshot.get("high", 0)), float(snapshot.get("price", 0)))
        holding["highest_price"] = round(latest_high, 3)
        alert = build_exit_alert(exit_signal, holding)
        suppress_key = f"exit::{symbol}"
        emitted = False
        if should_suppress_exit(suppress_key, state, alert, config):
            record_observation(state, alert, emitted=False)
            continue
        mark_alerted(suppress_key, state, alert)
        emit_alert(alert, delivery)
        alerts.append(alert)
        emitted = True
        record_observation(state, alert, emitted=emitted)

    return alerts


def main() -> int:
    parser = argparse.ArgumentParser(description="A-share alert template")
    parser.add_argument("--config", default="a_share_alert_template/config.json")
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument("--ignore-session", action="store_true", help="run even outside A-share trading hours")
    parser.add_argument("--cron-quiet", action="store_true", help="stay silent when there is no signal")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_json(config_path)
    state_path = Path(config.get("state_file", "a_share_alert_template/state/runtime_state.json"))
    state = load_json(state_path) if state_path.exists() else {"alerts": {}}
    interval = int(config.get("min_interval_seconds", 60))
    had_error = False

    while True:
        if args.ignore_session or is_in_trading_session():
            try:
                alerts = run_once(config, state)
                save_json(state_path, state)
                if args.once and args.cron_quiet and not alerts:
                    return 0
            except Exception as exc:
                had_error = True
                print(f"scan failed: {exc}", file=sys.stderr)
        elif args.once and not args.cron_quiet:
            print("outside A-share trading session", file=sys.stderr)

        if args.once:
            break
        time.sleep(interval)

    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
