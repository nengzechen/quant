# -*- coding: utf-8 -*-
"""
src/screening — 量化选股引擎
===========================

对外暴露的主要接口：
    from src.screening import Strategy1, Strategy2
    from src.screening import run_strategy1_batch, run_strategy2_batch
    from src.screening.indicators import get_daily_df, check_ma_bull, ...
"""

from src.screening.screener import (
    Strategy1,
    Strategy2,
    StrategyResult,
    DimResult,
    run_strategy1_batch,
    run_strategy2_batch,
)

__all__ = [
    "Strategy1",
    "Strategy2",
    "StrategyResult",
    "DimResult",
    "run_strategy1_batch",
    "run_strategy2_batch",
]
