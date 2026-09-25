#!/usr/bin/env python3
"""A 股监控只读看板。"""
from __future__ import annotations

import base64
import json
import math
import os
import secrets
import hashlib
import hmac
import sqlite3
import subprocess
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from data_source import fetch_history, fetch_snapshot
from advice_card import build_signals
from portfolio_store import PortfolioStore
from review_metrics import build_review


APP_DIR = Path(__file__).resolve().parent
STATE_PATH = Path("/root/.hermes/scripts/a_share_alert_runtime_state.json")
JOURNAL_PATH = Path("/root/.hermes/scripts/a_share_signal_journal.json")
SHADOW_PATH = Path("/root/.hermes/scripts/a_share_shadow_snapshot.json")
DYNAMIC_PATH = Path("/root/.hermes/scripts/a_share_dynamic_pool.json")
SENTIMENT_PATH = Path("/root/.hermes/scripts/a_share_market_sentiment.json")
RECOMMENDATION_PATH = Path("/root/.hermes/scripts/a_share_recommendation_snapshot.json")
FAILOVER_DRILL_PATH = Path("/root/.hermes/scripts/a_share_failover_drill.json")
GLOBAL_MARKET_PATH = Path("/root/.hermes/scripts/a_share_global_market_factor.json")
PIPELINE_MANIFEST_PATH = Path("/root/.hermes/scripts/a_share_pipeline_manifest.json")
SNAPSHOT_DB = Path("/root/.hermes/scripts/a_share_market_snapshots.db")
PORTFOLIO_DB = Path(os.environ.get("PORTFOLIO_DB", "/var/lib/a-share-dashboard/portfolios.db"))
CRON_BIN = "/usr/local/lib/hermes-agent/venv/bin/hermes"

app = FastAPI(title="A股监控看板", docs_url=None, redoc_url=None, openapi_url=None)
security = HTTPBasic()
CSRF_SECRET = os.environ.get("DASHBOARD_CSRF_SECRET") or secrets.token_hex(32)
CSRF_TTL_SECONDS = int(os.environ.get("DASHBOARD_CSRF_TTL_SECONDS", "3600"))
AUTH_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("DASHBOARD_AUTH_WINDOW_SECONDS", "300"))
AUTH_RATE_LIMIT_MAX_FAILURES = int(os.environ.get("DASHBOARD_AUTH_MAX_FAILURES", "8"))
SECURITY_AUDIT_PATH = Path(os.environ.get("DASHBOARD_SECURITY_AUDIT", "/var/lib/a-share-dashboard/security_audit.jsonl"))
_AUTH_FAILURES: dict[str, list[float]] = defaultdict(list)


def _audit_security(event: str, request: Request | None = None, **detail: Any) -> None:
    payload = {
        "event": event,
        "at": datetime.now().isoformat(timespec="seconds"),
        "client": request.client.host if request and request.client else None,
        "host": request.headers.get("host") if request else None,
        "path": str(request.url.path) if request else None,
        "detail": detail,
    }
    try:
        SECURITY_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SECURITY_AUDIT_PATH.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _trusted_hosts() -> set[str]:
    value = os.environ.get("DASHBOARD_TRUSTED_HOSTS", "")
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _host_name(value: str | None) -> str:
    value = (value or "").strip().lower()
    if not value:
        return ""
    if value.startswith("[") and "]" in value:
        return value[1:].split("]", 1)[0]
    return value.rsplit(":", 1)[0] if ":" in value else value


def _origin_host(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _host_name(urlsplit(value).netloc)
    except ValueError:
        return ""


def _is_trusted_host(host: str | None, trusted: set[str] | None = None) -> bool:
    trusted = _trusted_hosts() if trusted is None else {item.lower() for item in trusted}
    return not trusted or _host_name(host) in trusted


@app.middleware("http")
async def trusted_host_boundary(request: Request, call_next):
    if not _is_trusted_host(request.headers.get("host")):
        _audit_security("host_rejected", request)
        return JSONResponse({"detail": "Host不在可信列表"}, status_code=400)
    return await call_next(request)


def _client_key(request: Request | None) -> str:
    return request.client.host if request and request.client else "unknown"


def _too_many_auth_failures(client: str, now: float | None = None) -> bool:
    now = now or time.time()
    window_start = now - AUTH_RATE_LIMIT_WINDOW_SECONDS
    failures = [item for item in _AUTH_FAILURES[client] if item >= window_start]
    _AUTH_FAILURES[client] = failures
    return len(failures) >= AUTH_RATE_LIMIT_MAX_FAILURES


def _record_auth_failure(client: str, now: float | None = None) -> None:
    _AUTH_FAILURES[client].append(now or time.time())


def require_auth(request: Request, credentials: HTTPBasicCredentials = Depends(security)) -> str:
    expected_user = os.environ.get("DASHBOARD_USER", "viewer")
    expected_password = os.environ.get("DASHBOARD_PASSWORD", "")
    client = _client_key(request)
    if _too_many_auth_failures(client):
        _audit_security("auth_rate_limited", request, username=credentials.username)
        raise HTTPException(status_code=429, detail="登录失败次数过多，请稍后再试")
    valid = bool(expected_password) and secrets.compare_digest(credentials.username, expected_user)
    valid = valid and secrets.compare_digest(credentials.password, expected_password)
    if not valid:
        _record_auth_failure(client)
        _audit_security("auth_failed", request, username=credentials.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Basic"},
        )
    _AUTH_FAILURES.pop(client, None)
    return credentials.username


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _csrf_token(username: str, now: float | None = None) -> str:
    now = now or time.time()
    payload = {
        "u": username,
        "exp": int(now + CSRF_TTL_SECONDS),
        "n": secrets.token_urlsafe(10),
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    signature = _b64(hmac.new(CSRF_SECRET.encode(), body.encode(), hashlib.sha256).digest())
    return f"v1.{body}.{signature}"


def _verify_csrf_token(username: str, token: str, now: float | None = None) -> bool:
    try:
        version, body, signature = str(token).split(".", 2)
        if version != "v1":
            return False
        expected = _b64(hmac.new(CSRF_SECRET.encode(), body.encode(), hashlib.sha256).digest())
        if not secrets.compare_digest(signature, expected):
            return False
        payload = json.loads(_unb64(body))
        current = int(now or time.time())
        return payload.get("u") == username and int(payload.get("exp") or 0) >= current
    except Exception:
        return False


def require_write_auth(request: Request, csrf: str = Header(alias="X-CSRF-Token"),
                       credentials: HTTPBasicCredentials = Depends(security)) -> str:
    username = require_auth(request, credentials)
    origin = request.headers.get("origin")
    request_host = _host_name(request.headers.get("host"))
    origin_host = _origin_host(origin)
    if origin and (origin_host != request_host or not _is_trusted_host(origin_host)):
        _audit_security("origin_rejected", request, username=username, origin=origin)
        raise HTTPException(status_code=403, detail="写操作来源校验失败")
    if not _verify_csrf_token(username, csrf):
        _audit_security("csrf_rejected", request, username=username)
        raise HTTPException(status_code=403, detail="CSRF令牌无效")
    return username


_PORTFOLIO_PRICE_CACHE: dict[str, float] = {}
_PORTFOLIO_PRICE_CACHE_AT = 0.0
_PORTFOLIO_PRICE_TTL = float(os.environ.get("DASHBOARD_QUOTE_TTL_SECONDS", "8"))


def _portfolio_symbols() -> list[str]:
    """返回所有账户持仓和自选代码，避免只依赖分时状态动态池。"""
    if not PORTFOLIO_DB.exists():
        return []
    try:
        connection = sqlite3.connect(f"file:{PORTFOLIO_DB}?mode=ro", uri=True, timeout=2)
        rows = connection.execute(
            "SELECT symbol FROM positions WHERE status='active' "
            "UNION SELECT symbol FROM watchlist_items WHERE status!='deleted'"
        ).fetchall()
        connection.close()
        return sorted({str(row[0]).strip() for row in rows if row and row[0]})
    except Exception:
        return []


def _portfolio_prices() -> dict[str, float]:
    """实时行情优先，分时状态库仅作为短暂故障时的回退。"""
    global _PORTFOLIO_PRICE_CACHE_AT, _PORTFOLIO_PRICE_CACHE
    now = time.monotonic()
    if _PORTFOLIO_PRICE_CACHE and now - _PORTFOLIO_PRICE_CACHE_AT < _PORTFOLIO_PRICE_TTL:
        return dict(_PORTFOLIO_PRICE_CACHE)
    symbols = _portfolio_symbols()
    live: dict[str, float] = {}
    if symbols:
        try:
            snapshots = fetch_snapshot(symbols)
            live = {
                str(symbol): float(row.get("price"))
                for symbol, row in snapshots.items()
                if row and row.get("price") and float(row.get("price")) > 0
            }
        except Exception:
            live = {}
    fallback: dict[str, float] = {}
    if SNAPSHOT_DB.exists():
        try:
            connection = sqlite3.connect(f"file:{SNAPSHOT_DB}?mode=ro&immutable=1", uri=True, timeout=2)
            rows = connection.execute("SELECT symbol,json_extract(metrics_json,'$.current') FROM intraday_states").fetchall()
            connection.close()
            fallback = {str(symbol): float(price) for symbol, price in rows if price and float(price) > 0}
        except Exception:
            fallback = {}
    prices = {**fallback, **live}
    if prices:
        _PORTFOLIO_PRICE_CACHE = prices
        _PORTFOLIO_PRICE_CACHE_AT = now
    return dict(prices or _PORTFOLIO_PRICE_CACHE)


def _combined_portfolio_holdings() -> list[dict[str, Any]]:
    """统一展示多账户真实持仓与近期关注，不再读取旧JSON自选。"""
    merged: dict[str, dict[str, Any]] = {}
    try:
        prices = _portfolio_prices()
        with PortfolioStore(PORTFOLIO_DB) as store:
            for account in store.list_accounts():
                portfolio = store.portfolio(account["account_id"], prices)
                for position in portfolio["positions"]:
                    symbol = str(position["symbol"])
                    row = merged.setdefault(symbol, {
                        "symbol": symbol, "name": position.get("name") or symbol,
                        "position_accounts": [], "watch_accounts": [], "quantity": 0,
                        "available_quantity": 0, "cost_value": 0.0, "market_value": 0.0,
                        "current_price": position.get("current_price"), "stop_price": position.get("stop_price"),
                        "target_price": position.get("target_price"),
                    })
                    row["position_accounts"].append(account["name"])
                    row["quantity"] += int(position.get("quantity") or 0)
                    row["available_quantity"] += int(position.get("available_quantity") or 0)
                    row["cost_value"] += float(position.get("cost_value") or 0)
                    row["market_value"] += float(position.get("market_value") or 0)
                for item in portfolio["watchlist"]:
                    symbol = str(item["symbol"])
                    row = merged.setdefault(symbol, {
                        "symbol": symbol, "name": item.get("name") or symbol,
                        "position_accounts": [], "watch_accounts": [], "quantity": 0,
                        "available_quantity": 0, "cost_value": 0.0, "market_value": 0.0,
                        "current_price": prices.get(symbol), "stop_price": None, "target_price": None,
                    })
                    row["watch_accounts"].append(account["name"])
        result = []
        for row in merged.values():
            quantity = int(row["quantity"])
            row["average_cost"] = round(float(row["cost_value"]) / quantity, 4) if quantity else None
            row["pnl"] = round(float(row["market_value"]) - float(row["cost_value"]), 2) if quantity else None
            row["status"] = "position_watch" if row["position_accounts"] and row["watch_accounts"] else "position" if row["position_accounts"] else "watch"
            row["accounts"] = list(dict.fromkeys([*row.pop("position_accounts"), *row.pop("watch_accounts")]))
            result.append(row)
        return sorted(result, key=lambda row: (row["status"].startswith("position"), row["market_value"]), reverse=True)
    except Exception:
        return []


def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return None


def _runtime_health() -> dict[str, Any]:
    stuck_count = None
    cron_ok = None
    try:
        output = subprocess.run(
            ["pgrep", "-af", "a_share_alert_template/monitor.py"],
            capture_output=True, text=True, timeout=2, check=False,
        ).stdout
        stuck_count = sum(1 for line in output.splitlines() if "--once" in line)
    except Exception:
        pass
    try:
        result = subprocess.run(
            [CRON_BIN, "cron", "status"], capture_output=True, text=True,
            timeout=4, check=False,
        )
        cron_ok = result.returncode == 0 and "Gateway is running" in result.stdout
    except Exception:
        pass
    return {"cron_ok": cron_ok, "monitor_processes": stuck_count}


def _intraday_payload() -> dict[str, Any]:
    if not SNAPSHOT_DB.exists():
        return {"states": [], "transitions": [], "sector_history": [], "source_health": []}
    try:
        # systemd 将 /root/.hermes/scripts 只读挂载；immutable 避免 SQLite 尝试创建 -shm。
        # 分时写入任务每轮关闭连接时会自动检查点，页面只读取完整快照。
        connection = sqlite3.connect(f"file:{SNAPSHOT_DB}?mode=ro&immutable=1", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        states = [dict(row) for row in connection.execute("""
          SELECT symbol,name,sector,state,state_label,entered_at,bars_in_state,reason,bar_time,
                 json_extract(metrics_json,'$.current') price,
                 json_extract(metrics_json,'$.vwap_deviation_pct') vwap_deviation_pct,
                 json_extract(metrics_json,'$.volume_ratio_5m') volume_ratio_5m,
                 json_extract(metrics_json,'$.return_5m_pct') return_5m_pct
          FROM intraday_states ORDER BY CASE state
            WHEN 'second_ignition' THEN 1 WHEN 'confirmation' THEN 2 WHEN 'ignition' THEN 3
            WHEN 'pullback' THEN 4 WHEN 'setup' THEN 5 WHEN 'overheated' THEN 6
            WHEN 'failed' THEN 7 ELSE 8 END, symbol
        """)]
        latest_symbols = {
            row[0] for row in connection.execute(
                "SELECT DISTINCT symbol FROM stock_snapshots WHERE captured_at=(SELECT MAX(captured_at) FROM stock_snapshots)"
            )
        }
        if latest_symbols:
            states = [row for row in states if row.get("symbol") in latest_symbols]
        transitions = [dict(row) for row in connection.execute("""
          SELECT symbol,bar_time,from_state,to_state,reason FROM state_transitions
          ORDER BY bar_time DESC LIMIT 30
        """)]
        sector_history = [dict(row) for row in connection.execute("""
          SELECT captured_at,sector,score,breadth_pct,change_pct FROM sector_snapshots
          WHERE date(captured_at) >= date('now','localtime','-2 day')
          ORDER BY captured_at,sector LIMIT 1500
        """)]
        source_health = [dict(row) for row in connection.execute("""
          SELECT provider, MAX(checked_at) checked_at, COUNT(*) checks,
                 SUM(ok) ok_count, SUM(stale) stale_count,
                 ROUND(AVG(latency_ms),0) avg_latency_ms, MAX(fallback_level) fallback_level
          FROM source_health WHERE date(checked_at) >= date('now','localtime','-1 day') GROUP BY provider
        """)]
        try:
            recommendation_history = [dict(row) for row in connection.execute("""
              SELECT generated_at,symbol,name,sector,status,recommendation_type,state,role,
                     recommendation_score,entry_low,entry_high,invalid_price,target_price,risk_reward,expires_at
              FROM recommendation_snapshots ORDER BY generated_at DESC LIMIT 100
            """)]
        except sqlite3.OperationalError:
            recommendation_history = []
        try:
            recommendation_lifecycle = [dict(row) for row in connection.execute("""
              SELECT signal_id,symbol,name,sector,recommendation_type,state,role,status,
                     first_seen_at,last_seen_at,entry_low,entry_high,invalid_price,target_price,
                     expires_at,entry_reached_at,closed_at,mfe_pct,mae_pct,close_reason
              FROM recommendation_lifecycle ORDER BY first_seen_at DESC LIMIT 100
            """)]
            lifecycle_summary = {
                row["status"]: row["count"] for row in connection.execute(
                    "SELECT status,COUNT(*) count FROM recommendation_lifecycle GROUP BY status"
                )
            }
        except sqlite3.OperationalError:
            recommendation_lifecycle = []
            lifecycle_summary = {}
        try:
            technical_diagnoses = [dict(row) for row in connection.execute("""
              SELECT generated_at,symbol,name,sector,technical_score,trend,support_price,
                     resistance_price,data_quality,support_evidence_json,
                     opposing_evidence_json,missing_data_json,diagnosis_json
              FROM technical_diagnosis_snapshots
              WHERE generated_at=(SELECT MAX(generated_at) FROM technical_diagnosis_snapshots)
              ORDER BY technical_score DESC,symbol
            """)]
            for row in technical_diagnoses:
                row["support_evidence"] = json.loads(row.pop("support_evidence_json"))
                row["opposing_evidence"] = json.loads(row.pop("opposing_evidence_json"))
                row["missing_data"] = json.loads(row.pop("missing_data_json"))
                row["diagnosis"] = json.loads(row.pop("diagnosis_json"))
        except (sqlite3.OperationalError, json.JSONDecodeError):
            technical_diagnoses = []
        try:
            fundamental_diagnoses = [dict(row) for row in connection.execute("""
              SELECT generated_at,symbol,name,sector,fundamental_score,risk_level,report_date,
                     data_quality,support_evidence_json,opposing_evidence_json,event_risks_json,
                     missing_data_json,fundamental_json
              FROM fundamental_snapshots
              WHERE generated_at=(SELECT MAX(generated_at) FROM fundamental_snapshots)
              ORDER BY CASE risk_level WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                       fundamental_score,symbol
            """)]
            for row in fundamental_diagnoses:
                row["support_evidence"] = json.loads(row.pop("support_evidence_json"))
                row["opposing_evidence"] = json.loads(row.pop("opposing_evidence_json"))
                row["event_risks"] = json.loads(row.pop("event_risks_json"))
                row["missing_data"] = json.loads(row.pop("missing_data_json"))
                row["fundamental"] = json.loads(row.pop("fundamental_json"))
        except (sqlite3.OperationalError, json.JSONDecodeError):
            fundamental_diagnoses = []
        try:
            board_strength = [dict(row) for row in connection.execute("""
              SELECT generated_at,dimension,board_name name,board_code code,score,breadth_pct,
                     median_change_pct,main_net_today,main_net_5d,persistence,member_count,members_json
              FROM board_strength_snapshots
              WHERE generated_at=(SELECT MAX(generated_at) FROM board_strength_snapshots)
              ORDER BY CASE dimension WHEN 'industry' THEN 1 ELSE 2 END,score DESC
              LIMIT 80
            """)]
            for row in board_strength:
                row["members"] = json.loads(row.pop("members_json"))
        except (sqlite3.OperationalError, json.JSONDecodeError):
            board_strength = []
        try:
            intraday_replays = [dict(row) for row in connection.execute("""
              SELECT generated_at,trade_date,symbol,quality,bars,expected_bars,coverage_pct,
                     missing_count,invalid_count,fallback_consistent,idempotent,final_state,
                     transition_count,provider_counts_json,missing_times_json
              FROM intraday_replay_runs
              WHERE generated_at=(SELECT MAX(generated_at) FROM intraday_replay_runs)
              ORDER BY CASE quality WHEN 'invalid' THEN 1 WHEN 'insufficient' THEN 2 WHEN 'partial' THEN 3 ELSE 4 END,
                       coverage_pct,symbol LIMIT 200
            """)]
            for row in intraday_replays:
                row["provider_counts"] = json.loads(row.pop("provider_counts_json"))
                row["missing_times"] = json.loads(row.pop("missing_times_json"))
            replay_summary = {
                row["quality"]: row["count"] for row in connection.execute("""
                  SELECT quality,COUNT(*) count FROM intraday_replay_runs
                  WHERE generated_at=(SELECT MAX(generated_at) FROM intraday_replay_runs)
                  GROUP BY quality
                """)
            }
        except (sqlite3.OperationalError, json.JSONDecodeError):
            intraday_replays, replay_summary = [], {}
        connection.close()
        return {
            "states": states, "transitions": transitions, "sector_history": sector_history,
            "source_health": source_health, "recommendation_history": recommendation_history,
            "recommendation_lifecycle": recommendation_lifecycle, "lifecycle_summary": lifecycle_summary,
            "technical_diagnoses": technical_diagnoses,
            "fundamental_diagnoses": fundamental_diagnoses,
            "board_strength": board_strength,
            "intraday_replays": intraday_replays, "replay_summary": replay_summary,
        }
    except Exception as exc:
        return {"states": [], "transitions": [], "sector_history": [], "source_health": [], "error": str(exc)}


def build_payload() -> dict[str, Any]:
    state = _load(STATE_PATH, {})
    journal = _load(JOURNAL_PATH, {"records": []})
    shadow = _load(SHADOW_PATH, {})
    dynamic_pool = _load(DYNAMIC_PATH, {})
    sentiment = _load(SENTIMENT_PATH, {})
    recommendations = _load(RECOMMENDATION_PATH, {})
    failover_drill = _load(FAILOVER_DRILL_PATH, {})
    global_market = _load(GLOBAL_MARKET_PATH, {})
    pipeline = _load(PIPELINE_MANIFEST_PATH, {})
    try:
        generated = datetime.fromisoformat(str(recommendations.get("generated_at")))
        recommendations["snapshot_stale"] = generated.date() != datetime.now().date() or (datetime.now() - generated).total_seconds() > 12 * 60
    except Exception:
        recommendations["snapshot_stale"] = True
    observation = state.get("observation", {}) if isinstance(state, dict) else {}
    records = journal.get("records", []) if isinstance(journal, dict) else []
    evaluated = [record for record in records if record.get("evaluated")]
    returns = [float(record["return_pct"]) for record in evaluated if record.get("return_pct") is not None]

    recent_alerts = []
    for key, value in (state.get("alerts", {}) if isinstance(state, dict) else {}).items():
        signal = value.get("last_signal") or {}
        recent_alerts.append({
            "key": key,
            "time": value.get("last_alert_at"),
            "signal_type": signal.get("signal_type"),
            "direction": signal.get("direction"),
            "symbol": signal.get("symbol"),
            "name": signal.get("name"),
            "strategy_label": signal.get("strategy_label") or signal.get("strategy"),
            "score": signal.get("score"),
            "change_pct": signal.get("change_pct"),
        })
    recent_alerts.sort(key=lambda row: row.get("time") or "", reverse=True)

    strategy_stats = []
    for name, values in (observation.get("strategy_stats", {}) or {}).items():
        seen = int(values.get("seen", 0))
        emitted = int(values.get("emitted", 0))
        strategy_stats.append({
            "name": name,
            "seen": seen,
            "emitted": emitted,
            "emit_rate_pct": round(emitted / seen * 100, 1) if seen else 0,
        })
    strategy_stats.sort(key=lambda row: row["seen"], reverse=True)

    intraday_payload = _intraday_payload()
    holdings = _combined_portfolio_holdings()
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "health": {
            "last_scan_at": observation.get("last_seen_at"),
            "state_updated_at": _mtime(STATE_PATH),
            "shadow_updated_at": _mtime(SHADOW_PATH),
            "total_seen": int(observation.get("total_seen", 0)),
            "total_emitted": int(observation.get("total_emitted", 0)),
            "journal_records": len(records),
            "journal_evaluated": len(evaluated),
            "average_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
            **_runtime_health(),
        },
        "shadow": shadow,
        "dynamic_pool": dynamic_pool,
        "market_sentiment": sentiment,
        "recommendations": recommendations,
        "failover_drill": failover_drill,
        "global_market": global_market,
        "pipeline": pipeline,
        "intraday": intraday_payload,
        "strategy_stats": strategy_stats,
        "recent_alerts": recent_alerts[:30],
        "journal": sorted(records, key=lambda row: row.get("pushed_at") or "", reverse=True)[:30],
        "holdings": holdings,
        "review": build_review(SNAPSHOT_DB, portfolio_db=PORTFOLIO_DB),
        "signals": _signals_payload(),
    }


def _signals_payload(symbol: str | None = None, limit: int = 16) -> list[dict[str, Any]]:
    rows = build_signals(
        recommendations=_load(RECOMMENDATION_PATH, {}),
        portfolio_db=PORTFOLIO_DB,
        snapshot_db=SNAPSHOT_DB,
        price_lookup=_portfolio_prices(),
        limit=80 if symbol else limit,
    )
    if symbol:
        rows = [row for row in rows if str(row.get("symbol")) == symbol]
    return rows[:limit]


def _stock_global_factors(detail: dict[str, Any]) -> list[dict[str, Any]]:
    global_market = _load(GLOBAL_MARKET_PATH, {})
    tags = {str(detail.get("sector") or "").strip()}
    for item in ((detail.get("concepts") or {}).get("concept_tags") or []):
        if isinstance(item, dict):
            text = item.get("name") or item.get("concept") or item.get("label") or item.get("board")
        else:
            text = item
        if text:
            tags.add(str(text).strip())
    tags.discard("")
    factors = []
    for row in global_market.get("sector_factors") or []:
        sector = str(row.get("sector") or "").strip()
        if sector and any(tag and (tag in sector or sector in tag) for tag in tags):
            factors.append(row)
    return factors[:8]


def _diagnosis_detail(symbol: str) -> dict[str, Any] | None:
    if not SNAPSHOT_DB.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{SNAPSHOT_DB}?mode=ro&immutable=1", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        row = connection.execute("""
          SELECT generated_at,symbol,name,sector,technical_score,trend,support_price,
                 resistance_price,data_quality,diagnosis_json
          FROM technical_diagnosis_snapshots WHERE symbol=?
          ORDER BY generated_at DESC LIMIT 1
        """, (symbol,)).fetchone()
        fundamental = connection.execute("""
          SELECT generated_at,symbol,name,sector,fundamental_score,risk_level,report_date,
                 data_quality,fundamental_json
          FROM fundamental_snapshots WHERE symbol=?
          ORDER BY generated_at DESC LIMIT 1
        """, (symbol,)).fetchone()
        if not row and not fundamental:
            connection.close()
            return None
        result: dict[str, Any] = {"symbol": symbol}
        if row:
            result.update(dict(row))
            result["diagnosis"] = json.loads(result.pop("diagnosis_json"))
        if fundamental:
            fundamental_result = dict(fundamental)
            fundamental_result["fundamental"] = json.loads(fundamental_result.pop("fundamental_json"))
            result["fundamental"] = fundamental_result
        try:
            membership = connection.execute("""
              SELECT generated_at,concept_tags_json FROM stock_board_memberships
              WHERE symbol=? ORDER BY generated_at DESC LIMIT 1
            """, (symbol,)).fetchone()
            if membership:
                result["concepts"] = {
                    "generated_at": membership["generated_at"],
                    "concept_tags": json.loads(membership["concept_tags_json"]),
                }
        except sqlite3.OperationalError:
            pass
        connection.close()
        return result
    except (sqlite3.Error, json.JSONDecodeError):
        return None


def _chart_payload(symbol: str, period: str, limit: int) -> dict[str, Any]:
    if len(symbol) != 6 or not symbol.isdigit():
        raise HTTPException(status_code=422, detail="股票代码必须是6位数字")
    limit = min(max(int(limit), 20), 250)
    if period == "1d":
        frame = fetch_history(symbol, limit)
        bars = []
        for row in frame.tail(limit).to_dict("records"):
            def number(value: Any) -> float | int | None:
                try:
                    result = float(value)
                    return result if math.isfinite(result) else None
                except (TypeError, ValueError):
                    return None
            bars.append({
                "time": str(row.get("date"))[:10],
                "open": number(row.get("open")), "close": number(row.get("close")),
                "high": number(row.get("high")), "low": number(row.get("low")),
                "volume": number(row.get("volume")), "amount": number(row.get("amount")),
            })
        return {"symbol": symbol, "period": period, "bars": bars, "source": "history"}
    if period not in {"5m", "1m"}:
        raise HTTPException(status_code=422, detail="period仅支持1d/5m/1m")
    if not SNAPSHOT_DB.exists():
        return {"symbol": symbol, "period": period, "bars": [], "source": "snapshot_db"}
    connection = sqlite3.connect(f"file:{SNAPSHOT_DB}?mode=ro&immutable=1", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    rows = connection.execute("""SELECT bar_time AS time,open,close,high,low,
      volume_shares AS volume,amount_estimated AS amount,provider
      FROM intraday_bars WHERE symbol=? ORDER BY bar_time DESC LIMIT ?""", (symbol, limit)).fetchall()
    connection.close()
    return {"symbol": symbol, "period": "5m", "bars": [dict(row) for row in reversed(rows)], "source": "snapshot_db"}


def _stock_performance(symbol: str) -> dict[str, Any]:
    if not SNAPSHOT_DB.exists():
        return {"samples": 0, "reason": "暂无生命周期数据库"}
    try:
        connection = sqlite3.connect(f"file:{SNAPSHOT_DB}?mode=ro&immutable=1", uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        rows = connection.execute("""
          SELECT status,first_seen_at,entry_reached_at,closed_at,mfe_pct,mae_pct,close_reason,
                 opportunity_id,recommendation_type
          FROM recommendation_lifecycle WHERE symbol=?
          ORDER BY first_seen_at DESC LIMIT 120
        """, (symbol,)).fetchall()
        observations = []
        try:
            observations = [dict(row) for row in connection.execute("""
              SELECT horizon,completed_at,mfe_pct,mae_pct,target_hit,invalidated
              FROM recommendation_observations WHERE symbol=?
              ORDER BY due_at DESC LIMIT 120
            """, (symbol,)).fetchall()]
        except sqlite3.OperationalError:
            observations = []
        connection.close()
    except sqlite3.Error:
        return {"samples": 0, "reason": "生命周期读取失败"}
    reached = [row for row in rows if row["entry_reached_at"]]
    target = [row for row in rows if row["status"] == "target_hit"]
    invalid = [row for row in rows if row["status"] == "invalidated"]
    mfe = [float(row["mfe_pct"]) for row in reached if row["mfe_pct"] is not None]
    mae = [float(row["mae_pct"]) for row in reached if row["mae_pct"] is not None]
    return {
        "samples": len(rows),
        "entry_reached_samples": len(reached),
        "entry_reached_pct": round(len(reached) / len(rows) * 100, 2) if rows else 0,
        "target_hit_samples": len(target),
        "invalidated_samples": len(invalid),
        "average_mfe_pct": round(sum(mfe) / len(mfe), 3) if mfe else None,
        "average_mae_pct": round(sum(mae) / len(mae), 3) if mae else None,
        "fixed_observations": observations[:20],
        "records": [dict(row) for row in rows[:20]],
        "note": "样本不足时不展示胜率；这里仅展示固定观察与生命周期事实",
    }


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/api/session")
def session(username: str = Depends(require_auth)) -> JSONResponse:
    return JSONResponse({
        "username": username,
        "csrf_token": _csrf_token(username),
        "csrf_ttl_seconds": CSRF_TTL_SECONDS,
    }, headers={"Cache-Control": "no-store"})


@app.get("/")
def index(_: str = Depends(require_auth)) -> FileResponse:
    return FileResponse(APP_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/stock")
def stock_page(_: str = Depends(require_auth)) -> FileResponse:
    return FileResponse(APP_DIR / "stock.html", headers={"Cache-Control": "no-store"})


@app.get("/api/dashboard")
def dashboard(_: str = Depends(require_auth)) -> JSONResponse:
    return JSONResponse(build_payload(), headers={"Cache-Control": "no-store"})


@app.get("/api/signals")
def signals(symbol: str | None = None, _: str = Depends(require_auth)) -> JSONResponse:
    return JSONResponse(
        {"signals": _signals_payload(symbol=symbol, limit=24)},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/review")
def review(_: str = Depends(require_auth)) -> JSONResponse:
    return JSONResponse(build_review(SNAPSHOT_DB, portfolio_db=PORTFOLIO_DB), headers={"Cache-Control": "no-store"})


@app.get("/api/diagnosis/{symbol}")
def diagnosis(symbol: str, _: str = Depends(require_auth)) -> JSONResponse:
    result = _diagnosis_detail(symbol)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="暂无该标的诊股数据")
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.get("/api/stocks/{symbol}/detail")
def stock_detail(symbol: str, _: str = Depends(require_auth)) -> JSONResponse:
    result = _diagnosis_detail(symbol)
    if result is None:
        raise HTTPException(status_code=404, detail="暂无该标的详情")
    recommendations = _load(RECOMMENDATION_PATH, {}).get("recommendations") or []
    result["recommendation"] = next((row for row in recommendations if str(row.get("symbol")) == symbol), None)
    result["accounts"] = [row for row in _combined_portfolio_holdings() if row.get("symbol") == symbol]
    result["history"] = _stock_performance(symbol)
    result["signals"] = _signals_payload(symbol=symbol, limit=8)
    result["global_factors"] = _stock_global_factors(result)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.get("/api/stocks/{symbol}/chart")
def stock_chart(symbol: str, period: str = "5m", limit: int = 120,
                _: str = Depends(require_auth)) -> JSONResponse:
    return JSONResponse(_chart_payload(symbol, period, limit), headers={"Cache-Control": "no-store"})


from portfolio_api import create_portfolio_router
app.include_router(create_portfolio_router(PORTFOLIO_DB, require_auth, require_write_auth, _portfolio_prices))
