#!/usr/bin/env python3
"""按文件名A/B读取已确认QQ路由并输出待发送消息。"""
from __future__ import annotations
import json,sys
from pathlib import Path
sys.path.insert(0,"/usr/local/lib/hermes-agent/a_share_alert_template")
from portfolio_store import PortfolioStore

key=Path(__file__).stem.rsplit("_",1)[-1].upper()
routes=json.loads(Path("/root/.hermes/scripts/a_share_account_routes.json").read_text(encoding="utf-8"))
with PortfolioStore() as store:
    messages=store.emit_route(routes[key])
if messages: print("\n\n".join(messages))
