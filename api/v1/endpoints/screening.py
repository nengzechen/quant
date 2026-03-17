# -*- coding: utf-8 -*-
"""
选股流水线接口
GET /api/v1/screening/seed-pool  - 今日种子池（Phase1 + Phase2 触发状态）
GET /api/v1/screening/dates      - 可查询的历史日期列表
"""

import glob
import json
import logging
import os
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)
router = APIRouter()

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))
_DATA_DIR = os.path.join(_ROOT, "data")


def _find_pool_file(date_str: str | None) -> str | None:
    """返回指定日期（或最新）的种子池文件路径"""
    if date_str:
        p = os.path.join(_DATA_DIR, f"seed_pool_{date_str}.json")
        return p if os.path.exists(p) else None
    files = sorted(glob.glob(os.path.join(_DATA_DIR, "seed_pool_*.json")))
    return files[-1] if files else None


@router.get("/seed-pool")
def get_seed_pool(date: str | None = Query(None, description="YYYYMMDD，不传则取最新")):
    """返回种子池数据，含 Phase1 评分和 Phase2 触发状态"""
    path = _find_pool_file(date)
    if not path:
        return {"date": date or "—", "count": 0, "entries": [], "triggered_count": 0}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        entries = data.get("entries", [])
        triggered_count = sum(1 for e in entries if e.get("phase2_triggered"))

        return {
            "date": data.get("date", ""),
            "created_at": data.get("created_at", ""),
            "count": len(entries),
            "triggered_count": triggered_count,
            "entries": entries,
        }
    except Exception as e:
        logger.error(f"读取种子池失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/dates")
def get_available_dates():
    """返回所有有种子池文件的日期列表"""
    files = sorted(glob.glob(os.path.join(_DATA_DIR, "seed_pool_*.json")), reverse=True)
    dates = []
    for f in files:
        name = os.path.basename(f)  # seed_pool_20260316.json
        d = name.replace("seed_pool_", "").replace(".json", "")
        dates.append(d)
    return {"dates": dates}
