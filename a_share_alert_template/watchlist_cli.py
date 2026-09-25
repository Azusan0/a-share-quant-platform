"""自选 / 持仓管理 CLI（#1）。

供 Hermes 在收到用户回复（如「加入自选 中科曙光」「自选列表」「删除自选 603019」）时调用。
维护一个独立的自选状态文件，被 monitor 读入 holding_pool，从而对自选标的持续跟踪、
到点发离场（止损/止盈/回撤）提醒，形成买入到卖出的闭环。

设计要点：
  - 独立状态文件（watchlist_file），不污染 runtime_state / config。
  - 加入自选时记录 buy_price（默认取当前价快照）、buy_date、目标/止损位，供离场判断。
  - 支持按名称或代码增删；名称→代码通过 config 的方向池 + 快照名称反查。
  - 所有命令输出中文结果行，Hermes 可直接转发给用户。

命令：
  python watchlist_cli.py add    --config C --query "中科曙光"      # 或 --symbol 603019
  python watchlist_cli.py remove --config C --query "603019"
  python watchlist_cli.py list   --config C
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from data_source import fetch_snapshot


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _watchlist_path(config: dict[str, Any]) -> Path:
    return Path(config.get("watchlist_file", "/root/.hermes/scripts/a_share_watchlist.json"))


def _all_known_members(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从方向池 + 关注池汇总所有已知标的：symbol -> {name, type}。"""
    known: dict[str, dict[str, Any]] = {}
    for pool_key in ("direction_pool", "message_focus_pool"):
        for item in config.get(pool_key, []) or []:
            for m in item.get("members", []):
                known[m["symbol"]] = {"name": m.get("name") or m["symbol"], "type": m.get("type", "stock")}
    return known


def _resolve_symbol(query: str, config: dict[str, Any]) -> tuple[str, str, str] | None:
    """把用户输入（代码或名称）解析成 (symbol, name, type)。找不到返回 None。"""
    query = query.strip()
    known = _all_known_members(config)
    # 1) 直接是代码
    if query in known:
        return query, known[query]["name"], known[query]["type"]
    # 2) 纯数字代码但不在池里 → 认作个股，名称先用代码占位
    if query.isdigit() and len(query) == 6:
        return query, query, "stock"
    # 3) 按名称匹配已知池
    for sym, info in known.items():
        if query == info["name"] or query in info["name"]:
            return sym, info["name"], info["type"]
    return None


def cmd_add(config: dict[str, Any], query: str, symbol: str | None) -> str:
    resolved = None
    if symbol:
        known = _all_known_members(config)
        info = known.get(symbol, {"name": symbol, "type": "stock"})
        resolved = (symbol, info["name"], info["type"])
    elif query:
        resolved = _resolve_symbol(query, config)
    if not resolved:
        return f"未能识别标的「{query or symbol}」，请用6位代码或池内名称重试。"

    sym, name, typ = resolved
    wl_path = _watchlist_path(config)
    watchlist = _load_json(wl_path, {"holdings": []})
    if any(h["symbol"] == sym for h in watchlist["holdings"]):
        return f"{name}({sym}) 已在自选中，无需重复添加。"

    # 取当前价作为 buy_price 基准（拿不到则置0，离场逻辑会跳过无效buy_price）。
    buy_price = 0.0
    try:
        snap = fetch_snapshot([sym]).get(sym)
        if snap:
            buy_price = float(snap.get("price", 0) or 0)
            name = snap.get("name") or name
    except Exception:
        pass

    strategy = config.get("strategy", {})
    take_profit = float(strategy.get("take_profit_pct", 15.0))
    stop_loss = float(strategy.get("stop_loss_pct", -5.0))
    entry = {
        "symbol": sym,
        "name": name,
        "type": typ,
        "buy_price": round(buy_price, 3),
        "buy_date": datetime.now().strftime("%Y-%m-%d"),
        "highest_price": round(buy_price, 3),
        "target_price": round(buy_price * (1 + take_profit / 100), 3) if buy_price > 0 else 0,
        "stop_price": round(buy_price * (1 + stop_loss / 100), 3) if buy_price > 0 else 0,
        "added_at": datetime.now().isoformat(timespec="seconds"),
    }
    watchlist["holdings"].append(entry)
    _save_json(wl_path, watchlist)
    price_txt = f"，记录价{buy_price:.3f}（止损{entry['stop_price']}/目标{entry['target_price']}）" if buy_price > 0 else "（未取到现价，稍后自动补）"
    return f"已加入自选：{name}({sym}){price_txt}。系统将持续跟踪并在触发止损/止盈/回撤时提醒。"


def cmd_remove(config: dict[str, Any], query: str) -> str:
    wl_path = _watchlist_path(config)
    watchlist = _load_json(wl_path, {"holdings": []})
    before = len(watchlist["holdings"])
    q = query.strip()
    kept = [h for h in watchlist["holdings"] if h["symbol"] != q and q not in (h.get("name") or "")]
    removed = before - len(kept)
    if removed == 0:
        return f"自选中未找到「{q}」。"
    watchlist["holdings"] = kept
    _save_json(wl_path, watchlist)
    return f"已从自选移除「{q}」（移除{removed}项）。"


def cmd_list(config: dict[str, Any]) -> str:
    wl_path = _watchlist_path(config)
    watchlist = _load_json(wl_path, {"holdings": []})
    holdings = watchlist.get("holdings", [])
    if not holdings:
        return "当前自选为空。可回复「加入自选 <名称或代码>」添加。"
    lines = ["当前自选列表："]
    for i, h in enumerate(holdings, 1):
        typ = "个股" if h.get("type") == "stock" else "ETF"
        base = f"{i}. {h.get('name')}({h['symbol']}, {typ})"
        if h.get("buy_price", 0) > 0:
            base += f"　记录价{h['buy_price']}　止损{h.get('stop_price')}　目标{h.get('target_price')}　加入于{h.get('buy_date')}"
        lines.append(base)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="A股自选/持仓管理")
    parser.add_argument("action", choices=["add", "remove", "list"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--query", default="", help="名称或代码")
    parser.add_argument("--symbol", default="", help="明确的6位代码")
    args = parser.parse_args()

    config = _load_json(Path(args.config), {})
    if args.action == "add":
        print(cmd_add(config, args.query, args.symbol or None))
    elif args.action == "remove":
        print(cmd_remove(config, args.query))
    else:
        print(cmd_list(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
