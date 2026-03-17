# -*- coding: utf-8 -*-
"""
模拟盘接口
GET  /api/v1/quant/portfolio  - 持仓 + 账户 + 最近交易
POST /api/v1/quant/order      - 手动下单（买入/卖出）
"""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_broker():
    """每次请求加载最新的模拟盘数据（从 JSON 文件读取）"""
    from quant.broker.paper_broker import PaperBroker
    return PaperBroker()


# ─────────────────────────────────────────────
# GET /portfolio
# ─────────────────────────────────────────────

@router.get("/portfolio")
def get_portfolio():
    """返回账户概况、持仓列表、最近 30 笔交易"""
    try:
        broker = _get_broker()
        account = broker.get_account_info()
        positions = broker.get_positions()
        trades = broker.get_trade_records(limit=30)

        return {
            "account": account,
            "positions": [
                {
                    "stock_code": p.stock_code,
                    "stock_name": p.stock_name or p.stock_code,
                    "quantity": p.quantity,
                    "avg_cost": round(p.avg_cost, 3),
                    "current_price": round(p.current_price, 3),
                    "market_value": round(p.market_value, 2),
                    "pnl": round(p.pnl, 2),
                    "pnl_pct": round(p.pnl_pct, 2),
                    "stop_loss_price": p.stop_loss_price,
                    "open_time": p.open_time,
                }
                for p in positions
            ],
            "trades": [t.to_dict() for t in trades],
        }
    except Exception as e:
        logger.error(f"获取持仓失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# POST /order
# ─────────────────────────────────────────────

class OrderRequest(BaseModel):
    stock_code: str = Field(..., description="股票代码，如 600519")
    action: Literal["BUY", "SELL"] = Field(..., description="买入或卖出")
    quantity: int = Field(..., gt=0, description="交易股数（须为 100 的整数倍）")
    price: float = Field(..., gt=0, description="委托价格")
    stop_loss_price: float = Field(0.0, description="止损价（仅买入时有效）")


@router.post("/order")
def place_order(req: OrderRequest):
    """手动下单"""
    if req.quantity % 100 != 0:
        raise HTTPException(status_code=400, detail="股数须为 100 的整数倍")

    try:
        broker = _get_broker()
        record = broker.place_order(
            stock_code=req.stock_code,
            action=req.action,
            quantity=req.quantity,
            price=req.price,
        )

        # 买入时写入止损价
        if req.action == "BUY" and req.stop_loss_price > 0:
            try:
                broker.update_stop_loss(req.stock_code, req.stop_loss_price)
            except Exception:
                pass

        return {"status": record.status.value, "trade": record.to_dict()}
    except Exception as e:
        logger.error(f"下单失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
