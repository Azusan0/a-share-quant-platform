#!/usr/bin/env python3
"""创建已确认QQ路由账户并将旧自选安全迁移为关注项。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from portfolio_store import DEFAULT_DB, PortfolioStore


def migrate(db: str | Path, legacy: str | Path, route_a: str, route_b: str) -> dict:
    with PortfolioStore(db) as store:
        existing = {row["name"]: row for row in store.list_accounts()}
        mine = existing.get("我的账户") or store.create_account("我的账户", 0, route_a, actor="migration")
        friend = existing.get("朋友账户") or store.create_account("朋友账户", 0, route_b, actor="migration")
        path = Path(legacy)
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"holdings": []}
        migrated = 0
        for row in data.get("holdings") or []:
            if not row.get("symbol"):
                continue
            price = float(row.get("buy_price") or 0)
            store.upsert_watchlist(mine["account_id"], str(row["symbol"]), row.get("name"), status="watch",
                                   entry_low=price if price > 0 else None,
                                   invalid_price=row.get("stop_price") if float(row.get("stop_price") or 0) > 0 else None,
                                   target_price=row.get("target_price") if float(row.get("target_price") or 0) > 0 else None,
                                   note="从旧自选迁移；请在网页确认是否为真实持仓", actor="migration")
            migrated += 1
        return {"accounts": [mine["account_id"], friend["account_id"]], "watchlist_migrated": migrated}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--legacy", default="/root/.hermes/scripts/a_share_watchlist.json")
    parser.add_argument("--route-a", required=True)
    parser.add_argument("--route-b", required=True)
    args = parser.parse_args()
    print(json.dumps(migrate(args.db, args.legacy, args.route_a, args.route_b), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
