# -*- coding: utf-8 -*-
"""
===================================
量化下单 Agent 系统
===================================

完整的量化交易流水线，包含：
- 信号聚合（SignalAggregator）：并发分析股票，生成交易信号
- 风险守卫（RiskGuard）：多维度交易前风险检查
- 仓位管理（PortfolioManager）：智能仓位计算与交易执行
- 下单执行（OrderExecutor）：实际调用券商接口
- 模拟盘（PaperBroker）：本地 JSON 持久化的模拟交易

快速开始：
    from quant import QuantOrchestrator
    orchestrator = QuantOrchestrator()
    report = orchestrator.run(["600519", "000858"], dry_run=True)
"""

from quant.orchestrator import QuantOrchestrator

__version__ = "1.0.0"
__all__ = ["QuantOrchestrator"]
