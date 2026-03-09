# -*- coding: utf-8 -*-
"""
===================================
量化交易系统 - 仓位计算策略
===================================

提供三种仓位计算方法：
1. 固定分数法（Fixed Fraction）：每次用总资金的固定比例买入
2. Kelly公式（Kelly Criterion）：根据胜率和盈亏比计算最优仓位
3. ATR风险定量（ATR-based）：根据止损距离和风险预算计算仓位

所有方法返回建议购买的手数（1手 = 100股）。
"""

import logging
import math
from typing import Optional

from quant.models import OrderSignal, Portfolio

logger = logging.getLogger(__name__)

# 每手股数（A股标准）
SHARES_PER_LOT = 100


def fixed_fraction(
    portfolio: Portfolio,
    signal: OrderSignal,
    fraction: float = 0.10,
) -> int:
    """
    固定分数法仓位计算。

    策略：每次用总资金的 fraction 买入，无论信号强弱。
    特点：简单、稳定，适合新手和保守型投资者。

    Args:
        portfolio: 当前投资组合
        signal: 交易信号（含买入价格）
        fraction: 每次投入的资金比例（默认 10%）

    Returns:
        建议购买手数（整数，最小为 0）

    公式：
        资金 = total_capital * fraction
        手数 = floor(资金 / (price * 100))
    """
    if signal.ideal_buy_price <= 0:
        logger.warning(f"股票 {signal.stock_code} 理想买入价无效: {signal.ideal_buy_price}")
        return 0

    target_amount = portfolio.total_capital * fraction
    # 不超过可用现金的 95%
    max_amount = portfolio.available_cash * 0.95

    invest_amount = min(target_amount, max_amount)

    if invest_amount <= 0:
        return 0

    lots = int(invest_amount / (signal.ideal_buy_price * SHARES_PER_LOT))
    lots = max(0, lots)

    logger.debug(
        f"固定分数法: {signal.stock_code}, 目标金额={invest_amount:.0f}, "
        f"价格={signal.ideal_buy_price:.2f}, 建议手数={lots}"
    )
    return lots


def kelly_criterion(
    portfolio: Portfolio,
    signal: OrderSignal,
    win_rate: float = 0.55,
    win_loss_ratio: float = 1.5,
    half_kelly: bool = True,
) -> int:
    """
    Kelly公式仓位计算。

    策略：根据历史胜率和盈亏比计算理论最优仓位，使用半Kelly保守系数。
    特点：在长期收益最大化的同时控制回撤风险。

    Args:
        portfolio: 当前投资组合
        signal: 交易信号（含买入价格）
        win_rate: 预期胜率（0-1，默认 0.55）
        win_loss_ratio: 盈亏比（默认 1.5，即赢时赚1.5倍止损额）
        half_kelly: 是否使用半Kelly（默认True，更保守）

    Returns:
        建议购买手数（整数，最小为 0）

    公式：
        f* = (p * b - q) / b
        其中 p=胜率, q=1-p, b=盈亏比
        半Kelly: f = f* / 2
    """
    if signal.ideal_buy_price <= 0:
        logger.warning(f"股票 {signal.stock_code} 理想买入价无效: {signal.ideal_buy_price}")
        return 0

    p = win_rate
    q = 1.0 - p
    b = win_loss_ratio

    # Kelly公式
    kelly_fraction = (p * b - q) / b

    if kelly_fraction <= 0:
        logger.debug(f"Kelly公式结果为负（胜率不足），建议不入场: {signal.stock_code}")
        return 0

    # 半Kelly保守系数
    if half_kelly:
        kelly_fraction /= 2.0

    # Kelly仓位不超过 30%（防止过于激进）
    kelly_fraction = min(kelly_fraction, 0.30)

    target_amount = portfolio.total_capital * kelly_fraction
    max_amount = portfolio.available_cash * 0.95
    invest_amount = min(target_amount, max_amount)

    if invest_amount <= 0:
        return 0

    lots = int(invest_amount / (signal.ideal_buy_price * SHARES_PER_LOT))
    lots = max(0, lots)

    logger.debug(
        f"Kelly公式: {signal.stock_code}, "
        f"f*={kelly_fraction:.3f}(半Kelly), "
        f"目标金额={invest_amount:.0f}, 建议手数={lots}"
    )
    return lots


def atr_based(
    portfolio: Portfolio,
    signal: OrderSignal,
    atr: Optional[float] = None,
    risk_pct: float = 0.02,
) -> int:
    """
    ATR风险定量仓位计算。

    策略：每笔交易最多亏损总资金的 risk_pct，根据止损距离反推仓位。
    特点：严格风险控制，每笔交易损失可预期。

    Args:
        portfolio: 当前投资组合
        signal: 交易信号（含买入价和止损价）
        atr: 平均真实波幅（可选，如不提供则用买入价和止损价之差作为风险单位）
        risk_pct: 单笔最大亏损占总资金比例（默认 2%）

    Returns:
        建议购买手数（整数，最小为 0）

    公式：
        风险金额 = total_capital * risk_pct
        止损距离 = buy_price - stop_loss_price（或 ATR）
        股数 = floor(风险金额 / 止损距离)
        手数 = floor(股数 / 100)
    """
    if signal.ideal_buy_price <= 0:
        logger.warning(f"股票 {signal.stock_code} 理想买入价无效: {signal.ideal_buy_price}")
        return 0

    # 计算止损距离
    if atr is not None and atr > 0:
        risk_per_share = atr  # 用ATR作为风险单位（通常是1-2倍ATR的止损）
    elif signal.stop_loss_price > 0:
        risk_per_share = signal.ideal_buy_price - signal.stop_loss_price
    else:
        # 默认使用买入价的 5% 作为止损距离
        risk_per_share = signal.ideal_buy_price * 0.05

    if risk_per_share <= 0:
        logger.warning(
            f"止损距离为零或负数，无法计算ATR仓位: "
            f"{signal.stock_code} 买入={signal.ideal_buy_price:.2f} 止损={signal.stop_loss_price:.2f}"
        )
        # 降级到固定分数法
        return fixed_fraction(portfolio, signal, fraction=0.05)

    # 每笔交易允许的最大亏损金额
    max_risk_amount = portfolio.total_capital * risk_pct
    max_cash_amount = portfolio.available_cash * 0.95

    # 根据风险金额反推可买股数
    max_shares_by_risk = int(max_risk_amount / risk_per_share)
    max_shares_by_cash = int(max_cash_amount / signal.ideal_buy_price)

    shares = min(max_shares_by_risk, max_shares_by_cash)
    lots = int(shares / SHARES_PER_LOT)
    lots = max(0, lots)

    logger.debug(
        f"ATR风险定量: {signal.stock_code}, "
        f"风险金额={max_risk_amount:.0f}, "
        f"止损距离={risk_per_share:.2f}, "
        f"可买股数={shares}(风险限制)/{max_shares_by_cash}(资金限制), "
        f"建议手数={lots}"
    )
    return lots


def calculate_position_size(
    portfolio: Portfolio,
    signal: OrderSignal,
    method: str = "atr_based",
    **kwargs,
) -> int:
    """
    统一仓位计算接口。

    根据 method 参数选择计算策略，并处理所有异常情况。

    Args:
        portfolio: 当前投资组合
        signal: 交易信号
        method: 计算方法 "fixed_fraction" / "kelly" / "atr_based"
        **kwargs: 传递给具体策略的参数

    Returns:
        建议购买手数（整数，最小为 0）
    """
    try:
        if method == "fixed_fraction":
            return fixed_fraction(portfolio, signal, **kwargs)
        elif method == "kelly":
            return kelly_criterion(portfolio, signal, **kwargs)
        elif method == "atr_based":
            return atr_based(portfolio, signal, **kwargs)
        else:
            logger.warning(f"未知仓位计算方法 '{method}'，使用固定分数法")
            return fixed_fraction(portfolio, signal)
    except Exception as e:
        logger.error(f"仓位计算失败 ({method}): {e}")
        return 0
