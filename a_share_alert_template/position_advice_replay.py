#!/usr/bin/env python3
"""历史分钟K回放动态退出，量化相对固定回撤规则的差异。"""
from __future__ import annotations
import argparse,json,sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from position_advice import evaluate_position
from portfolio_store import DEFAULT_DB,PortfolioStore


def replay(position: dict[str,Any],bars:list[dict[str,Any]],market_score:float=50,sector_score:float=50)->dict[str,Any]:
    if len(bars)<6:return {"quality":"insufficient","bars":len(bars)}
    high=float(position.get("average_cost") or 0);previous=None;first_dynamic=None;first_fixed=None;actions=[]
    for i,row in enumerate(bars):
        price=float(row["close"]);high=max(high,float(row.get("high") or price));drawdown=(price/high-1)*100 if high else 0
        if first_fixed is None and drawdown<=-2:first_fixed=row["time"]
        evidence={"market_score":market_score,"sector_score":sector_score,"intraday_state":"failed" if price<float(row.get("vwap") or price) else "setup","vwap_deviation_pct":((price/float(row.get("vwap") or price))-1)*100,"below_vwap_bars":2 if i>=1 and price<float(row.get("vwap") or price) else 0,"sector_weak":sector_score<35,"rebound_failed":i>=2 and price<bars[i-1]["close"]}
        current=evaluate_position(dict(position,highest_price=high),{"price":price,"high":high},evidence,previous=previous,now=datetime.fromisoformat(row["time"]));previous=current
        if current["action"] in {"trim","exit"}:actions.append(current);first_dynamic=first_dynamic or row["time"]
    return {"quality":"complete","bars":len(bars),"fixed_first_exit":first_fixed,"dynamic_first_exit":first_dynamic,"avoided_early_exit":bool(first_fixed and (not first_dynamic or first_dynamic>first_fixed)),"actions":actions,"final_price":float(bars[-1]["close"]),"max_price":high}


def run(portfolio_db:Path,market_db:Path,trade_date:str|None=None)->dict[str,Any]:
    c=sqlite3.connect(market_db);c.row_factory=sqlite3.Row
    if not trade_date:trade_date=str(c.execute("SELECT MAX(trade_date) FROM intraday_bars").fetchone()[0] or "")
    results=[]
    with PortfolioStore(portfolio_db) as store:
        for account in store.list_accounts():
            for position in store.portfolio(account["account_id"])["positions"]:
                rows=[dict(row) for row in c.execute("SELECT bar_time time,open,close,high,low,volume_shares,amount_estimated FROM intraday_bars WHERE symbol=? AND trade_date=? ORDER BY bar_time",(position["symbol"],trade_date))]
                volume=amount=0
                for row in rows:
                    volume+=float(row["volume_shares"]);amount+=float(row["amount_estimated"]);row["vwap"]=amount/volume if volume else row["close"]
                results.append({"account_id":account["account_id"],"account_name":account["name"],"symbol":position["symbol"],**replay(position,rows)})
    c.close();return {"generated_at":datetime.now().isoformat(timespec="seconds"),"trade_date":trade_date,"positions":len(results),"avoided_early_exit":sum(bool(row.get("avoided_early_exit")) for row in results),"results":results}


def main()->int:
    p=argparse.ArgumentParser();p.add_argument("--portfolio-db",default=str(DEFAULT_DB));p.add_argument("--market-db",default="/root/.hermes/scripts/a_share_market_snapshots.db");p.add_argument("--date");p.add_argument("--output",default="/var/lib/a-share-dashboard/position_advice_replay.json");a=p.parse_args();payload=run(Path(a.portfolio_db),Path(a.market_db),a.date);Path(a.output).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8");print(json.dumps({k:v for k,v in payload.items() if k!='results'},ensure_ascii=False));return 0
if __name__=="__main__":raise SystemExit(main())
