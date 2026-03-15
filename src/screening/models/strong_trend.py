# -*- coding: utf-8 -*-
"""
模型四：强势 (StrongTrend)
适用场景：追踪市场热点板块，捕捉主升浪

维度（共 12 维）：
  A. 市场环境  - Top5板块 + 市场情绪扩张（涨跌家数比>1）
  B. 技术共振  - 均线多头排列 + MACD金叉>MA20 + KDJ>90 + DMI强势手拉手
  C. 强势特征  - 博弈长阳 + CYS13>9.5
  D. 缠论确认  - 底分型
  E. 基本面    - PE合理 + 净利润同比预增

Phase1（realtime=False）：计算 A + B + C + D + E（离线）
Phase2（realtime=True） ：追加实时触发条件

进种子池门槛：总分 >= 5，且 A组至少1维 + B组至少2维
"""
import logging

from src.screening.models.base import ModelResult
from src.screening.screener import DimResult, _get_stock_name
from src.screening.indicators import (
    get_daily_df, get_stock_sector,
    check_ma_bull, check_dmi_strong, check_battle_long,
    check_macd_golden_above_ma20, check_kdj_above90,
    check_chan_bottom_pattern,
    check_cys_positive,
    check_sector_top5, check_sector_limitup,
    check_market_breadth,
    check_intraday_strong, check_volume_ratio, check_fund_flow,
    check_pe, check_profit_growth,
)

logger = logging.getLogger(__name__)

MIN_SEED_SCORE = 5  # Phase1（12维）中至少 5 分进种子池


class StrongTrend:
    """强势模型 - Phase1 离线预选 + Phase2 实时触发"""

    NAME = "模型四：强势(StrongTrend)"

    def run(self, code: str, df=None, realtime: bool = False) -> ModelResult:
        result = ModelResult(code=code, strategy=self.NAME, model_name="StrongTrend")

        if df is None:
            df = get_daily_df(code, days=120)

        result.name = _get_stock_name(code)
        sector = get_stock_sector(code)

        # ---- A. 市场环境 ----
        grp_a = []
        for name, r in [
            ("Top5板块", check_sector_top5(code, sector)),
            ("市场情绪扩张", check_market_breadth()),
        ]:
            d = DimResult(name, r["passed"], r["value"], r["detail"])
            result.dims.append(d); grp_a.append(d)
        result.groups["A.市场环境"] = grp_a

        # ---- B. 技术共振 ----
        grp_b = []
        for name, r in [
            ("均线多头排列", check_ma_bull(df)),
            ("MACD金叉>MA20", check_macd_golden_above_ma20(df)),
            ("KDJ>90", check_kdj_above90(df)),
            ("DMI强势手拉手", check_dmi_strong(df)),
        ]:
            d = DimResult(name, r["passed"], r["value"], r["detail"])
            result.dims.append(d); grp_b.append(d)
        result.groups["B.技术共振"] = grp_b

        # ---- C. 强势特征 ----
        grp_c = []
        for name, r in [
            ("博弈长阳", check_battle_long(df)),
            ("CYS13>9.5", check_cys_positive(df, threshold=9.5)),
        ]:
            d = DimResult(name, r["passed"], r["value"], r["detail"])
            result.dims.append(d); grp_c.append(d)
        result.groups["C.强势特征"] = grp_c

        # ---- D. 缠论确认 ----
        grp_d = []
        r_bp = check_chan_bottom_pattern(df)
        d_chan = DimResult("底分型确认", r_bp["passed"], r_bp["value"], r_bp["detail"])
        result.dims.append(d_chan); grp_d.append(d_chan)
        result.groups["D.缠论确认"] = grp_d

        # ---- E. 基本面 ----
        grp_e = []
        for name, r in [
            ("PE合理", check_pe(code)),
            ("净利润预增", check_profit_growth(code)),
        ]:
            d = DimResult(name, r["passed"], r["value"], r["detail"])
            result.dims.append(d); grp_e.append(d)
        result.groups["E.基本面"] = grp_e

        # ---- F. 日内触发（Phase2 实时计算） ----
        grp_f = []
        if realtime:
            for name, r in [
                ("强分时", check_intraday_strong(code)),
                ("量比>1", check_volume_ratio(df, threshold=1.0)),
                ("大单净流入", check_fund_flow(code)),
            ]:
                d = DimResult(name, r["passed"], r["value"], r["detail"])
                result.dims.append(d); grp_f.append(d)
        result.groups["F.日内触发"] = grp_f

        result.phase1_score = result.total_score
        return result

    def is_qualified_seed(self, result: ModelResult) -> bool:
        """
        进种子池条件：
        - 总分 >= MIN_SEED_SCORE
        - A组至少 1 维通过（板块或市场情绪支撑）
        - B组至少 2 维通过（技术共振）
        """
        if result.total_score < MIN_SEED_SCORE:
            return False
        a_dims = result.groups.get("A.市场环境", [])
        b_dims = result.groups.get("B.技术共振", [])
        a_ok = sum(1 for d in a_dims if d.passed is True) >= 1
        b_ok = sum(1 for d in b_dims if d.passed is True) >= 2
        return a_ok and b_ok
