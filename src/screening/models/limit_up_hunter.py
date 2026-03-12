# -*- coding: utf-8 -*-
"""
模型四：强势涨停 (LimitUpHunter)

核心逻辑：针对极强个股的接力或突破。

维度（共 7 维）：
  A. 核心信号  - 九五之尊 + 高开(2%-5%)
  B. 分时强度  - 强分时脉冲 + 极高换手(>3%)
  C. 量能梯队  - 量比>1 + 成交量阶梯式放大
  D. 资金确认  - 大单净流入

Phase1（realtime=False）：只计算 A + C（离线技术指标）
Phase2（realtime=True） ：追加计算 B + D（实时盘口数据）

进种子池门槛：phase1 得分 >= 2（A组至少通过九五之尊）
"""
import logging
from typing import Dict, Any

import pandas as pd

from src.screening.models.base import ModelResult
from src.screening.screener import DimResult, _get_stock_name
from src.screening.indicators import (
    get_daily_df,
    check_jiuyu_zhizun, check_volume_expand, check_volume_ratio,
    check_intraday_strong, check_turnover, check_fund_flow,
    _ok, _fail, _skip,
)

logger = logging.getLogger(__name__)

MIN_SEED_SCORE = 2  # Phase1（4维离线）中至少 2 分进种子池

IndicatorResult = Dict[str, Any]


def _check_high_open_range(df: pd.DataFrame, low_pct: float = 2.0, high_pct: float = 5.0) -> IndicatorResult:
    """
    涨停模型专用高开检测：高开区间 [low_pct%, high_pct%]。
    过度高开（>5%）往往是情绪透支，不如区间内高开。
    """
    if df is None or len(df) < 2:
        return _skip("数据不足")
    try:
        today_open = df.iloc[-1]["open"]
        yesterday_close = df.iloc[-2]["close"]
        open_pct = (today_open - yesterday_close) / yesterday_close * 100
        if low_pct <= open_pct <= high_pct:
            return _ok(open_pct, f"高开{open_pct:.2f}%（在目标区间[{low_pct}%,{high_pct}%]）")
        return _fail(open_pct, f"高开{open_pct:.2f}%，不在目标区间[{low_pct}%,{high_pct}%]")
    except Exception as e:
        return _skip(str(e))


class LimitUpHunter:
    """强势涨停模型 - Phase1 粗筛九五之尊形态 + Phase2 实时触发"""

    NAME = "模型四：强势涨停(LimitUpHunter)"

    def run(self, code: str, df=None, realtime: bool = False) -> ModelResult:
        """
        Args:
            code    : 股票代码
            df      : 日线 DataFrame
            realtime: False=Phase1离线，True=Phase2实时（追加B+D）
        """
        result = ModelResult(code=code, strategy=self.NAME, model_name="LimitUpHunter")

        if df is None:
            df = get_daily_df(code, days=30)

        result.name = _get_stock_name(code)

        # ---- A. 核心信号 ----
        grp_a = []
        for name, r in [
            ("九五之尊", check_jiuyu_zhizun(df)),
            ("高开2%-5%", _check_high_open_range(df, 2.0, 5.0)),
        ]:
            d = DimResult(name, r["passed"], r["value"], r["detail"])
            result.dims.append(d); grp_a.append(d)
        result.groups["A.核心信号"] = grp_a

        # ---- B. 分时强度（Phase2 实时） ----
        grp_b = []
        if realtime:
            for name, r in [
                ("强分时脉冲", check_intraday_strong(code)),
                ("极高换手>3%", check_turnover(df, threshold=3.0)),
            ]:
                d = DimResult(name, r["passed"], r["value"], r["detail"])
                result.dims.append(d); grp_b.append(d)
        result.groups["B.分时强度"] = grp_b

        # ---- C. 量能梯队 ----
        grp_c = []
        for name, r in [
            ("量比>1", check_volume_ratio(df, threshold=1.0)),
            ("量能阶梯放大", check_volume_expand(df)),
        ]:
            d = DimResult(name, r["passed"], r["value"], r["detail"])
            result.dims.append(d); grp_c.append(d)
        result.groups["C.量能梯队"] = grp_c

        # ---- D. 资金确认（Phase2 实时） ----
        grp_d = []
        if realtime:
            r_ff = check_fund_flow(code)
            d_ff = DimResult("大单净流入", r_ff["passed"], r_ff["value"], r_ff["detail"])
            result.dims.append(d_ff); grp_d.append(d_ff)
        result.groups["D.资金确认"] = grp_d

        result.phase1_score = result.total_score
        return result

    def is_qualified_seed(self, result: ModelResult) -> bool:
        """
        进种子池条件：
        - 总分 >= MIN_SEED_SCORE
        - A组（九五之尊）必须通过
        """
        if result.total_score < MIN_SEED_SCORE:
            return False
        a_dims = result.groups.get("A.核心信号", [])
        return any(d.name == "九五之尊" and d.passed is True for d in a_dims)
