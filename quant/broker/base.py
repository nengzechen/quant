# -*- coding: utf-8 -*-
"""
===================================
量化交易系统 - 抽象券商接口
===================================

定义所有券商实现必须遵循的统一接口。
无论是模拟盘还是真实券商（富途等），都要实现此接口。
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from quant.models import Position, TradeRecord


class BaseBroker(ABC):
    """
    抽象券商接口。

    所有券商实现（模拟盘、富途等）必须继承此类并实现所有抽象方法。
    这样上层业务逻辑（PortfolioManager、OrderExecutor）可以与具体券商解耦。
    """

    @abstractmethod
    def get_account_info(self) -> dict:
        """
        获取账户基本信息。

        Returns:
            包含账户信息的字典，至少包含：
            {
                "total_assets": float,    # 总资产
                "available_cash": float,  # 可用现金
                "market_value": float,    # 持仓市值
                "total_pnl": float,       # 总盈亏
            }
        """
        ...

    @abstractmethod
    def place_order(
        self,
        stock_code: str,
        action: str,
        quantity: int,
        price: float,
        order_type: str = "LIMIT",
    ) -> TradeRecord:
        """
        下单。

        Args:
            stock_code: 股票代码（如 "600519"）
            action: 交易动作 "BUY" 或 "SELL"
            quantity: 交易数量（股）
            price: 委托价格（限价单）
            order_type: 订单类型 "LIMIT"（限价）或 "MARKET"（市价）

        Returns:
            TradeRecord 交易记录，状态为 PENDING 或 FILLED
        """
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """
        撤销订单。

        Args:
            order_id: 订单ID

        Returns:
            True 表示撤单成功，False 表示失败
        """
        ...

    @abstractmethod
    def get_positions(self) -> List[Position]:
        """
        获取当前所有持仓。

        Returns:
            持仓列表，每个元素为 Position 对象
        """
        ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> str:
        """
        查询订单状态。

        Args:
            order_id: 订单ID

        Returns:
            订单状态字符串："PENDING" / "FILLED" / "REJECTED" / "CANCELLED"
        """
        ...

    @abstractmethod
    def get_trade_records(self, limit: int = 50) -> List[TradeRecord]:
        """
        获取历史交易记录。

        Args:
            limit: 返回记录数量上限

        Returns:
            交易记录列表，按时间倒序排列
        """
        ...

    @abstractmethod
    def get_portfolio(self):
        """
        获取完整投资组合状态。

        Returns:
            Portfolio 对象
        """
        ...
