# -*- coding: utf-8 -*-
"""
===================================
量化交易系统 - 模拟盘（Paper Trading Broker）
===================================

本地 JSON 文件持久化的模拟盘实现。

特性：
- 账户状态持久化到 ~/.stock_quant/paper_account.json
- 买卖立即成交（模拟限价单立即成交）
- 手续费：买入万分之三 + 卖出万分之三 + 印花税千分之一（仅卖出）
- 支持完整的 BaseBroker 接口
"""

import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
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

# 手续费常量
COMMISSION_RATE_BUY = 0.0003     # 买入佣金：万分之三
COMMISSION_RATE_SELL = 0.0003    # 卖出佣金：万分之三
STAMP_DUTY_RATE = 0.001          # 印花税：千分之一（仅卖出）
MIN_COMMISSION = 5.0             # 最低佣金：5 元


class PaperBroker(BaseBroker):
    """
    模拟盘券商实现。

    使用本地 JSON 文件持久化账户状态，模拟真实交易行为。
    适用于策略验证和回测，无需真实资金。
    """

    def __init__(
        self,
        account_path: str = None,
        initial_capital: float = 1_000_000,
        max_positions: int = 10,
        risk_per_trade_pct: float = 0.02,
    ):
        """
        初始化模拟券商。

        Args:
            account_path: 账户数据文件路径（默认 ~/.stock_quant/paper_account.json）
            initial_capital: 初始资金
            max_positions: 最大持仓数量
            risk_per_trade_pct: 单笔最大风险比例
        """
        if account_path is None:
            account_path = os.path.expanduser("~/.stock_quant/paper_account.json")

        self.account_path = Path(account_path)
        self.initial_capital = initial_capital
        self.max_positions = max_positions
        self.risk_per_trade_pct = risk_per_trade_pct

        # 确保账户数据目录存在
        self.account_path.parent.mkdir(parents=True, exist_ok=True)

        # 加载或初始化账户
        self._portfolio = self._load_or_init()

    # ===== 内部方法 =====

    def _load_or_init(self) -> Portfolio:
        """加载已有账户或初始化新账户"""
        if self.account_path.exists():
            try:
                with open(self.account_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                portfolio = Portfolio.from_dict(data['portfolio'])
                logger.info(
                    f"模拟账户已加载: 总资产={portfolio.total_assets:.2f}, "
                    f"可用现金={portfolio.available_cash:.2f}"
                )
                return portfolio
            except Exception as e:
                logger.warning(f"加载模拟账户失败，将重新初始化: {e}")

        return self._init_account()

    def _init_account(self) -> Portfolio:
        """初始化新账户"""
        portfolio = Portfolio(
            total_capital=self.initial_capital,
            available_cash=self.initial_capital,
            total_market_value=0.0,
            total_pnl=0.0,
            positions={},
            max_positions=self.max_positions,
            risk_per_trade_pct=self.risk_per_trade_pct,
        )
        logger.info(f"模拟账户初始化完成，初始资金: {self.initial_capital:.2f}")
        self._save(portfolio)
        return portfolio

    def _save(self, portfolio: Optional[Portfolio] = None) -> None:
        """持久化账户数据到 JSON 文件"""
        if portfolio is None:
            portfolio = self._portfolio
        try:
            # 读取现有交易记录（不在 Portfolio 中存储）
            trade_records = []
            if self.account_path.exists():
                try:
                    with open(self.account_path, 'r', encoding='utf-8') as f:
                        existing = json.load(f)
                        trade_records = existing.get('trade_records', [])
                except Exception:
                    pass

            data = {
                'portfolio': portfolio.to_dict(),
                'trade_records': trade_records,
                'last_updated': datetime.now().isoformat(),
            }
            with open(self.account_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存模拟账户数据失败: {e}")

    def _save_trade_record(self, record: TradeRecord) -> None:
        """追加一条交易记录到 JSON 文件"""
        try:
            data = {}
            if self.account_path.exists():
                with open(self.account_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)

            records = data.get('trade_records', [])
            records.insert(0, record.to_dict())  # 最新记录排在最前
            data['trade_records'] = records
            data['last_updated'] = datetime.now().isoformat()

            with open(self.account_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存交易记录失败: {e}")

    def _calculate_buy_commission(self, amount: float) -> float:
        """计算买入手续费（佣金，不含印花税）"""
        commission = amount * COMMISSION_RATE_BUY
        return max(commission, MIN_COMMISSION)

    def _calculate_sell_commission(self, amount: float) -> float:
        """计算卖出手续费（佣金 + 印花税）"""
        commission = amount * COMMISSION_RATE_SELL
        commission = max(commission, MIN_COMMISSION)
        stamp_duty = amount * STAMP_DUTY_RATE
        return commission + stamp_duty

    # ===== BaseBroker 接口实现 =====

    def get_account_info(self) -> dict:
        """获取账户基本信息"""
        p = self._portfolio
        p.recalculate()
        return {
            "total_assets": p.total_assets,
            "available_cash": p.available_cash,
            "market_value": p.total_market_value,
            "total_pnl": p.total_pnl,
            "pnl_pct": p.pnl_pct,
            "total_capital": p.total_capital,
            "position_count": len(p.positions),
            "max_positions": p.max_positions,
        }

    def place_order(
        self,
        stock_code: str,
        action: str,
        quantity: int,
        price: float,
        order_type: str = "LIMIT",
    ) -> TradeRecord:
        """
        模拟下单（立即成交）。

        模拟盘实现：限价单按委托价立即成交（不考虑盘口深度）。
        """
        try:
            trade_action = TradeAction(action.upper())
        except ValueError:
            logger.error(f"无效的交易动作: {action}")
            return TradeRecord.create(
                stock_code=stock_code,
                action=TradeAction.BUY,
                quantity=quantity,
                price=price,
                commission=0.0,
                reason="无效的交易动作",
                status=TradeStatus.REJECTED,
            )

        amount = price * quantity

        if trade_action == TradeAction.BUY:
            commission = self._calculate_buy_commission(amount)
            total_cost = amount + commission

            # 检查可用现金
            if total_cost > self._portfolio.available_cash:
                logger.warning(
                    f"可用现金不足: 需要 {total_cost:.2f}, "
                    f"可用 {self._portfolio.available_cash:.2f}"
                )
                record = TradeRecord.create(
                    stock_code=stock_code,
                    action=trade_action,
                    quantity=quantity,
                    price=price,
                    commission=commission,
                    reason="可用现金不足",
                    status=TradeStatus.REJECTED,
                )
                self._save_trade_record(record)
                return record

            # 执行买入
            self._portfolio.available_cash -= total_cost

            if stock_code in self._portfolio.positions:
                # 已有持仓：计算新的平均成本
                pos = self._portfolio.positions[stock_code]
                total_qty = pos.quantity + quantity
                avg_cost = (pos.avg_cost * pos.quantity + price * quantity) / total_qty
                pos.quantity = total_qty
                pos.avg_cost = avg_cost
                pos.current_price = price
                pos.market_value = total_qty * price
                pos.pnl = (price - avg_cost) * total_qty
                pos.pnl_pct = (price - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0
            else:
                # 新建持仓
                self._portfolio.positions[stock_code] = Position(
                    stock_code=stock_code,
                    stock_name=stock_code,  # 名称由外部更新
                    quantity=quantity,
                    avg_cost=price,
                    current_price=price,
                    market_value=amount,
                    pnl=0.0,
                    pnl_pct=0.0,
                    open_time=datetime.now().isoformat(),
                )

            self._portfolio.recalculate()
            self._save()

            record = TradeRecord.create(
                stock_code=stock_code,
                action=trade_action,
                quantity=quantity,
                price=price,
                commission=commission,
                reason="模拟买入成功",
                status=TradeStatus.FILLED,
            )
            self._save_trade_record(record)
            logger.info(
                f"买入成交: {stock_code} x{quantity}股 @ {price:.2f}, "
                f"手续费 {commission:.2f}, 总花费 {total_cost:.2f}"
            )
            return record

        else:  # SELL
            commission = self._calculate_sell_commission(amount)

            # 检查持仓
            if stock_code not in self._portfolio.positions:
                logger.warning(f"无法卖出：未持有股票 {stock_code}")
                record = TradeRecord.create(
                    stock_code=stock_code,
                    action=trade_action,
                    quantity=quantity,
                    price=price,
                    commission=0.0,
                    reason="未持有该股票",
                    status=TradeStatus.REJECTED,
                )
                self._save_trade_record(record)
                return record

            pos = self._portfolio.positions[stock_code]
            if pos.quantity < quantity:
                logger.warning(
                    f"持仓不足: {stock_code} 持有 {pos.quantity} 股, "
                    f"尝试卖出 {quantity} 股"
                )
                record = TradeRecord.create(
                    stock_code=stock_code,
                    action=trade_action,
                    quantity=quantity,
                    price=price,
                    commission=0.0,
                    reason="持仓数量不足",
                    status=TradeStatus.REJECTED,
                )
                self._save_trade_record(record)
                return record

            # 执行卖出
            proceeds = amount - commission
            self._portfolio.available_cash += proceeds

            if pos.quantity == quantity:
                del self._portfolio.positions[stock_code]
            else:
                pos.quantity -= quantity
                pos.current_price = price
                pos.market_value = pos.quantity * price
                pos.pnl = (price - pos.avg_cost) * pos.quantity
                pos.pnl_pct = (price - pos.avg_cost) / pos.avg_cost * 100 if pos.avg_cost > 0 else 0

            self._portfolio.recalculate()
            self._save()

            record = TradeRecord.create(
                stock_code=stock_code,
                action=trade_action,
                quantity=quantity,
                price=price,
                commission=commission,
                reason="模拟卖出成功",
                status=TradeStatus.FILLED,
            )
            self._save_trade_record(record)
            logger.info(
                f"卖出成交: {stock_code} x{quantity}股 @ {price:.2f}, "
                f"手续费 {commission:.2f}, 到账 {proceeds:.2f}"
            )
            return record

    def cancel_order(self, order_id: str) -> bool:
        """
        模拟撤单。

        模拟盘所有订单立即成交，无法撤单。
        """
        logger.warning(f"模拟盘不支持撤单（订单已立即成交）: {order_id}")
        return False

    def get_positions(self) -> List[Position]:
        """获取当前所有持仓"""
        return list(self._portfolio.positions.values())

    def get_order_status(self, order_id: str) -> str:
        """
        查询订单状态。

        模拟盘所有订单立即成交，返回 FILLED。
        """
        return TradeStatus.FILLED.value

    def get_trade_records(self, limit: int = 50) -> List[TradeRecord]:
        """获取历史交易记录"""
        try:
            if not self.account_path.exists():
                return []
            with open(self.account_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            records_data = data.get('trade_records', [])[:limit]
            return [TradeRecord.from_dict(r) for r in records_data]
        except Exception as e:
            logger.error(f"获取交易记录失败: {e}")
            return []

    def get_portfolio(self) -> Portfolio:
        """获取完整投资组合状态"""
        self._portfolio.recalculate()
        return self._portfolio

    def reset(self, initial_capital: float = None) -> None:
        """
        重置模拟账户。

        Args:
            initial_capital: 新的初始资金（None 则使用初始化时的值）
        """
        if initial_capital is not None:
            self.initial_capital = initial_capital
        self._portfolio = self._init_account()
        # 清空交易记录
        try:
            data = {
                'portfolio': self._portfolio.to_dict(),
                'trade_records': [],
                'last_updated': datetime.now().isoformat(),
            }
            with open(self.account_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"重置账户失败: {e}")
        logger.info(f"模拟账户已重置，初始资金: {self.initial_capital:.2f}")

    def update_position_name(self, stock_code: str, stock_name: str) -> None:
        """更新持仓中的股票名称"""
        if stock_code in self._portfolio.positions:
            self._portfolio.positions[stock_code].stock_name = stock_name
            self._save()

    def update_position_prices(self, price_map: dict) -> None:
        """
        批量更新持仓价格。

        Args:
            price_map: {stock_code: current_price} 字典
        """
        updated = False
        for code, price in price_map.items():
            if code in self._portfolio.positions:
                self._portfolio.positions[code].update_price(price)
                updated = True
        if updated:
            self._portfolio.recalculate()
            self._save()

    def update_stop_loss(self, stock_code: str, stop_loss_price: float) -> None:
        """更新持仓止损价"""
        if stock_code in self._portfolio.positions:
            self._portfolio.positions[stock_code].stop_loss_price = stop_loss_price
            self._save()
