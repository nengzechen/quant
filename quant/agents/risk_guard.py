# -*- coding: utf-8 -*-
"""
===================================
量化交易系统 - 风险守卫 Agent
===================================

职责：
1. 交易前多维度安全检查
2. 持仓止损 / 止盈监控
3. 防止过度集中和高风险操作

检查项目：
1. 资金充足性检查
2. 单股集中度检查（最大30%）
3. 持仓数量上限检查
4. 单笔风险检查（最大 risk_per_trade_pct）
5. 情感评分检查（评分过低阻止买入）
6. 黑名单股票检查
7. 卖出 T+1 检查（当日买入股份不可卖出）
8. 止损价有效性检查（止损价不低于买入价的买入信号视为无效）
9. 移动止损（持仓浮盈后从最高价回撤 trailing_stop_pct 即止损）
"""

import logging
from typing import List, Optional, Tuple

from quant.models import OrderSignal, Portfolio, Position, SignalType

logger = logging.getLogger(__name__)

# 风险控制参数
MAX_SINGLE_STOCK_RATIO = 0.30   # 单只股票最大仓位比例（30%）
MIN_BUY_SENTIMENT = 40          # 最低买入情感评分阈值
CASH_BUFFER_RATIO = 0.95        # 可用现金缓冲比（留5%备用）


class RiskGuard:
    """
    风险守卫 Agent。

    在每次交易前运行多项风险检查，确保每笔交易符合风控规则。
    """

    def __init__(
        self,
        blacklist: Optional[List[str]] = None,
        config=None,
        max_single_stock_ratio: float = MAX_SINGLE_STOCK_RATIO,
        min_buy_sentiment: float = MIN_BUY_SENTIMENT,
        trailing_stop_pct: Optional[float] = None,
    ):
        """
        初始化风险守卫。

        Args:
            blacklist: 黑名单股票代码列表（大写）
            config: 量化配置（可选）
            max_single_stock_ratio: 单只股票最大仓位比例
            min_buy_sentiment: 最低买入情感评分（低于此值拒绝买入）
            trailing_stop_pct: 移动止损回撤比例（None 时取 config.trailing_stop_pct，0 关闭）
        """
        self.blacklist = [c.upper() for c in (blacklist or [])]
        self._config = config
        self.max_single_stock_ratio = max_single_stock_ratio
        self.min_buy_sentiment = min_buy_sentiment
        if trailing_stop_pct is None:
            trailing_stop_pct = getattr(config, 'trailing_stop_pct', 0.0) or 0.0
        if not 0 <= trailing_stop_pct < 1:
            logger.warning(f"移动止损比例无效 {trailing_stop_pct}，已关闭移动止损")
            trailing_stop_pct = 0.0
        self.trailing_stop_pct = trailing_stop_pct

    def check_buy(
        self,
        portfolio: Portfolio,
        signal: OrderSignal,
        quantity: int,
        price: float,
    ) -> Tuple[bool, str]:
        """
        检查买入请求。

        依次执行所有风险检查，任何一项失败即拒绝。

        Args:
            portfolio: 当前投资组合
            signal: 交易信号
            quantity: 买入数量（股）
            price: 买入价格

        Returns:
            (passed, reason): 是否通过, 原因说明
        """
        if quantity <= 0:
            return False, "买入数量必须大于 0"

        if price <= 0:
            return False, "买入价格必须大于 0"

        # 检查1: 黑名单
        passed, reason = self._check_blacklist(signal.stock_code)
        if not passed:
            return False, reason

        # 检查1.5: 止损价有效性（止损价 >= 买入价时成交后会立刻触发止损）
        passed, reason = self._check_stop_loss_price(signal, price)
        if not passed:
            return False, reason

        # 检查2: 情感评分（乖离率检查）
        passed, reason = self._check_sentiment(signal)
        if not passed:
            return False, reason

        # 检查3: 可用资金
        passed, reason = self._check_cash(portfolio, quantity, price)
        if not passed:
            return False, reason

        # 检查4: 单股集中度
        passed, reason = self._check_concentration(portfolio, signal.stock_code, quantity, price)
        if not passed:
            return False, reason

        # 检查5: 持仓数量上限
        passed, reason = self._check_position_count(portfolio, signal.stock_code)
        if not passed:
            return False, reason

        # 检查6: 单笔风险
        passed, reason = self._check_single_trade_risk(portfolio, signal, quantity, price)
        if not passed:
            return False, reason

        logger.info(
            f"风险检查通过: {signal.stock_code} 买入 {quantity}股 @ {price:.2f}"
        )
        return True, "风险检查通过"

    def max_buy_quantity(
        self,
        portfolio: Portfolio,
        signal: OrderSignal,
        price: float,
        lot_size: int = 100,
    ) -> int:
        """
        在资金、单股集中度、单笔风险三项限额内最多可买的股数（按整手向下取整）。

        供仓位管理在下单前把建议数量收敛到限额内，避免整单被 check_buy 拒绝。
        与 _check_cash / _check_concentration / _check_single_trade_risk 口径一致。

        Args:
            portfolio: 当前投资组合
            signal: 交易信号（用于股票代码和止损价）
            price: 买入价格
            lot_size: 每手股数

        Returns:
            可买股数（lot_size 的整数倍，最小为 0）
        """
        if price <= 0 or lot_size <= 0:
            return 0

        # 资金：price*q + max(price*q*0.0003, 5) <= available * buffer
        available = portfolio.available_cash * CASH_BUFFER_RATIO
        limits = [
            (available - 5.0) / price,
            available / (price * 1.0003),
        ]

        # 集中度：existing + price*q <= total_assets * ratio
        total_assets = portfolio.total_assets
        if total_assets > 0:
            pos = portfolio.positions.get(signal.stock_code.upper())
            existing_value = pos.market_value if pos else 0.0
            limits.append(
                (total_assets * self.max_single_stock_ratio - existing_value) / price
            )

        # 单笔风险：(price - stop) * q <= total_capital * risk_per_trade_pct
        risk_per_share = price - signal.stop_loss_price
        if signal.stop_loss_price > 0 and risk_per_share > 0:
            limits.append(
                portfolio.total_capital * portfolio.risk_per_trade_pct / risk_per_share
            )

        max_shares = int(min(limits))
        return max(0, max_shares // lot_size * lot_size)

    def check_sell(
        self,
        portfolio: Portfolio,
        stock_code: str,
        quantity: int,
        price: float,
    ) -> Tuple[bool, str]:
        """
        检查卖出请求。

        卖出检查比买入宽松，主要检查持仓是否充足。

        Args:
            portfolio: 当前投资组合
            stock_code: 股票代码
            quantity: 卖出数量（股）
            price: 卖出价格

        Returns:
            (passed, reason): 是否通过, 原因说明
        """
        if quantity <= 0:
            return False, "卖出数量必须大于 0"

        if price <= 0:
            return False, "卖出价格必须大于 0"

        # 检查持仓
        code_upper = stock_code.upper()
        if code_upper not in portfolio.positions:
            return False, f"未持有股票 {stock_code}，无法卖出"

        pos = portfolio.positions[code_upper]
        if pos.quantity < quantity:
            return False, (
                f"持仓不足: {stock_code} 持有 {pos.quantity} 股, "
                f"尝试卖出 {quantity} 股"
            )

        sellable = pos.sellable_quantity()
        if quantity > sellable:
            return False, (
                f"T+1 限制: {stock_code} 今日买入 {pos.quantity - sellable} 股不可卖出, "
                f"可卖 {sellable} 股, 尝试卖出 {quantity} 股"
            )

        logger.info(f"卖出风险检查通过: {stock_code} 卖出 {quantity}股 @ {price:.2f}")
        return True, "卖出检查通过"

    def effective_stop_price(self, pos: Position) -> float:
        """
        持仓的有效止损价：固定止损价与移动止损价取较高者，0 表示无止损。

        移动止损价 = 持仓最高价 * (1 - trailing_stop_pct)，只在最高价超过成本价
        （持仓曾浮盈）后生效：未盈利阶段仍由信号止损价控制初始风险，盈利后止损
        跟随最高价上移、只升不降，锁住大部分浮盈。
        """
        stop = pos.stop_loss_price if pos.stop_loss_price and pos.stop_loss_price > 0 else 0.0
        if (
            self.trailing_stop_pct > 0
            and pos.highest_price
            and pos.avg_cost > 0
            and pos.highest_price > pos.avg_cost
        ):
            stop = max(stop, pos.highest_price * (1 - self.trailing_stop_pct))
        return stop

    def check_stop_loss(self, portfolio: Portfolio) -> List[str]:
        """
        检查所有持仓是否触发止损（含移动止损）。

        当前价格跌破有效止损价时，返回对应股票代码。

        Args:
            portfolio: 当前投资组合

        Returns:
            需要止损的股票代码列表
        """
        stop_loss_codes = []

        for code, pos in portfolio.positions.items():
            stop = self.effective_stop_price(pos)
            if stop > 0 and pos.current_price <= stop:
                logger.warning(
                    f"止损触发: {code} 当前价 {pos.current_price:.2f} "
                    f"跌破止损价 {stop:.2f}"
                )
                stop_loss_codes.append(code)

        return stop_loss_codes

    def check_take_profit(self, portfolio: Portfolio) -> List[str]:
        """
        检查所有持仓是否触发止盈。

        当前价格达到或超过止盈价时，返回对应股票代码。

        Args:
            portfolio: 当前投资组合

        Returns:
            需要止盈的股票代码列表
        """
        take_profit_codes = []

        for code, pos in portfolio.positions.items():
            if pos.take_profit_price and pos.take_profit_price > 0:
                if pos.current_price >= pos.take_profit_price:
                    logger.info(
                        f"止盈触发: {code} 当前价 {pos.current_price:.2f} "
                        f"达到止盈价 {pos.take_profit_price:.2f}"
                    )
                    take_profit_codes.append(code)

        return take_profit_codes

    # ===== 内部检查方法 =====

    def _check_blacklist(self, stock_code: str) -> Tuple[bool, str]:
        """检查黑名单"""
        if stock_code.upper() in self.blacklist:
            return False, f"股票 {stock_code} 在黑名单中，禁止交易"
        return True, ""

    def _check_stop_loss_price(
        self,
        signal: OrderSignal,
        price: float,
    ) -> Tuple[bool, str]:
        """
        检查止损价是否位于买入价下方。

        止损价来自 LLM 解析，可能倒挂（如解析错位、买入价回退为现价后高于原止损）。
        倒挂时单笔风险检查会被跳过、成交后止损价写入持仓，下次止损检查必然触发，
        等于买入即止损，因此直接拒绝。止损价为 0 表示未知，不在此拦截。
        """
        if signal.stop_loss_price > 0 and signal.stop_loss_price >= price:
            return False, (
                f"止损价无效: {signal.stock_code} 止损 {signal.stop_loss_price:.2f} "
                f"不低于买入价 {price:.2f}，拒绝买入"
            )
        return True, ""

    def _check_sentiment(self, signal: OrderSignal) -> Tuple[bool, str]:
        """检查情感评分（乖离率检查）"""
        if signal.signal_type == SignalType.BUY and signal.sentiment_score < self.min_buy_sentiment:
            return False, (
                f"情感评分过低 ({signal.sentiment_score:.0f} < {self.min_buy_sentiment})，"
                f"拒绝买入 {signal.stock_code}"
            )
        return True, ""

    def _check_cash(
        self,
        portfolio: Portfolio,
        quantity: int,
        price: float,
    ) -> Tuple[bool, str]:
        """检查可用现金是否充足（包含手续费缓冲）"""
        required = price * quantity
        # 预估手续费：万分之三 + 最低5元
        estimated_commission = max(required * 0.0003, 5.0)
        total_required = required + estimated_commission

        available = portfolio.available_cash * CASH_BUFFER_RATIO

        if total_required > available:
            return False, (
                f"可用现金不足: 需要 {total_required:.2f}（含预估手续费）, "
                f"可用 {available:.2f}"
            )
        return True, ""

    def _check_concentration(
        self,
        portfolio: Portfolio,
        stock_code: str,
        quantity: int,
        price: float,
    ) -> Tuple[bool, str]:
        """检查单只股票持仓集中度（不超过总资金30%）"""
        total_assets = portfolio.total_assets
        if total_assets <= 0:
            return True, ""

        # 现有持仓市值
        existing_value = 0.0
        code_upper = stock_code.upper()
        if code_upper in portfolio.positions:
            existing_value = portfolio.positions[code_upper].market_value

        # 新增买入金额
        new_value = existing_value + price * quantity
        ratio = new_value / total_assets

        if ratio > self.max_single_stock_ratio:
            return False, (
                f"单股集中度过高: {stock_code} 预计占总资产 {ratio*100:.1f}% "
                f"(上限 {self.max_single_stock_ratio*100:.0f}%)"
            )
        return True, ""

    def _check_position_count(
        self,
        portfolio: Portfolio,
        stock_code: str,
    ) -> Tuple[bool, str]:
        """检查持仓数量是否超过上限"""
        code_upper = stock_code.upper()
        current_count = len(portfolio.positions)

        # 如果已持有该股票，不算新增持仓
        if code_upper in portfolio.positions:
            return True, ""

        if current_count >= portfolio.max_positions:
            return False, (
                f"持仓数量已达上限 ({current_count}/{portfolio.max_positions})，"
                f"无法新增持仓"
            )
        return True, ""

    def _check_single_trade_risk(
        self,
        portfolio: Portfolio,
        signal: OrderSignal,
        quantity: int,
        price: float,
    ) -> Tuple[bool, str]:
        """
        检查单笔交易风险（基于止损价计算最大潜在亏损）。

        如果止损价已知，验证最大亏损不超过 risk_per_trade_pct。
        """
        if signal.stop_loss_price <= 0:
            # 无止损价信息，跳过此检查
            return True, ""

        risk_per_share = price - signal.stop_loss_price
        if risk_per_share <= 0:
            return True, ""

        max_risk_amount = risk_per_share * quantity
        max_allowed_risk = portfolio.total_capital * portfolio.risk_per_trade_pct

        if max_risk_amount > max_allowed_risk:
            return False, (
                f"单笔风险过高: 最大亏损 {max_risk_amount:.2f} "
                f"超过限额 {max_allowed_risk:.2f} "
                f"({portfolio.risk_per_trade_pct*100:.1f}% 总资金)"
            )
        return True, ""

    def add_to_blacklist(self, stock_code: str) -> None:
        """将股票加入黑名单"""
        code = stock_code.upper()
        if code not in self.blacklist:
            self.blacklist.append(code)
            logger.info(f"已将 {code} 加入黑名单")

    def remove_from_blacklist(self, stock_code: str) -> None:
        """将股票从黑名单移除"""
        code = stock_code.upper()
        if code in self.blacklist:
            self.blacklist.remove(code)
            logger.info(f"已将 {code} 从黑名单移除")
