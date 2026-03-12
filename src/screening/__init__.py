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
from src.screening.notify import run_and_notify_screening

# V2：四大模型
from src.screening.models import (
    ModelResult,
    BottomFishing,
    SwingTrading,
    StrongTrend,
    LimitUpHunter,
)

# V2：两阶段流水线
from src.screening.pipeline import (
    run_phase1,
    run_phase2,
    run_phase2_once,
    SeedEntry,
)

__all__ = [
    # V1 原有（保持兼容）
    "Strategy1",
    "Strategy2",
    "StrategyResult",
    "DimResult",
    "run_strategy1_batch",
    "run_strategy2_batch",
    "run_and_notify_screening",
    # V2 新增
    "ModelResult",
    "BottomFishing",
    "SwingTrading",
    "StrongTrend",
    "LimitUpHunter",
    "run_phase1",
    "run_phase2",
    "run_phase2_once",
    "SeedEntry",
]
