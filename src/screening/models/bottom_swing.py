# -*- coding: utf-8 -*-
"""
模型一：抄底/波段 (BottomSwing)

合并了原 BottomFishing（深度抄底）与 SwingTrading（波段操作）两个模型。
核心判断：以缠论底分型为锚点，分两条路径入选：

  抄底路径（oversold reversal）：
    A组大级别背离至少1维通过 → 总分 >= 4
    适用于：价格仍在低位，均线尚未多头，等待反转

  波段路径（trend pullback）：
    C组趋势环境全部通过 + E组缠论底分型通过 → 总分 >= 5
    适用于：均线多头排列，回踩支撑，寻找起涨点

维度（共 11 维）：
  A. 大级别背离  - 日线MACD底背离 + 周线MACD底背离
  B. 极度超卖    - CYS<-15 + CD40<-20
  C. 趋势环境    - 均线多头排列 + KDJ[50,100]
  D. 形态支撑    - 头肩底 OR 回踩关键均线（任一通过）
  E. 缠论确认    - 底分型确认
  F. 动能辅助    - MACD金叉>MA20 + 量能放大
  G. 资金启动    - 资金流入 + 量比>1
  H. 基本面      - PE合理
"""
import logging
from typing import Optional

import pandas as pd

from src.screening.models.base import ModelResult
from src.screening.screener import DimResult, _get_stock_name
from src.screening.indicators import (
    get_daily_df,
    check_macd_divergence,
    check_cys, check_cd40,
    check_ma_bull, check_kdj_above50,
    check_head_shoulder_bottom,
    check_chan_bottom_pattern,
    check_macd_golden_above_ma20,
    check_volume_expand,
    check_fund_flow,
    check_volume_ratio,
    check_pe,
    _ok, _fail, _skip,
)

logger = logging.getLogger(__name__)

MIN_SEED_SCORE_REVERSAL = 4   # 抄底路径：11维中至少 4 分
MIN_SEED_SCORE_PULLBACK = 5   # 波段路径：11维中至少 5 分


def _check_pullback_ma(df: pd.DataFrame) -> dict:
    """回踩关键均线支撑（MA20 或 MA60），价格在均线 ±2% 以内"""
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


class BottomSwing:
    """抄底/波段模型 - Phase1 离线全市场扫描用"""

    NAME = "模型一：抄底/波段(BottomSwing)"

    def run(self, code: str, df=None, weekly_df=None) -> ModelResult:
        """
        Args:
            code     : 股票代码
            df       : 日线 DataFrame（None 时自动拉取）
            weekly_df: 周线 DataFrame（None 时由日线聚合）
        Returns:
            ModelResult
        """
        result = ModelResult(code=code, strategy=self.NAME, model_name="BottomSwing")

        if df is None:
            df = get_daily_df(code, days=120)

        if weekly_df is None and df is not None and len(df) >= 50:
            weekly_df = df.resample("W", on="date").agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum",
            }).dropna().reset_index()

        result.name = _get_stock_name(code)

        # ---- A. 大级别背离 ----
        grp_a = []
        r_d = check_macd_divergence(df, use_weekly=False)
        d_daily = DimResult("日线MACD底背离", r_d["passed"], r_d["value"], r_d["detail"])
        result.dims.append(d_daily); grp_a.append(d_daily)

        if weekly_df is not None and len(weekly_df) >= 8:
            r_w = check_macd_divergence(weekly_df)
            detail_w = r_w["detail"].replace("底背驰", "周线底背驰").replace("无背离", "周线无背离")
            d_weekly = DimResult("周线MACD底背离", r_w["passed"], r_w["value"], detail_w)
        else:
            d_weekly = DimResult("周线MACD底背离", None, None, "数据不足")
        result.dims.append(d_weekly); grp_a.append(d_weekly)
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

        # ---- C. 趋势环境 ----
        grp_c = []
        for name, r in [
            ("均线多头排列", check_ma_bull(df)),
            ("KDJ[50,100]", check_kdj_above50(df)),
        ]:
            d = DimResult(name, r["passed"], r["value"], r["detail"])
            result.dims.append(d); grp_c.append(d)
        result.groups["C.趋势环境"] = grp_c

        # ---- D. 形态支撑（头肩底 OR 回踩均线，任一通过得分） ----
        grp_d = []
        r_hs = check_head_shoulder_bottom(df)
        r_pb = _check_pullback_ma(df)
        if r_hs["passed"] is True:
            d_form = DimResult("头肩底形态", True, r_hs["value"], r_hs["detail"])
        elif r_pb["passed"] is True:
            d_form = DimResult("回踩关键均线", True, r_pb["value"], r_pb["detail"])
        else:
            d_form = DimResult("形态支撑", False, None,
                               f"头肩底:{r_hs['detail']} / 回踩:{r_pb['detail']}")
        result.dims.append(d_form); grp_d.append(d_form)
        result.groups["D.形态支撑"] = grp_d

        # ---- E. 缠论底分型确认 ----
        grp_e = []
        r_bp = check_chan_bottom_pattern(df)
        d_chan = DimResult("底分型确认", r_bp["passed"], r_bp["value"], r_bp["detail"])
        result.dims.append(d_chan); grp_e.append(d_chan)
        result.groups["E.缠论确认"] = grp_e

        # ---- F. 动能辅助 ----
        grp_f = []
        for name, r in [
            ("MACD金叉>MA20", check_macd_golden_above_ma20(df)),
            ("量能放大", check_volume_expand(df)),
        ]:
            d = DimResult(name, r["passed"], r["value"], r["detail"])
            result.dims.append(d); grp_f.append(d)
        result.groups["F.动能辅助"] = grp_f

        # ---- G. 资金启动 ----
        grp_g = []
        for name, r in [
            ("资金流入", check_fund_flow(code)),
            ("量比>1", check_volume_ratio(df, threshold=1.0)),
        ]:
            d = DimResult(name, r["passed"], r["value"], r["detail"])
            result.dims.append(d); grp_g.append(d)
        result.groups["G.资金启动"] = grp_g

        # ---- H. 基本面 ----
        grp_h = []
        r_pe = check_pe(code)
        d_pe = DimResult("PE合理", r_pe["passed"], r_pe["value"], r_pe["detail"])
        result.dims.append(d_pe); grp_h.append(d_pe)
        result.groups["H.基本面"] = grp_h

        result.phase1_score = result.total_score
        return result

    def is_qualified_seed(self, result: ModelResult) -> bool:
        """
        进种子池判断（两条路径之一满足即可）：

        抄底路径：总分 >= 4，且 A组（大级别背离）至少 1 维通过
        波段路径：总分 >= 5，且 C组（趋势环境）全部通过，且 E组（底分型）通过
        """
        score = result.total_score
        a_dims = result.groups.get("A.大级别背离", [])
        c_dims = result.groups.get("C.趋势环境", [])
        e_dims = result.groups.get("E.缠论确认", [])

        # 抄底路径
        reversal_ok = (
            score >= MIN_SEED_SCORE_REVERSAL
            and any(d.passed is True for d in a_dims)
        )
        # 波段路径
        pullback_ok = (
            score >= MIN_SEED_SCORE_PULLBACK
            and all(d.passed is True for d in c_dims)
            and any(d.passed is True for d in e_dims)
        )
        return reversal_ok or pullback_ok
