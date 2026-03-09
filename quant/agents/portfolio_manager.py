# -*- coding: utf-8 -*-
"""
===================================
量化交易系统 - 仓位管理 Agent
===================================

职责：
1. 接收交易信号，完成完整的交易决策流程
2. 协调 RiskGuard（风险检查）、position_sizing（仓位计算）、OrderExecutor（执行）
3. 维护仓位再平衡逻辑
4. 提供组合状态摘要

处理流程：
信号 -> 风险检查 -> 仓位计算 -> 下单执行 -> 更新记录
"""

import logging
from typing import List, Optional

from quant.agents.order_executor import OrderExecutor
from quant.agents.risk_guard import RiskGuard
from quant.broker.base import BaseBroker
from quant.models import OrderSignal, Portfolio, SignalType, TradeRecord, TradeStatus
from quant.strategies.position_sizing import calculate_position_size

logger = logging.getLogger(__name__)

# 每手股数
SHARES_PER_LOT = 100


class PortfolioManager:
    """
    仓位管理 Agent。

    整合风险守卫、仓位计算和下单执行，提供完整的交易决策执行能力。
    """

    def __init__(
        self,
        broker: BaseBroker,
        sizing_method: str = "atr_based",
        config=None,
    ):
        """
        初始化仓位管理器。

        Args:
            broker: 券商接口（PaperBroker 或 FutuBroker）
            sizing_method: 仓位计算方法 "fixed_fraction" / "kelly" / "atr_based"
            config: 量化配置（QuantConfig 实例，可选）
        """
        self.broker = broker
        self.sizing_method = sizing_method
        self._config = config

        # 初始化子组件
        blacklist = getattr(config, 'blacklist', []) if config else []
        self.risk_guard = RiskGuard(blacklist=blacklist, config=config)
        self.executor = OrderExecutor(broker=broker)

    def process_signal(self, signal: OrderSignal) -> Optional[TradeRecord]:
        """
        处理一个交易信号，完成风险检查、仓位计算、下单的完整流程。

        Args:
            signal: 交易信号

        Returns:
            TradeRecord 交易记录（成功则 status=FILLED，失败则 REJECTED），
            信号被过滤时返回 None
        """
        if signal.signal_type == SignalType.HOLD:
            logger.debug(f"信号类型为 HOLD，跳过: {signal.stock_code}")
            return None

        portfolio = self.broker.get_portfolio()

        if signal.signal_type == SignalType.BUY:
            return self._process_buy_signal(signal, portfolio)
        elif signal.signal_type == SignalType.SELL:
            return self._process_sell_signal(signal, portfolio)

        return None

    def _process_buy_signal(
        self,
        signal: OrderSignal,
        portfolio: Portfolio,
    ) -> Optional[TradeRecord]:
        """
        处理买入信号的内部逻辑。

        Args:
            signal: 买入信号
            portfolio: 当前投资组合

        Returns:
            TradeRecord 或 None
        """
        # Step 1: 仓位计算
        sizing_kwargs = {}
        if self._config:
            if self.sizing_method == "fixed_fraction":
                sizing_kwargs['fraction'] = getattr(self._config, 'fixed_fraction', 0.10)
            elif self.sizing_method == "kelly":
                sizing_kwargs['win_rate'] = getattr(self._config, 'kelly_win_rate', 0.55)
                sizing_kwargs['win_loss_ratio'] = getattr(self._config, 'kelly_win_loss_ratio', 1.5)

        lots = calculate_position_size(
            portfolio=portfolio,
            signal=signal,
            method=self.sizing_method,
            **sizing_kwargs,
        )

        if lots <= 0:
            logger.info(f"仓位计算结果为0，跳过买入: {signal.stock_code}")
            return None

        quantity = lots * SHARES_PER_LOT
        price = signal.ideal_buy_price

        # Step 2: 风险检查
        passed, reason = self.risk_guard.check_buy(
            portfolio=portfolio,
            signal=signal,
            quantity=quantity,
            price=price,
        )

        if not passed:
            logger.info(f"风险检查未通过，拒绝买入 {signal.stock_code}: {reason}")
            return None

        # Step 3: 执行买入
        record = self.executor.execute_buy(signal=signal, quantity=quantity)

        if record.status == TradeStatus.FILLED:
            logger.info(
                f"买入成功: {signal.stock_code} {quantity}股 @ {price:.2f}, "
                f"手续费 {record.commission:.2f}"
            )
        else:
            logger.warning(
                f"买入失败: {signal.stock_code} 状态={record.status.value} "
                f"原因={record.reason}"
            )

        return record

    def _process_sell_signal(
        self,
        signal: OrderSignal,
        portfolio: Portfolio,
    ) -> Optional[TradeRecord]:
        """
        处理卖出信号的内部逻辑。

        Args:
            signal: 卖出信号
            portfolio: 当前投资组合

        Returns:
            TradeRecord 或 None
        """
        code_upper = signal.stock_code.upper()
        if code_upper not in portfolio.positions:
            logger.info(f"未持有 {signal.stock_code}，跳过卖出信号")
            return None

        pos = portfolio.positions[code_upper]
        quantity = pos.quantity
        price = signal.ideal_buy_price if signal.ideal_buy_price > 0 else pos.current_price

        if price <= 0:
            logger.warning(f"卖出价格无效: {signal.stock_code}，使用当前持仓价")
            price = pos.avg_cost

        # 风险检查（卖出检查较宽松）
        passed, reason = self.risk_guard.check_sell(
            portfolio=portfolio,
            stock_code=signal.stock_code,
            quantity=quantity,
            price=price,
        )

        if not passed:
            logger.info(f"卖出风险检查未通过 {signal.stock_code}: {reason}")
            return None

        # 执行卖出
        record = self.executor.execute_sell(
            stock_code=signal.stock_code,
            quantity=quantity,
            price=price,
            reason=signal.buy_reason or "信号卖出",
        )

        if record.status == TradeStatus.FILLED:
            logger.info(
                f"卖出成功: {signal.stock_code} {quantity}股 @ {price:.2f}"
            )

        return record

    def check_and_stop_loss(self) -> List[TradeRecord]:
        """
        检查所有持仓是否触发止损，自动止损。

        Returns:
            止损产生的交易记录列表
        """
        portfolio = self.broker.get_portfolio()
        stop_loss_codes = self.risk_guard.check_stop_loss(portfolio)

        if not stop_loss_codes:
            logger.debug("止损检查：无需止损")
            return []

        records = []
        for code in stop_loss_codes:
            if code not in portfolio.positions:
                continue

            pos = portfolio.positions[code]
            # 用当前价止损（或稍低，模拟滑点）
            sell_price = pos.current_price

            logger.warning(
                f"触发止损: {code} 当前价={sell_price:.2f} "
                f"止损价={pos.stop_loss_price:.2f} "
                f"亏损={pos.pnl:.2f}({pos.pnl_pct:.2f}%)"
            )

            # 执行止损卖出
            passed, reason = self.risk_guard.check_sell(
                portfolio=portfolio,
                stock_code=code,
                quantity=pos.quantity,
                price=sell_price,
            )

            if not passed:
                logger.error(f"止损检查意外失败: {code} - {reason}")
                continue

            record = self.executor.execute_sell(
                stock_code=code,
                quantity=pos.quantity,
                price=sell_price,
                reason=f"止损触发: 跌破 {pos.stop_loss_price:.2f}",
            )
            records.append(record)

        return records

    def rebalance(self) -> List[TradeRecord]:
        """
        仓位再平衡：卖出亏损持仓，为新机会腾出空间。

        简单实现：
        - 卖出亏损超过 5% 且无止损价保护的持仓

        Returns:
            再平衡产生的交易记录列表
        """
        portfolio = self.broker.get_portfolio()
        records = []

        for code, pos in list(portfolio.positions.items()):
            # 已有止损保护的跳过
            if pos.stop_loss_price and pos.stop_loss_price > 0:
                continue

            # 亏损超过 5% 的考虑再平衡
            if pos.pnl_pct < -5.0:
                logger.info(
                    f"再平衡: {code} 亏损 {pos.pnl_pct:.2f}%，准备卖出"
                )
                sell_price = pos.current_price
                passed, reason = self.risk_guard.check_sell(
                    portfolio=portfolio,
                    stock_code=code,
                    quantity=pos.quantity,
                    price=sell_price,
                )

                if passed:
                    record = self.executor.execute_sell(
                        stock_code=code,
                        quantity=pos.quantity,
                        price=sell_price,
                        reason=f"再平衡: 亏损 {pos.pnl_pct:.2f}%",
                    )
                    records.append(record)
                    # 重新获取 portfolio（因为持仓已变化）
                    portfolio = self.broker.get_portfolio()

        logger.info(f"再平衡完成: 执行 {len(records)} 笔卖出")
        return records

    def get_portfolio_summary(self) -> dict:
        """
        返回当前组合状态摘要。

        Returns:
            包含账户状态、持仓明细、盈亏汇总的字典
        """
        try:
            portfolio = self.broker.get_portfolio()
            account_info = self.broker.get_account_info()

            position_details = []
            for code, pos in portfolio.positions.items():
                position_details.append({
                    "stock_code": pos.stock_code,
                    "stock_name": pos.stock_name,
                    "quantity": pos.quantity,
                    "avg_cost": round(pos.avg_cost, 3),
                    "current_price": round(pos.current_price, 3),
                    "market_value": round(pos.market_value, 2),
                    "pnl": round(pos.pnl, 2),
                    "pnl_pct": round(pos.pnl_pct, 2),
                    "stop_loss_price": pos.stop_loss_price,
                    "open_time": pos.open_time,
                })

            return {
                "account": account_info,
                "positions": position_details,
                "position_count": len(portfolio.positions),
                "max_positions": portfolio.max_positions,
                "total_pnl": round(portfolio.total_pnl, 2),
                "pnl_pct": round(portfolio.pnl_pct, 2),
                "sizing_method": self.sizing_method,
            }

        except Exception as e:
            logger.error(f"获取组合摘要失败: {e}")
            return {"error": str(e)}
