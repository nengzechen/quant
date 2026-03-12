# -*- coding: utf-8 -*-
"""
模型三：强势 (StrongTrend)

核心逻辑：追踪市场热点板块，捕捉主升浪。

维度（共 8 维）：
  A. 市场环境  - Top5板块 + 市场情绪扩张（涨跌家数比）
  B. 技术共振  - 均线多头排列 + DMI手拉手 + 博弈长阳
  C. 日内触发  - 强分时 + 量比>1 + 大单净流入（Phase2 实时计算）
  D. 基本面    - 板块内涨停最多

Phase1（realtime=False）：只计算 A + B + D（离线可算）
Phase2（realtime=True） ：追加计算 C 组（实时盘口数据）

进种子池门槛：phase1 得分 >= 4（A组≥1 且 B组≥2）
"""
import logging

from src.screening.models.base import ModelResult
from src.screening.screener import DimResult, _get_stock_name
from src.screening.indicators import (
    get_daily_df, get_stock_sector,
    check_ma_bull, check_dmi, check_battle_long,
    check_sector_top5, check_sector_limitup,
    check_market_breadth,
    check_intraday_strong, check_volume_ratio, check_fund_flow,
)

logger = logging.getLogger(__name__)

MIN_SEED_SCORE = 4  # Phase1（5维离线）中至少 4 分进种子池


class StrongTrend:
    """强势模型 - Phase1 离线预选 + Phase2 实时触发"""

    NAME = "模型三：强势(StrongTrend)"

    def run(self, code: str, df=None, realtime: bool = False) -> ModelResult:
        """
        Args:
            code    : 股票代码
            df      : 日线 DataFrame
            realtime: False=Phase1离线模式，True=Phase2实时模式（追加C组计算）
        """
        result = ModelResult(code=code, strategy=self.NAME, model_name="StrongTrend")

        if df is None:
            df = get_daily_df(code, days=100)

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
            ("DMI手拉手", check_dmi(df)),
            ("博弈长阳", check_battle_long(df)),
        ]:
            d = DimResult(name, r["passed"], r["value"], r["detail"])
            result.dims.append(d); grp_b.append(d)
        result.groups["B.技术共振"] = grp_b

        # ---- C. 日内触发（Phase2 实时计算） ----
        grp_c = []
        if realtime:
            for name, r in [
                ("强分时", check_intraday_strong(code)),
                ("量比>1", check_volume_ratio(df, threshold=1.0)),
                ("大单净流入", check_fund_flow(code)),
            ]:
                d = DimResult(name, r["passed"], r["value"], r["detail"])
                result.dims.append(d); grp_c.append(d)
        result.groups["C.日内触发"] = grp_c

        # ---- D. 基本面辅助 ----
        grp_d = []
        r_lu = check_sector_limitup(code, sector)
        d_lu = DimResult("板块涨停最多", r_lu["passed"], r_lu["value"], r_lu["detail"])
        result.dims.append(d_lu); grp_d.append(d_lu)
        result.groups["D.基本面辅助"] = grp_d

        result.phase1_score = result.total_score
        return result

    def is_qualified_seed(self, result: ModelResult) -> bool:
        """
        进种子池条件：
        - 总分 >= MIN_SEED_SCORE
        - A组至少 1 维通过（必须有板块或情绪支撑）
        - B组至少 2 维通过
        """
        if result.total_score < MIN_SEED_SCORE:
            return False
        a_dims = result.groups.get("A.市场环境", [])
        b_dims = result.groups.get("B.技术共振", [])
        a_ok = sum(1 for d in a_dims if d.passed is True) >= 1
        b_ok = sum(1 for d in b_dims if d.passed is True) >= 2
        return a_ok and b_ok
