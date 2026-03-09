# -*- coding: utf-8 -*-
"""
量化交易系统 - Agent 模块

包含以下专业化 Agent：
- SignalAggregator: 信号聚合，并发分析股票
- RiskGuard: 风险守卫，交易前安全检查
- PortfolioManager: 仓位管理，处理完整交易流程
- OrderExecutor: 下单执行，最终调用券商接口
"""

from quant.agents.signal_aggregator import SignalAggregator
from quant.agents.risk_guard import RiskGuard
from quant.agents.portfolio_manager import PortfolioManager
from quant.agents.order_executor import OrderExecutor

__all__ = [
    "SignalAggregator",
    "RiskGuard",
    "PortfolioManager",
    "OrderExecutor",
]
