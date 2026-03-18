# -*- coding: utf-8 -*-
"""
模拟盘接口
GET  /api/v1/quant/portfolio  - 持仓 + 账户 + 最近交易
POST /api/v1/quant/order      - 手动下单（买入/卖出）
"""

import logging
import time
import threading
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter()

# ─── 价格缓存（非阻塞后台刷新，避免每次 API 请求都等外部数据源超时）─────
_price_cache: dict = {}        # {code: price}
_price_cache_ts: float = 0.0   # 上次成功更新时间戳
_PRICE_CACHE_TTL = 60          # 缓存有效期（秒），超过后触发后台刷新
_price_fetch_lock = threading.Lock()
_price_fetch_running = False    # 防止并发重复刷新


def _get_broker():
    """每次请求加载最新的模拟盘数据（从 JSON 文件读取）"""
    from quant.broker.paper_broker import PaperBroker
    return PaperBroker()


def _do_fetch_prices(codes: list) -> dict:
    """用 get_daily_df（内置多数据源 fallback）取每只股最新收盘价。"""
    from src.screening.indicators import get_daily_df
    result = {}
    for code in codes:
        try:
            df = get_daily_df(code, days=3)
            if df is not None and not df.empty:
                price = float(df.iloc[-1]["close"])
                if price > 0:
                    result[code] = price
        except Exception:
            pass
    return result


def _bg_refresh(codes: list) -> None:
    """后台线程：拉价格并更新缓存，完成后释放锁。"""
    global _price_cache, _price_cache_ts, _price_fetch_running
    try:
        prices = _do_fetch_prices(codes)
        if prices:
            with _price_fetch_lock:
                _price_cache.update(prices)
                _price_cache_ts = time.time()
            logger.info(f"[portfolio] 价格缓存刷新: {prices}")
    except Exception as e:
        logger.debug(f"[portfolio] 后台价格刷新失败: {e}")
    finally:
        _price_fetch_running = False


def _get_prices_nonblocking(codes: list) -> dict:
    """
    立即返回缓存价格；若缓存已过期且没有正在进行的刷新，触发后台线程更新。
    调用方永远不会阻塞等待外部数据源。
    """
    global _price_fetch_running
    now = time.time()

    with _price_fetch_lock:
        cached = {c: _price_cache[c] for c in codes if c in _price_cache}
        cache_age = now - _price_cache_ts
        should_refresh = (cache_age > _PRICE_CACHE_TTL) and not _price_fetch_running

    if should_refresh:
        _price_fetch_running = True
        t = threading.Thread(target=_bg_refresh, args=(codes,), daemon=True)
        t.start()

    return cached


# ─────────────────────────────────────────────
# GET /portfolio
# ─────────────────────────────────────────────

@router.get("/portfolio")
def get_portfolio():
    """返回账户概况、持仓列表、最近 30 笔交易"""
    try:
        broker = _get_broker()

        # 非阻塞价格更新：立即用缓存，后台刷新不影响响应时间
        positions_raw = broker.get_positions()
        if positions_raw:
            codes = [p.stock_code for p in positions_raw]
            price_map = _get_prices_nonblocking(codes)
            if price_map:
                broker.update_position_prices(price_map)

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
