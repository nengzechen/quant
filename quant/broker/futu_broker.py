# -*- coding: utf-8 -*-
"""
===================================
量化交易系统 - 富途 OpenD 真实下单 Broker（可选）
===================================

依赖 futu-api（pip install futu-api）。
需要本地运行富途 OpenD 客户端（默认 localhost:11111）。

如果 futu-api 未安装，导入此模块会给出友好提示而不会报错。

支持：
- 港股、A股（沪深港通）、美股
- 市价单和限价单
- 模拟盘和真实盘（通过 trade_env 控制）
"""

import logging
from datetime import datetime
from typing import List, Optional

from quant.broker.base import BaseBroker
from quant.models import (
    Portfolio,
    Position,
    TradeAction,
    TradeRecord,
    TradeStatus,
)

logger = logging.getLogger(__name__)

# 尝试导入富途 API
try:
    import futu as ft
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False
    logger.warning(
        "富途 API 未安装，FutuBroker 不可用。"
        "请运行: pip install futu-api"
    )


def _check_futu_available():
    """检查富途 API 是否可用，不可用则抛出有意义的错误"""
    if not FUTU_AVAILABLE:
        raise RuntimeError(
            "futu-api 未安装。请运行: pip install futu-api\n"
            "同时需要运行富途 OpenD 客户端。"
        )


class FutuBroker(BaseBroker):
    """
    富途 OpenD 真实下单 Broker。

    通过富途 OpenD 客户端连接富途证券账户，支持港股/A股/美股下单。

    使用前提：
    1. 安装 futu-api: pip install futu-api
    2. 运行富途 OpenD 客户端（参考富途官方文档）
    3. 配置正确的 host/port 和 trade_env
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        trade_env: str = "SIMULATE",
        market: str = "HK",
        max_positions: int = 10,
        risk_per_trade_pct: float = 0.02,
    ):
        """
        初始化富途 Broker。

        Args:
            host: OpenD 地址
            port: OpenD 端口
            trade_env: 交易环境 "SIMULATE"（模拟）或 "REAL"（真实）
            market: 默认市场 "HK"/"US"/"CN"
            max_positions: 最大持仓数量
            risk_per_trade_pct: 单笔最大风险比例
        """
        _check_futu_available()

        self.host = host
        self.port = port
        self.trade_env = trade_env
        self.market = market
        self.max_positions = max_positions
        self.risk_per_trade_pct = risk_per_trade_pct

        self._quote_ctx: Optional[object] = None
        self._trade_ctx: Optional[object] = None
        self._account_id: Optional[str] = None

        self._connect()

    def _connect(self) -> None:
        """建立与 OpenD 的连接"""
        try:
            self._quote_ctx = ft.OpenQuoteContext(host=self.host, port=self.port)

            env = ft.TrdEnv.SIMULATE if self.trade_env == "SIMULATE" else ft.TrdEnv.REAL

            # 根据市场选择交易连接
            if self.market in ("HK", "US"):
                self._trade_ctx = ft.OpenHKTradeContext(host=self.host, port=self.port)
            else:
                # A 股通过港股通交易
                self._trade_ctx = ft.OpenHKTradeContext(host=self.host, port=self.port)

            self.trd_env = env
            logger.info(
                f"富途 OpenD 已连接: {self.host}:{self.port}, "
                f"环境: {self.trade_env}"
            )
        except Exception as e:
            logger.error(f"连接富途 OpenD 失败: {e}")
            raise

    def _convert_code(self, stock_code: str) -> str:
        """
        将内部股票代码转换为富途格式。

        例如：
        - "600519" -> "SH.600519"
        - "000858" -> "SZ.000858"
        - "AAPL" -> "US.AAPL"
        - "00700" -> "HK.00700"
        """
        if stock_code.isdigit():
            if stock_code.startswith('6') or stock_code.startswith('5'):
                return f"SH.{stock_code}"
            else:
                return f"SZ.{stock_code}"
        elif len(stock_code) == 5 and stock_code.isdigit():
            return f"HK.{stock_code}"
        else:
            return f"US.{stock_code}"

    def _get_accounts(self) -> List[str]:
        """获取账户列表"""
        try:
            ret, data = self._trade_ctx.get_acc_list()
            if ret == ft.RET_OK:
                return [str(row['acc_id']) for _, row in data.iterrows()]
            return []
        except Exception as e:
            logger.error(f"获取账户列表失败: {e}")
            return []

    def get_account_info(self) -> dict:
        """获取富途账户基本信息"""
        try:
            accounts = self._get_accounts()
            if not accounts:
                raise RuntimeError("未找到账户")
            acc_id = int(accounts[0])

            ret, data = self._trade_ctx.accinfo_query(
                trd_env=self.trd_env,
                acc_id=acc_id,
            )
            if ret != ft.RET_OK:
                raise RuntimeError(f"账户查询失败: {data}")

            row = data.iloc[0]
            return {
                "total_assets": float(row.get('total_assets', 0)),
                "available_cash": float(row.get('cash', 0)),
                "market_value": float(row.get('market_val', 0)),
                "total_pnl": float(row.get('unrealized_pl', 0)),
                "pnl_pct": 0.0,
                "total_capital": float(row.get('total_assets', 0)),
                "position_count": 0,
                "max_positions": self.max_positions,
            }
        except Exception as e:
            logger.error(f"获取富途账户信息失败: {e}")
            return {
                "total_assets": 0, "available_cash": 0,
                "market_value": 0, "total_pnl": 0,
                "pnl_pct": 0, "total_capital": 0,
                "position_count": 0, "max_positions": self.max_positions,
            }

    def place_order(
        self,
        stock_code: str,
        action: str,
        quantity: int,
        price: float,
        order_type: str = "LIMIT",
    ) -> TradeRecord:
        """通过富途 OpenD 下单"""
        try:
            futu_code = self._convert_code(stock_code)
            trade_side = ft.TrdSide.BUY if action.upper() == "BUY" else ft.TrdSide.SELL

            if order_type == "MARKET":
                order_type_ft = ft.OrderType.MARKET
            else:
                order_type_ft = ft.OrderType.NORMAL  # 普通限价单

            accounts = self._get_accounts()
            acc_id = int(accounts[0]) if accounts else 0

            ret, data = self._trade_ctx.place_order(
                price=price,
                qty=quantity,
                code=futu_code,
                trd_side=trade_side,
                order_type=order_type_ft,
                trd_env=self.trd_env,
                acc_id=acc_id,
            )

            if ret != ft.RET_OK:
                raise RuntimeError(f"下单失败: {data}")

            order_id = str(data.iloc[0]['order_id'])
            trade_action = TradeAction.BUY if action.upper() == "BUY" else TradeAction.SELL

            record = TradeRecord.create(
                stock_code=stock_code,
                action=trade_action,
                quantity=quantity,
                price=price,
                commission=0.0,  # 富途会自动扣除手续费
                reason=f"富途{self.trade_env}下单",
                status=TradeStatus.PENDING,
            )
            record.order_id = order_id
            logger.info(
                f"富途下单成功: {stock_code} {action} x{quantity} @ {price:.2f}, "
                f"订单ID: {order_id}"
            )
            return record

        except Exception as e:
            logger.error(f"富途下单失败: {e}")
            trade_action = TradeAction.BUY if action.upper() == "BUY" else TradeAction.SELL
            return TradeRecord.create(
                stock_code=stock_code,
                action=trade_action,
                quantity=quantity,
                price=price,
                commission=0.0,
                reason=f"富途下单失败: {e}",
                status=TradeStatus.REJECTED,
            )

    def cancel_order(self, order_id: str) -> bool:
        """撤销富途订单"""
        try:
            accounts = self._get_accounts()
            acc_id = int(accounts[0]) if accounts else 0

            ret, data = self._trade_ctx.modify_order(
                modify_order_op=ft.ModifyOrderOp.CANCEL,
                order_id=order_id,
                qty=0,
                price=0,
                trd_env=self.trd_env,
                acc_id=acc_id,
            )
            if ret == ft.RET_OK:
                logger.info(f"富途撤单成功: {order_id}")
                return True
            else:
                logger.warning(f"富途撤单失败: {data}")
                return False
        except Exception as e:
            logger.error(f"富途撤单异常: {e}")
            return False

    def get_positions(self) -> List[Position]:
        """获取富途账户持仓"""
        try:
            accounts = self._get_accounts()
            acc_id = int(accounts[0]) if accounts else 0

            ret, data = self._trade_ctx.position_list_query(
                trd_env=self.trd_env,
                acc_id=acc_id,
            )
            if ret != ft.RET_OK:
                logger.error(f"查询持仓失败: {data}")
                return []

            positions = []
            for _, row in data.iterrows():
                code = str(row.get('code', '')).split('.')[-1]  # 去掉市场前缀
                qty = int(row.get('qty', 0))
                cost = float(row.get('cost_price', 0))
                current = float(row.get('nominal_price', 0))
                pnl = float(row.get('pl_val', 0))
                positions.append(Position(
                    stock_code=code,
                    stock_name=str(row.get('stock_name', code)),
                    quantity=qty,
                    avg_cost=cost,
                    current_price=current,
                    market_value=qty * current,
                    pnl=pnl,
                    pnl_pct=float(row.get('pl_ratio', 0)) * 100,
                    open_time=datetime.now().isoformat(),
                ))
            return positions
        except Exception as e:
            logger.error(f"获取富途持仓失败: {e}")
            return []

    def get_order_status(self, order_id: str) -> str:
        """查询富途订单状态"""
        try:
            accounts = self._get_accounts()
            acc_id = int(accounts[0]) if accounts else 0

            ret, data = self._trade_ctx.order_list_query(
                order_id=order_id,
                trd_env=self.trd_env,
                acc_id=acc_id,
            )
            if ret != ft.RET_OK or data.empty:
                return TradeStatus.PENDING.value

            row = data.iloc[0]
            order_status = str(row.get('order_status', ''))

            # 富途状态映射
            status_map = {
                'SUBMITTED': TradeStatus.PENDING.value,
                'FILLED_ALL': TradeStatus.FILLED.value,
                'CANCELLED_ALL': TradeStatus.CANCELLED.value,
                'FAILED': TradeStatus.REJECTED.value,
            }
            return status_map.get(order_status, TradeStatus.PENDING.value)
        except Exception as e:
            logger.error(f"查询富途订单状态失败: {e}")
            return TradeStatus.PENDING.value

    def get_trade_records(self, limit: int = 50) -> List[TradeRecord]:
        """获取富途历史成交记录"""
        try:
            accounts = self._get_accounts()
            acc_id = int(accounts[0]) if accounts else 0

            ret, data = self._trade_ctx.deal_list_query(
                trd_env=self.trd_env,
                acc_id=acc_id,
            )
            if ret != ft.RET_OK:
                logger.error(f"查询成交记录失败: {data}")
                return []

            records = []
            for _, row in list(data.iterrows())[:limit]:
                code = str(row.get('code', '')).split('.')[-1]
                trd_side = str(row.get('trd_side', 'BUY'))
                action = TradeAction.BUY if 'BUY' in trd_side.upper() else TradeAction.SELL
                records.append(TradeRecord.create(
                    stock_code=code,
                    action=action,
                    quantity=int(row.get('qty', 0)),
                    price=float(row.get('price', 0)),
                    commission=0.0,
                    reason="富途成交",
                    status=TradeStatus.FILLED,
                ))
            return records
        except Exception as e:
            logger.error(f"获取富途成交记录失败: {e}")
            return []

    def get_portfolio(self) -> Portfolio:
        """获取富途账户投资组合"""
        try:
            account_info = self.get_account_info()
            positions = self.get_positions()
            positions_dict = {p.stock_code: p for p in positions}

            return Portfolio(
                total_capital=account_info['total_assets'],
                available_cash=account_info['available_cash'],
                total_market_value=account_info['market_value'],
                total_pnl=account_info['total_pnl'],
                positions=positions_dict,
                max_positions=self.max_positions,
                risk_per_trade_pct=self.risk_per_trade_pct,
            )
        except Exception as e:
            logger.error(f"获取富途投资组合失败: {e}")
            return Portfolio(
                total_capital=0,
                available_cash=0,
                total_market_value=0,
                total_pnl=0,
            )

    def __del__(self):
        """析构时关闭连接"""
        try:
            if self._quote_ctx:
                self._quote_ctx.close()
            if self._trade_ctx:
                self._trade_ctx.close()
        except Exception:
            pass
