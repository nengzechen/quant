# -*- coding: utf-8 -*-
"""
===================================
量化交易系统 - 主编排器
===================================

将所有 Agent 串联为完整的量化交易流水线：

流程：
    1. 从参数获取股票池
    2. SignalAggregator 并发分析所有股票（AI 驱动）
    3. RiskGuard 过滤不符合风险控制的信号
    4. PortfolioManager 计算仓位并执行下单
    5. 输出交易报告

使用方式：
    from quant import QuantOrchestrator

    # 使用模拟盘（默认）
    orchestrator = QuantOrchestrator()

    # 仅分析，不下单
    report = orchestrator.run(["600519", "000858"], dry_run=True)

    # 实际下单（模拟盘）
    report = orchestrator.run(["600519", "000858"], dry_run=False)

    # 查看账户
    report = orchestrator.get_report()
"""

import logging
from datetime import datetime
from typing import List, Optional

from quant.agents.portfolio_manager import PortfolioManager
from quant.agents.risk_guard import RiskGuard
from quant.agents.signal_aggregator import SignalAggregator
from quant.broker.base import BaseBroker
from quant.broker.paper_broker import PaperBroker
from quant.config import QuantConfig
from quant.models import OrderSignal, SignalType, TradeRecord

logger = logging.getLogger(__name__)


class QuantOrchestrator:
    """
    量化交易流水线编排器。

    整合所有子系统，提供统一的量化交易运行入口。
    支持 dry_run 模式（只分析不下单）和实盘/模拟盘模式。
    """

    def __init__(
        self,
        broker: Optional[BaseBroker] = None,
        config: Optional[QuantConfig] = None,
    ):
        """
        初始化编排器。

        Args:
            broker: 券商接口（None 时根据 config 自动创建模拟盘）
            config: 量化配置（None 时从环境变量加载）
        """
        self._config = config or QuantConfig.from_env()

        # 初始化 Broker（默认模拟盘）
        if broker is not None:
            self._broker = broker
        else:
            self._broker = self._create_broker()

        # 初始化子 Agent
        self._signal_aggregator = SignalAggregator(
            config=None,  # 使用 src.config 的全局配置
            max_workers=self._config.max_signal_workers,
        )

        self._portfolio_manager = PortfolioManager(
            broker=self._broker,
            sizing_method=self._config.sizing_method,
            config=self._config,
        )

        self._risk_guard = self._portfolio_manager.risk_guard

        logger.info(
            f"QuantOrchestrator 初始化完成: "
            f"broker={type(self._broker).__name__}, "
            f"sizing={self._config.sizing_method}, "
            f"max_positions={self._config.max_positions}"
        )

    def _create_broker(self) -> BaseBroker:
        """根据配置创建对应的 Broker 实例"""
        broker_type = self._config.broker_type.lower()

        if broker_type == "futu":
            try:
                from quant.broker.futu_broker import FutuBroker
                return FutuBroker(
                    host=self._config.futu_host,
                    port=self._config.futu_port,
                    trade_env=self._config.futu_trade_env,
                    max_positions=self._config.max_positions,
                    risk_per_trade_pct=self._config.risk_per_trade_pct,
                )
            except Exception as e:
                logger.warning(f"FutuBroker 初始化失败，降级到模拟盘: {e}")
                broker_type = "paper"

        # 默认模拟盘
        return PaperBroker(
            account_path=self._config.paper_account_path,
            initial_capital=self._config.initial_capital,
            max_positions=self._config.max_positions,
            risk_per_trade_pct=self._config.risk_per_trade_pct,
        )

    def run(
        self,
        stock_codes: List[str],
        dry_run: bool = False,
    ) -> dict:
        """
        完整运行一次量化流程，返回运行报告。

        Args:
            stock_codes: 待分析的股票代码列表
            dry_run: True 时只分析信号，不实际下单

        Returns:
            运行报告字典，包含：
            - signals: 所有分析信号
            - executed_trades: 实际执行的交易记录
            - portfolio_summary: 账户状态摘要
            - timestamp: 运行时间
        """
        start_time = datetime.now()
        logger.info(
            f"{'[DRY RUN] ' if dry_run else ''}开始量化流程: "
            f"{len(stock_codes)} 只股票 {stock_codes}"
        )

        report = {
            "timestamp": start_time.isoformat(),
            "dry_run": dry_run,
            "stock_codes": stock_codes,
            "signals": [],
            "buy_signals": [],
            "sell_signals": [],
            "executed_trades": [],
            "portfolio_summary": {},
            "errors": [],
        }

        # ===== Step 1: 信号聚合 =====
        try:
            logger.info("Step 1: 开始信号聚合...")

            def progress_cb(code, done, total):
                logger.info(f"  分析进度 [{done}/{total}]: {code}")

            signals = self._signal_aggregator.aggregate(
                stock_codes=stock_codes,
                progress_callback=progress_cb,
            )

            report["signals"] = [s.to_dict() for s in signals]
            buy_signals = [s for s in signals if s.signal_type == SignalType.BUY]
            sell_signals = [s for s in signals if s.signal_type == SignalType.SELL]
            report["buy_signals"] = [s.to_dict() for s in buy_signals]
            report["sell_signals"] = [s.to_dict() for s in sell_signals]

            logger.info(
                f"信号聚合完成: 总信号={len(signals)}, "
                f"买入={len(buy_signals)}, 卖出={len(sell_signals)}"
            )

        except Exception as e:
            error_msg = f"信号聚合失败: {e}"
            logger.error(error_msg)
            report["errors"].append(error_msg)
            report["portfolio_summary"] = self._portfolio_manager.get_portfolio_summary()
            return report

        # ===== Step 2: 止损检查（先于新买入） =====
        try:
            if not dry_run:
                logger.info("Step 2: 执行止损检查...")
                stop_loss_records = self._portfolio_manager.check_and_stop_loss()
                if stop_loss_records:
                    logger.warning(f"触发止损 {len(stop_loss_records)} 笔")
                    report["executed_trades"].extend(
                        [r.to_dict() for r in stop_loss_records]
                    )
        except Exception as e:
            logger.error(f"止损检查失败: {e}")
            report["errors"].append(f"止损检查失败: {e}")

        # ===== Step 3: 处理卖出信号 =====
        if not dry_run:
            try:
                logger.info(f"Step 3: 处理 {len(sell_signals)} 个卖出信号...")
                for signal in sell_signals:
                    try:
                        record = self._portfolio_manager.process_signal(signal)
                        if record is not None:
                            report["executed_trades"].append(record.to_dict())
                    except Exception as e:
                        logger.error(f"处理卖出信号失败 {signal.stock_code}: {e}")
            except Exception as e:
                logger.error(f"卖出处理异常: {e}")
                report["errors"].append(f"卖出处理异常: {e}")

        # ===== Step 4: 处理买入信号 =====
        if not dry_run:
            try:
                logger.info(f"Step 4: 处理 {len(buy_signals)} 个买入信号...")
                for signal in buy_signals:
                    try:
                        record = self._portfolio_manager.process_signal(signal)
                        if record is not None:
                            report["executed_trades"].append(record.to_dict())
                    except Exception as e:
                        logger.error(f"处理买入信号失败 {signal.stock_code}: {e}")
            except Exception as e:
                logger.error(f"买入处理异常: {e}")
                report["errors"].append(f"买入处理异常: {e}")

        # ===== Step 5: 生成报告 =====
        try:
            report["portfolio_summary"] = self._portfolio_manager.get_portfolio_summary()
        except Exception as e:
            logger.error(f"生成组合摘要失败: {e}")
            report["errors"].append(f"生成组合摘要失败: {e}")

        elapsed = (datetime.now() - start_time).total_seconds()
        report["elapsed_seconds"] = round(elapsed, 1)

        logger.info(
            f"量化流程完成: 耗时 {elapsed:.1f}s, "
            f"信号={len(signals)}, "
            f"交易={len(report['executed_trades'])}, "
            f"错误={len(report['errors'])}"
        )

        return report

    def run_stop_loss_check(self) -> List[TradeRecord]:
        """
        独立运行止损检查（可用于定时任务）。

        Returns:
            止损产生的交易记录列表
        """
        logger.info("运行止损检查...")
        try:
            return self._portfolio_manager.check_and_stop_loss()
        except Exception as e:
            logger.error(f"止损检查失败: {e}")
            return []

    def get_report(self) -> dict:
        """
        获取当前账户状态报告。

        Returns:
            账户状态摘要字典
        """
        try:
            account_info = self._broker.get_account_info()
            positions = self._broker.get_positions()
            trade_records = self._broker.get_trade_records(limit=10)

            return {
                "timestamp": datetime.now().isoformat(),
                "account": account_info,
                "positions": [
                    {
                        "stock_code": p.stock_code,
                        "stock_name": p.stock_name,
                        "quantity": p.quantity,
                        "avg_cost": round(p.avg_cost, 3),
                        "current_price": round(p.current_price, 3),
                        "market_value": round(p.market_value, 2),
                        "pnl": round(p.pnl, 2),
                        "pnl_pct": round(p.pnl_pct, 2),
                        "stop_loss_price": p.stop_loss_price,
                    }
                    for p in positions
                ],
                "recent_trades": [r.to_dict() for r in trade_records],
                "broker_type": type(self._broker).__name__,
                "config": {
                    "sizing_method": self._config.sizing_method,
                    "max_positions": self._config.max_positions,
                    "risk_per_trade_pct": self._config.risk_per_trade_pct,
                },
            }
        except Exception as e:
            logger.error(f"获取报告失败: {e}")
            return {"error": str(e), "timestamp": datetime.now().isoformat()}

    @property
    def broker(self) -> BaseBroker:
        """返回当前 Broker 实例"""
        return self._broker

    @property
    def config(self) -> QuantConfig:
        """返回当前配置"""
        return self._config
