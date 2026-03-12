# -*- coding: utf-8 -*-
"""
模型二：波段操作 (SwingTrading)

核心逻辑：在趋势明确后的回踩中寻找起涨点。

维度（共 8 维）：
  A. 趋势环境  - 均线多头排列 + KDJ[50,100]
  B. 形态支撑  - 头肩底形态 OR 回踩关键均线（任一通过即得分）
  C. 缠论确认  - 底分型确认（回调结束的关键信号）
  D. 动能辅助  - MACD金叉>MA20 + 量能放大
  E. 基本面    - PE合理

进种子池门槛：total_score >= 5，且 A组全部通过 + C组通过
"""
import logging
from typing import Dict, Any

import pandas as pd

from src.screening.models.base import ModelResult
from src.screening.screener import DimResult, _get_stock_name
from src.screening.indicators import (
    get_daily_df,
    check_ma_bull, check_kdj_above50,
    check_head_shoulder_bottom,
    check_chan_bottom_pattern,
    check_macd_golden_above_ma20,
    check_volume_expand,
    check_pe,
    _ok, _fail, _skip,
)

logger = logging.getLogger(__name__)

MIN_SEED_SCORE = 5  # 8维中至少 5 分进种子池

IndicatorResult = Dict[str, Any]


def _check_pullback_ma(df: pd.DataFrame) -> IndicatorResult:
    """
    内部辅助：回踩关键均线支撑（MA20 或 MA60），价格在均线 ±2% 以内
    """
    if df is None or len(df) < 60:
        return _skip("数据不足")
    try:
        c = df["close"]
        price = c.iloc[-1]
        ma20 = c.rolling(20).mean().iloc[-1]
        ma60 = c.rolling(60).mean().iloc[-1]
        near_ma20 = abs(price - ma20) / ma20 < 0.02
        near_ma60 = abs(price - ma60) / ma60 < 0.02
        if near_ma20:
            return _ok(ma20, f"回踩MA20支撑 ({ma20:.2f})，偏离{(price-ma20)/ma20*100:.1f}%")
        if near_ma60:
            return _ok(ma60, f"回踩MA60支撑 ({ma60:.2f})，偏离{(price-ma60)/ma60*100:.1f}%")
        return _fail(None, f"未触及关键均线 MA20={ma20:.2f} MA60={ma60:.2f}")
    except Exception as e:
        return _skip(str(e))


class SwingTrading:
    """波段操作模型 - Phase1 离线全市场扫描用"""

    NAME = "模型二：波段操作(SwingTrading)"

    def run(self, code: str, df=None) -> ModelResult:
        result = ModelResult(code=code, strategy=self.NAME, model_name="SwingTrading")

        if df is None:
            df = get_daily_df(code, days=120)

        result.name = _get_stock_name(code)

        # ---- A. 趋势环境 ----
        grp_a = []
        for name, r in [
            ("均线多头排列", check_ma_bull(df)),
            ("KDJ[50,100]", check_kdj_above50(df)),
        ]:
            d = DimResult(name, r["passed"], r["value"], r["detail"])
            result.dims.append(d); grp_a.append(d)
        result.groups["A.趋势环境"] = grp_a

        # ---- B. 形态支撑（头肩底 OR 回踩均线，任一通过得分） ----
        grp_b = []
        r_hs = check_head_shoulder_bottom(df)
        r_pb = _check_pullback_ma(df)
        # 取更好的那个
        if r_hs["passed"] is True:
            d_form = DimResult("头肩底形态", True, r_hs["value"], r_hs["detail"])
        elif r_pb["passed"] is True:
            d_form = DimResult("回踩关键均线", True, r_pb["value"], r_pb["detail"])
        else:
            # 都未通过，记录头肩底（更有价值的信号）
            d_form = DimResult("形态支撑", False, None,
                               f"头肩底:{r_hs['detail']} / 回踩:{r_pb['detail']}")
        result.dims.append(d_form); grp_b.append(d_form)
        result.groups["B.形态支撑"] = grp_b

        # ---- C. 缠论底分型确认 ----
        grp_c = []
        r_bp = check_chan_bottom_pattern(df)
        d_chan = DimResult("底分型确认", r_bp["passed"], r_bp["value"], r_bp["detail"])
        result.dims.append(d_chan); grp_c.append(d_chan)
        result.groups["C.缠论确认"] = grp_c

        # ---- D. 动能辅助 ----
        grp_d = []
        for name, r in [
            ("MACD金叉>MA20", check_macd_golden_above_ma20(df)),
            ("量能放大", check_volume_expand(df)),
        ]:
            d = DimResult(name, r["passed"], r["value"], r["detail"])
            result.dims.append(d); grp_d.append(d)
        result.groups["D.动能辅助"] = grp_d

        # ---- E. 基本面 ----
        grp_e = []
        r_pe = check_pe(code)
        d_pe = DimResult("PE合理", r_pe["passed"], r_pe["value"], r_pe["detail"])
        result.dims.append(d_pe); grp_e.append(d_pe)
        result.groups["E.基本面"] = grp_e

        result.phase1_score = result.total_score
        return result

    def is_qualified_seed(self, result: ModelResult) -> bool:
        """
        进种子池条件：
        - 总分 >= MIN_SEED_SCORE
        - A组（均线+KDJ）全部通过
        - C组（底分型）通过
        """
        if result.total_score < MIN_SEED_SCORE:
            return False
        a_dims = result.groups.get("A.趋势环境", [])
        c_dims = result.groups.get("C.缠论确认", [])
        a_ok = all(d.passed is True for d in a_dims)
        c_ok = any(d.passed is True for d in c_dims)
        return a_ok and c_ok
