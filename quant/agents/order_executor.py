# -*- coding: utf-8 -*-
"""
===================================
量化交易系统 - 下单执行 Agent
===================================

职责：
最终执行层，负责实际调用 Broker 接口完成下单操作。
提供买入、卖出、批量执行等接口，并记录执行日志。

架构角色：
PortfolioManager -> OrderExecutor -> Broker -> 交易所/模拟盘
"""

import logging
from typing import List, Optional, Tuple

from quant.broker.base import BaseBroker
from quant.models import OrderSignal, TradeAction, TradeRecord, TradeStatus

logger = logging.getLogger(__name__)


class OrderExecutor:
    """
    下单执行 Agent。

    最终的执行层，封装 Broker 调用，提供统一的下单接口。
    所有实际的交易动作（买入/卖出）都通过此类执行。
    """

    def __init__(self, broker: BaseBroker):
        """
        初始化执行器。

        Args:
            broker: 券商接口实例（PaperBroker 或 FutuBroker）
        """
        self.broker = broker

    def execute_buy(self, signal: OrderSignal, quantity: int) -> TradeRecord:
        """
        执行买入操作。

        使用信号中的 ideal_buy_price 作为限价下单。

        Args:
            signal: 包含买入价格信息的交易信号
            quantity: 买入数量（股）

        Returns:
            TradeRecord 交易记录
        """
        if quantity <= 0:
            logger.warning(f"买入数量无效: {quantity}，跳过执行")
            return TradeRecord.create(
                stock_code=signal.stock_code,
                action=TradeAction.BUY,
                quantity=quantity,
                price=signal.ideal_buy_price,
                commission=0.0,
                reason="无效数量",
                status=TradeStatus.REJECTED,
            )

        if signal.ideal_buy_price <= 0:
            logger.warning(f"买入价格无效: {signal.ideal_buy_price}，跳过执行")
            return TradeRecord.create(
                stock_code=signal.stock_code,
                action=TradeAction.BUY,
                quantity=quantity,
                price=0.0,
                commission=0.0,
                reason="无效价格",
                status=TradeStatus.REJECTED,
            )

        try:
            logger.info(
                f"执行买入: {signal.stock_code}({signal.stock_name}) "
                f"{quantity}股 @ {signal.ideal_buy_price:.2f}, "
                f"原因: {signal.buy_reason[:50] if signal.buy_reason else '无'}"
            )
            record = self.broker.place_order(
                stock_code=signal.stock_code,
                action="BUY",
                quantity=quantity,
                price=signal.ideal_buy_price,
                order_type="LIMIT",
            )
            # 更新券商持仓中的股票名称
            try:
                if hasattr(self.broker, 'update_position_name'):
                    self.broker.update_position_name(signal.stock_code, signal.stock_name)
                # 同时设置止损价
                if signal.stop_loss_price > 0 and hasattr(self.broker, 'update_stop_loss'):
                    self.broker.update_stop_loss(signal.stock_code, signal.stop_loss_price)
            except Exception:
                pass

            logger.info(
                f"买入执行结果: {signal.stock_code} 状态={record.status.value}"
            )
            return record

        except Exception as e:
            logger.error(f"买入执行异常: {signal.stock_code} - {e}")
            return TradeRecord.create(
                stock_code=signal.stock_code,
                action=TradeAction.BUY,
                quantity=quantity,
                price=signal.ideal_buy_price,
                commission=0.0,
                reason=f"执行异常: {e}",
                status=TradeStatus.REJECTED,
            )

    def execute_sell(
        self,
        stock_code: str,
        quantity: int,
        price: float,
        reason: str = "",
    ) -> TradeRecord:
        """
        执行卖出操作。

        Args:
            stock_code: 股票代码
            quantity: 卖出数量（股）
            price: 卖出价格
            reason: 卖出原因（如 "止损" / "止盈" / "再平衡"）

        Returns:
            TradeRecord 交易记录
        """
        if quantity <= 0:
            logger.warning(f"卖出数量无效: {quantity}，跳过执行")
            return TradeRecord.create(
                stock_code=stock_code,
                action=TradeAction.SELL,
                quantity=quantity,
                price=price,
                commission=0.0,
                reason="无效数量",
                status=TradeStatus.REJECTED,
            )

        if price <= 0:
            logger.warning(f"卖出价格无效: {price}，跳过执行")
            return TradeRecord.create(
                stock_code=stock_code,
                action=TradeAction.SELL,
                quantity=quantity,
                price=price,
                commission=0.0,
                reason="无效价格",
                status=TradeStatus.REJECTED,
            )

        try:
            logger.info(
                f"执行卖出: {stock_code} {quantity}股 @ {price:.2f}, "
                f"原因: {reason or '无'}"
            )
            record = self.broker.place_order(
                stock_code=stock_code,
                action="SELL",
                quantity=quantity,
                price=price,
                order_type="LIMIT",
            )
            logger.info(
                f"卖出执行结果: {stock_code} 状态={record.status.value}"
            )
            return record

        except Exception as e:
            logger.error(f"卖出执行异常: {stock_code} - {e}")
            return TradeRecord.create(
                stock_code=stock_code,
                action=TradeAction.SELL,
                quantity=quantity,
                price=price,
                commission=0.0,
                reason=f"执行异常: {e}",
                status=TradeStatus.REJECTED,
            )

    def batch_execute(
        self,
        signals: List[Tuple],
    ) -> List[TradeRecord]:
        """
        批量执行多个交易信号。

        按顺序执行，单个失败不影响后续执行。

        Args:
            signals: 信号列表，每个元素为 (action, args...) 的元组。
                     买入: ("buy", signal, quantity)
                     卖出: ("sell", stock_code, quantity, price, reason)

        Returns:
            所有交易记录列表
        """
        records = []
        for item in signals:
            try:
                if not item:
                    continue

                action = str(item[0]).lower()
                if action == "buy" and len(item) >= 3:
                    signal, quantity = item[1], item[2]
                    record = self.execute_buy(signal, quantity)
                    records.append(record)

                elif action == "sell" and len(item) >= 4:
                    stock_code, quantity, price = item[1], item[2], item[3]
                    reason = item[4] if len(item) > 4 else ""
                    record = self.execute_sell(stock_code, quantity, price, reason)
                    records.append(record)

                else:
                    logger.warning(f"未知的批量执行格式: {item}")

            except Exception as e:
                logger.error(f"批量执行单项失败: {item} - {e}")

        logger.info(f"批量执行完成: 共 {len(records)} 笔交易")
        return records
