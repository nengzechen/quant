# -*- coding: utf-8 -*-
"""
模型一：深度抄底 (BottomFishing)

核心逻辑：利用大级别背离与极度超卖寻找反转。
注意：本模型禁用均线多头排列过滤（价格仍在低位是正常的）。

维度（共 7 维）：
  A. 大级别背离  - 日线MACD底背离 + 周线MACD底背离（共振加分）
  B. 极度超卖    - CYS<-15 + CD40<-20
  C. 缠论确认    - 底分型确认
  D. 资金启动    - 资金流入 + 量比>1

进种子池门槛：total_score >= 4，且 A组至少通过 1 维
"""
import logging
from typing import Optional

from src.screening.models.base import ModelResult
from src.screening.screener import DimResult, _get_stock_name
from src.screening.indicators import (
    get_daily_df,
    check_macd_divergence,
    check_cys, check_cd40,
    check_chan_bottom_pattern,
    check_fund_flow,
    check_volume_ratio,
)

logger = logging.getLogger(__name__)

MIN_SEED_SCORE = 4  # 7维中至少 4 分进种子池


class BottomFishing:
    """深度抄底模型 - Phase1 离线全市场扫描用"""

    NAME = "模型一：深度抄底(BottomFishing)"

    def run(self, code: str, df=None, weekly_df=None) -> ModelResult:
        """
        Args:
            code     : 股票代码
            df       : 日线 DataFrame（外部传入可复用缓存，None 时自动拉取）
            weekly_df: 周线 DataFrame（None 时由日线聚合生成）

        Returns:
            ModelResult
        """
        result = ModelResult(code=code, strategy=self.NAME, model_name="BottomFishing")

        if df is None:
            df = get_daily_df(code, days=120)

        # 周线数据：由日线聚合生成（与 Strategy2 保持一致）
        if weekly_df is None and df is not None and len(df) >= 50:
            weekly_df = df.resample("W", on="date").agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum",
            }).dropna().reset_index()

        result.name = _get_stock_name(code)

        # ---- A. 大级别背离 ----
        grp_a = []
        r_d = check_macd_divergence(df, use_weekly=False)
        d1 = DimResult("日线MACD底背离", r_d["passed"], r_d["value"], r_d["detail"])
        result.dims.append(d1); grp_a.append(d1)

        if weekly_df is not None and len(weekly_df) >= 8:
            r_w = check_macd_divergence(weekly_df)
            detail_w = r_w["detail"].replace("底背驰", "周线底背驰").replace("无背离", "周线无背离")
            d2 = DimResult("周线MACD底背离", r_w["passed"], r_w["value"], detail_w)
        else:
            d2 = DimResult("周线MACD底背离", None, None, "数据不足")
        result.dims.append(d2); grp_a.append(d2)
        result.groups["A.大级别背离"] = grp_a

        # ---- B. 极度超卖 ----
        grp_b = []
        for name, r in [
            ("CYS<-15(超跌)", check_cys(df, threshold=-15.0)),
            ("CD40<-20(动量超跌)", check_cd40(df, threshold=-20.0)),
        ]:
            d = DimResult(name, r["passed"], r["value"], r["detail"])
            result.dims.append(d); grp_b.append(d)
        result.groups["B.极度超卖"] = grp_b

        # ---- C. 缠论底分型确认 ----
        grp_c = []
        r_bp = check_chan_bottom_pattern(df)
        d3 = DimResult("底分型确认", r_bp["passed"], r_bp["value"], r_bp["detail"])
        result.dims.append(d3); grp_c.append(d3)
        result.groups["C.缠论确认"] = grp_c

        # ---- D. 资金启动信号 ----
        grp_d = []
        for name, r in [
            ("资金流入", check_fund_flow(code)),
            ("量比>1", check_volume_ratio(df, threshold=1.0)),
        ]:
            d = DimResult(name, r["passed"], r["value"], r["detail"])
            result.dims.append(d); grp_d.append(d)
        result.groups["D.资金启动"] = grp_d

        result.phase1_score = result.total_score
        return result

    def is_qualified_seed(self, result: ModelResult) -> bool:
        """
        判断是否进入种子池：
        - 总分 >= MIN_SEED_SCORE
        - A组（大级别背离）至少通过 1 维
        """
        if result.total_score < MIN_SEED_SCORE:
            return False
        a_dims = result.groups.get("A.大级别背离", [])
        return any(d.passed is True for d in a_dims)
