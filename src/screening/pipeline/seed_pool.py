# -*- coding: utf-8 -*-
"""
种子池数据模型与持久化

Phase1 写入，Phase2 读取并追加实时信号结果。
文件路径：data/seed_pool_YYYYMMDD.json
"""
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")


@dataclass
class SeedEntry:
    """
    种子池单条记录。

    Phase1 生成时填充 code~dim_details。
    Phase2 触发时更新 phase2_* 字段。
    """
    code: str
    name: str
    model: str                            # BottomFishing / SwingTrading / StrongTrend / LimitUpHunter
    phase1_score: int
    max_score: int
    passed_dims: List[str] = field(default_factory=list)
    failed_dims: List[str] = field(default_factory=list)
    dim_details: Dict[str, str] = field(default_factory=dict)
    created_at: str = ""
    phase2_triggered: bool = False
    phase2_trigger_time: str = ""
    phase2_reason: str = ""

    @classmethod
    def from_model_result(cls, result) -> "SeedEntry":
        """从 ModelResult 构造种子条目"""
        return cls(
            code=result.code,
            name=result.name,
            model=result.model_name,
            phase1_score=result.total_score,
            max_score=result.max_score,
            passed_dims=result.passed_dims,
            failed_dims=result.failed_dims,
            dim_details={d.name: d.detail for d in result.dims if d.passed is True and d.detail},
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "model": self.model,
            "phase1_score": self.phase1_score,
            "max_score": self.max_score,
            "passed_dims": self.passed_dims,
            "failed_dims": self.failed_dims,
            "dim_details": self.dim_details,
            "created_at": self.created_at,
            "phase2_triggered": self.phase2_triggered,
            "phase2_trigger_time": self.phase2_trigger_time,
            "phase2_reason": self.phase2_reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SeedEntry":
        return cls(
            code=d.get("code", ""),
            name=d.get("name", ""),
            model=d.get("model", ""),
            phase1_score=d.get("phase1_score", 0),
            max_score=d.get("max_score", 0),
            passed_dims=d.get("passed_dims", []),
            failed_dims=d.get("failed_dims", []),
            dim_details=d.get("dim_details", {}),
            created_at=d.get("created_at", ""),
            phase2_triggered=d.get("phase2_triggered", False),
            phase2_trigger_time=d.get("phase2_trigger_time", ""),
            phase2_reason=d.get("phase2_reason", ""),
        )


def get_seed_pool_path(date_str: Optional[str] = None) -> str:
    """获取种子池文件路径：data/seed_pool_YYYYMMDD.json"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")
    return os.path.join(_DATA_DIR, f"seed_pool_{date_str}.json")


def save_seed_pool(entries: List[SeedEntry], date_str: Optional[str] = None) -> str:
    """保存种子池到 JSON，返回保存路径"""
    os.makedirs(_DATA_DIR, exist_ok=True)
    path = get_seed_pool_path(date_str)
    data = {
        "date": date_str or datetime.now().strftime("%Y-%m-%d"),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(entries),
        "entries": [e.to_dict() for e in entries],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load_seed_pool(date_str: Optional[str] = None) -> List[SeedEntry]:
    """读取种子池 JSON，找不到文件返回空列表"""
    path = get_seed_pool_path(date_str)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [SeedEntry.from_dict(e) for e in data.get("entries", [])]
