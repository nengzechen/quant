# -*- coding: utf-8 -*-
"""
四大模型结果基类 - 继承 StrategyResult 保持通知/报告模块零改动
"""
from dataclasses import dataclass, field
from typing import List
from src.screening.screener import StrategyResult, DimResult


@dataclass
class ModelResult(StrategyResult):
    """
    四大模型的统一结果类。

    继承 StrategyResult，额外字段：
      model_name       : 模型标识（BottomFishing / SwingTrading / StrongTrend / LimitUpHunter）
      phase1_score     : Phase1 离线评分（写入种子池时用）
      phase2_triggered : 是否在 Phase2 触发实时买入信号
      trigger_reason   : Phase2 触发原因
    """
    model_name: str = ""
    phase1_score: int = 0
    phase2_triggered: bool = False
    trigger_reason: str = ""

    def is_seed(self, min_score: int) -> bool:
        """判断是否达到进入种子池的门槛"""
        return self.total_score >= min_score

    def to_seed_dict(self) -> dict:
        """序列化为种子池 JSON 条目"""
        base = self.to_dict()
        base.update({
            "model_name": self.model_name,
            "phase1_score": self.phase1_score,
        })
        return base
