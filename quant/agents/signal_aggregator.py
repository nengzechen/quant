# -*- coding: utf-8 -*-
"""
===================================
量化交易系统 - 信号聚合 Agent
===================================

职责：
1. 接收股票代码列表
2. 并发调用现有 AI 分析框架（AgentExecutor）对每只股票做分析
3. 解析分析结果，提取结构化的 OrderSignal
4. 按情感评分排序，分类返回买卖信号

并发策略：
- 使用 ThreadPoolExecutor 并发分析
- 限制最大并发数（默认3，防止 API 限流）
- 单只股票失败不影响整体流程
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable, List, Optional

from quant.models import ConfidenceLevel, OrderSignal, SignalType

logger = logging.getLogger(__name__)


def _parse_price_str(price_str) -> float:
    """
    解析价格字符串为浮点数。

    处理各种格式：数字字符串、带条件的文字描述（取其中数字）。
    例如："1800.00" -> 1800.0
         "回踩到1750支撑" -> 1750.0
         "1750-1800" -> 1750.0（取第一个）

    Args:
        price_str: 价格字符串或数字

    Returns:
        浮点数价格，解析失败返回 0.0
    """
    if price_str is None:
        return 0.0
    if isinstance(price_str, (int, float)):
        return float(price_str)
    try:
        return float(str(price_str).strip())
    except ValueError:
        # 尝试从文字描述中提取数字
        numbers = re.findall(r'\d+(?:\.\d+)?', str(price_str))
        if numbers:
            return float(numbers[0])
        return 0.0


def _parse_confidence(confidence_str: str, sentiment_score: float) -> ConfidenceLevel:
    """
    解析信心等级。

    Args:
        confidence_str: 原始信心字符串（如 "高/中/低"）
        sentiment_score: 情感评分（0-100，辅助判断）

    Returns:
        ConfidenceLevel 枚举值
    """
    if confidence_str:
        c = str(confidence_str).strip()
        if c in ('高', 'HIGH', 'high', '高置信度'):
            return ConfidenceLevel.HIGH
        if c in ('低', 'LOW', 'low', '低置信度'):
            return ConfidenceLevel.LOW
    # 根据情感评分推断
    if sentiment_score >= 70:
        return ConfidenceLevel.HIGH
    elif sentiment_score >= 50:
        return ConfidenceLevel.MEDIUM
    else:
        return ConfidenceLevel.LOW


class SignalAggregator:
    """
    信号聚合 Agent。

    并发分析多只股票，将 AI 分析结果转化为标准化的 OrderSignal 对象。
    """

    def __init__(self, config=None, max_workers: int = 3):
        """
        初始化信号聚合器。

        Args:
            config: 应用配置（None 时自动加载）
            max_workers: 最大并发分析线程数（默认3，防封禁）
        """
        self._config = config
        self.max_workers = max_workers
        self._executor_cache = None  # 缓存 AgentExecutor 避免重复构建

    def _get_agent_executor(self):
        """
        获取 AgentExecutor 实例（懒加载，带缓存）。

        Returns:
            AgentExecutor 实例
        """
        if self._executor_cache is not None:
            return self._executor_cache

        try:
            from src.agent.factory import build_agent_executor
            config = self._config
            self._executor_cache = build_agent_executor(config=config)
            logger.debug("AgentExecutor 构建成功")
        except Exception as e:
            logger.error(f"构建 AgentExecutor 失败: {e}")
            raise

        return self._executor_cache

    def _analyze_one(self, stock_code: str) -> Optional[OrderSignal]:
        """
        分析单只股票，返回 OrderSignal。

        使用现有的 AgentExecutor 运行完整的四阶段分析流程，
        然后从 dashboard JSON 中提取交易信号。

        Args:
            stock_code: 股票代码

        Returns:
            OrderSignal 对象，分析失败返回 None
        """
        try:
            executor = self._get_agent_executor()
            task = f"分析股票 {stock_code}，生成完整的交易决策报告"
            result = executor.run(
                task=task,
                context={"stock_code": stock_code, "report_type": "simple"},
            )

            if not result.success:
                logger.warning(f"股票 {stock_code} 分析失败: {result.error}")
                return None

            dashboard = result.dashboard
            if not dashboard:
                logger.warning(f"股票 {stock_code} 未返回有效 dashboard")
                return None

            return self._extract_signal(stock_code, dashboard)

        except Exception as e:
            logger.error(f"分析股票 {stock_code} 时发生异常: {e}")
            return None

    def _extract_signal(self, stock_code: str, dashboard: dict) -> Optional[OrderSignal]:
        """
        从 dashboard 字典中提取 OrderSignal。

        对应 AgentExecutor 返回的 dashboard JSON 结构：
        - sentiment_score
        - decision_type: buy/hold/sell
        - confidence_level: 高/中/低
        - stock_name
        - dashboard.battle_plan.sniper_points.ideal_buy
        - dashboard.battle_plan.sniper_points.stop_loss
        - dashboard.battle_plan.sniper_points.take_profit
        - buy_reason

        Args:
            stock_code: 股票代码
            dashboard: AI 分析结果字典

        Returns:
            OrderSignal 对象，解析失败返回 None
        """
        try:
            sentiment_score = float(dashboard.get('sentiment_score', 50))
            decision_type = str(dashboard.get('decision_type', 'hold')).lower()
            confidence_str = dashboard.get('confidence_level', '')
            stock_name = dashboard.get('stock_name', stock_code)
            buy_reason = dashboard.get('buy_reason', dashboard.get('analysis_summary', ''))

            # 解析信号类型
            if decision_type == 'buy':
                signal_type = SignalType.BUY
            elif decision_type == 'sell':
                signal_type = SignalType.SELL
            else:
                signal_type = SignalType.HOLD

            # 提取价格信息（从 battle_plan.sniper_points）
            ideal_buy_price = 0.0
            stop_loss_price = 0.0
            take_profit_price = 0.0

            battle_plan = dashboard.get('dashboard', {}).get('battle_plan', {})
            sniper_points = battle_plan.get('sniper_points', {})

            if sniper_points:
                ideal_buy_price = _parse_price_str(sniper_points.get('ideal_buy', 0))
                stop_loss_price = _parse_price_str(sniper_points.get('stop_loss', 0))
                take_profit_price = _parse_price_str(sniper_points.get('take_profit', 0))

            # 如果 sniper_points 没有价格，尝试从 data_perspective 获取当前价
            if ideal_buy_price <= 0:
                data_perspective = dashboard.get('dashboard', {}).get('data_perspective', {})
                price_position = data_perspective.get('price_position', {})
                current_price = _parse_price_str(price_position.get('current_price', 0))
                if current_price > 0:
                    ideal_buy_price = current_price
                    # 默认止损：当前价 * 95%
                    if stop_loss_price <= 0:
                        stop_loss_price = current_price * 0.95
                    # 默认止盈：当前价 * 110%
                    if take_profit_price <= 0:
                        take_profit_price = current_price * 1.10

            confidence = _parse_confidence(confidence_str, sentiment_score)

            signal = OrderSignal(
                stock_code=stock_code,
                stock_name=stock_name,
                signal_type=signal_type,
                confidence=confidence,
                sentiment_score=sentiment_score,
                ideal_buy_price=ideal_buy_price,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                buy_reason=buy_reason,
                timestamp=datetime.now().isoformat(),
            )

            logger.info(
                f"信号提取成功: {stock_code}({stock_name}) "
                f"信号={signal_type.value} 评分={sentiment_score:.0f} "
                f"买入价={ideal_buy_price:.2f} 止损={stop_loss_price:.2f}"
            )
            return signal

        except Exception as e:
            logger.error(f"从 dashboard 提取信号失败 ({stock_code}): {e}")
            return None

    def aggregate(
        self,
        stock_codes: List[str],
        strategy: str = "auto",
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> List[OrderSignal]:
        """
        并发分析所有股票，聚合并返回排好序的信号列表。

        Args:
            stock_codes: 待分析的股票代码列表
            strategy: 分析策略（目前仅支持 "auto"）
            progress_callback: 进度回调函数 (stock_code, completed, total)

        Returns:
            按 sentiment_score 从高到低排序的 OrderSignal 列表
        """
        if not stock_codes:
            return []

        total = len(stock_codes)
        signals: List[OrderSignal] = []
        completed = 0

        logger.info(f"开始并发分析 {total} 只股票，最大并发数={self.max_workers}")

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_code = {
                executor.submit(self._analyze_one, code): code
                for code in stock_codes
            }

            for future in as_completed(future_to_code):
                code = future_to_code[future]
                completed += 1

                try:
                    signal = future.result()
                    if signal is not None:
                        signals.append(signal)
                        logger.debug(f"[{completed}/{total}] {code} 分析完成")
                    else:
                        logger.warning(f"[{completed}/{total}] {code} 未产生有效信号")
                except Exception as e:
                    logger.error(f"[{completed}/{total}] {code} 分析异常: {e}")

                if progress_callback:
                    try:
                        progress_callback(code, completed, total)
                    except Exception:
                        pass

        # 按情感评分从高到低排序
        signals.sort(key=lambda s: s.sentiment_score, reverse=True)

        buy_signals = [s for s in signals if s.signal_type == SignalType.BUY]
        sell_signals = [s for s in signals if s.signal_type == SignalType.SELL]
        hold_signals = [s for s in signals if s.signal_type == SignalType.HOLD]

        logger.info(
            f"信号聚合完成: 共 {len(signals)} 个有效信号, "
            f"买入={len(buy_signals)}, 卖出={len(sell_signals)}, 持有={len(hold_signals)}"
        )

        return signals

    def get_buy_signals(self, signals: List[OrderSignal]) -> List[OrderSignal]:
        """过滤出买入信号"""
        return [s for s in signals if s.signal_type == SignalType.BUY]

    def get_sell_signals(self, signals: List[OrderSignal]) -> List[OrderSignal]:
        """过滤出卖出信号"""
        return [s for s in signals if s.signal_type == SignalType.SELL]
