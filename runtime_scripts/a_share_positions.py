#!/usr/bin/env python3
"""
A 股自选监控录入入口 — CLI 增删查统一自选监控池（watchlist_file）

用法:
    python3 a_share_positions.py add <symbol> <name> [type] [quantity] [record_price]
    python3 a_share_positions.py list
    python3 a_share_positions.py remove <symbol>

示例:
    python3 a_share_positions.py add 512480 半导体ETF etf 1000 1.200
    python3 a_share_positions.py add 002371 北方华创 stock 200 180.50
    python3 a_share_positions.py list
    python3 a_share_positions.py remove 512480

配置文件: /root/.hermes/scripts/a_share_alert_runtime_config.json
实际监控池: 由配置中的 watchlist_file 指定，默认 /root/.hermes/scripts/a_share_watchlist.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

CONFIG_PATH = Path("/root/.hermes/scripts/a_share_alert_runtime_config.json")
DEFAULT_WATCHLIST_PATH = Path("/root/.hermes/scripts/a_share_watchlist.json")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        print(f"错误: 配置文件不存在 {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def watchlist_path(cfg: dict[str, Any]) -> Path:
    raw = cfg.get("watchlist_file") or str(DEFAULT_WATCHLIST_PATH)
    return Path(str(raw))


def load_watchlist(cfg: dict[str, Any]) -> dict[str, Any]:
    path = watchlist_path(cfg)
    if not path.exists():
        return {"holdings": []}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {"holdings": []}
    holdings = data.get("holdings")
    if not isinstance(holdings, list):
        data["holdings"] = []
    return data


def save_watchlist(cfg: dict[str, Any], watchlist: dict[str, Any]) -> None:
    path = watchlist_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2)
        f.write("\n")


def build_entry(symbol: str, name: str, ptype: str, quantity: float, record_price: float) -> dict[str, Any]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    buy_date = now.split(" ", 1)[0]
    price = round(record_price, 3) if record_price > 0 else 0.0
    return {
        "symbol": symbol,
        "name": name,
        "type": ptype,
        "quantity": quantity,
        "buy_price": price,
        "buy_date": buy_date,
        "highest_price": price,
        "target_price": round(price * 1.15, 3) if price > 0 else 0.0,
        "stop_price": round(price * 0.95, 3) if price > 0 else 0.0,
        "added_at": now,
        "source": "positions_cli",
    }


def cmd_add(args: list[str]) -> None:
    """add <symbol> <name> [type=stock] [quantity=0] [record_price=0.0]"""
    if len(args) < 2:
        print("用法: add <symbol> <name> [type] [quantity] [record_price]", file=sys.stderr)
        sys.exit(1)

    symbol = args[0]
    name = args[1]
    ptype = args[2] if len(args) > 2 else "stock"
    quantity = float(args[3]) if len(args) > 3 else 0.0
    record_price = float(args[4]) if len(args) > 4 else 0.0

    cfg = load_config()
    watchlist = load_watchlist(cfg)
    pool: list[dict[str, Any]] = watchlist.setdefault("holdings", [])

    for item in pool:
        if str(item.get("symbol", "")).strip() == symbol:
            print(f"错误: 自选监控池中已存在 {symbol} ({item.get('name') or symbol})", file=sys.stderr)
            print("如需更新请先 remove 再 add，或直接使用快捷更新入口呢。", file=sys.stderr)
            sys.exit(1)

    entry = build_entry(symbol, name, ptype, quantity, record_price)
    pool.append(entry)
    save_watchlist(cfg, watchlist)
    print(f"✓ 已加入自选监控: {symbol} {name}")


def cmd_list(args: list[str]) -> None:
    """list — 列出统一自选监控池"""
    _ = args
    cfg = load_config()
    watchlist = load_watchlist(cfg)
    pool: list[dict[str, Any]] = watchlist.get("holdings", [])
    if not pool:
        print("当前自选监控池为空")
        return

    print(f"{'代码':<8} {'名称':<12} {'类型':<6} {'数量':<10} {'记录价':<10}")
    print("-" * 52)
    for item in pool:
        qty = item.get("quantity", 0)
        price = item.get("buy_price", 0)
        qty_str = f"{qty:.0f}" if isinstance(qty, (int, float)) and qty == int(qty) else f"{float(qty):.2f}"
        price_str = f"{float(price):.3f}" if price else "-"
        print(f"{item['symbol']:<8} {item['name']:<12} {item.get('type','stock'):<6} {qty_str:<10} {price_str:<10}")


def cmd_remove(args: list[str]) -> None:
    """remove <symbol> — 从统一自选监控池移除"""
    if len(args) < 1:
        print("用法: remove <symbol>", file=sys.stderr)
        sys.exit(1)

    symbol = args[0]
    cfg = load_config()
    watchlist = load_watchlist(cfg)
    pool: list[dict[str, Any]] = watchlist.get("holdings", [])
    before = len(pool)
    watchlist["holdings"] = [item for item in pool if str(item.get("symbol", "")).strip() != symbol]
    after = len(watchlist["holdings"])

    if before == after:
        print(f"未找到自选监控记录: {symbol}", file=sys.stderr)
        sys.exit(1)

    save_watchlist(cfg, watchlist)
    print(f"✓ 已移除自选监控: {symbol}")


def print_help() -> None:
    print(__doc__.strip())


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print_help()
        return

    command = sys.argv[1]
    args = sys.argv[2:]

    if command == "add":
        cmd_add(args)
    elif command == "list":
        cmd_list(args)
    elif command == "remove":
        cmd_remove(args)
    else:
        print(f"未知命令: {command}\n", file=sys.stderr)
        print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
