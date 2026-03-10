# -*- coding: utf-8 -*-
"""
===================================
选股策略引擎
===================================

策略一：强势多头突破/接力策略
    - 基本面 + 板块前五 → 均线多头+底分型+头肩底 → MACD金叉+KDJ>50+DMI手拉手
    - 博弈长阳+量能放大 → 高开+强分时+量比+换手率+资金流入

策略二：缠论深度抄底策略
    - 日线MACD底背离（最好共振周线）
    - CYS<-15 && CD40<-20（极度超跌）
    - 日线底分型确认（不见底分型不抄底）

用法：
    s1 = Strategy1()
    result = s1.run("600519")

    s2 = Strategy2()
    result = s2.run("000858")
"""

import logging
import time
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from src.indicators import (
    get_daily_df, get_stock_sector,
    # 市场情绪
    check_kdj_market, check_market_breadth,
    # 板块
    check_sector_top5, check_sector_limitup, get_top5_sectors, get_limitup_sector,
    # 基本面
    check_pe, check_profit_growth,
    # 资金盘口
    check_high_open, check_intraday_strong, check_volume_ratio,
    check_turnover, check_fund_flow, check_volume_expand,
    # 技术形态
    check_ma_bull, check_macd_golden_above_ma20, check_kdj_above50,
    check_dmi, check_head_shoulder_bottom,
    # 缠论
    check_chan_bottom_pattern, check_macd_divergence,
    # 特色主力
    check_battle_long, check_jiuyu_zhizun, check_cys, check_cd40,
)

logger = logging.getLogger(__name__)


# ============================================================
# 结果数据结构
# ============================================================

@dataclass
class DimResult:
    name: str
    passed: Optional[bool]   # True/False/None(跳过)
    value: Any = None
    detail: str = ""

    @property
    def score(self) -> int:
        if self.passed is True:
            return 1
        return 0


@dataclass
class StrategyResult:
    code: str
    strategy: str
    name: str = ""

    dims: List[DimResult] = field(default_factory=list)
    groups: Dict[str, List[DimResult]] = field(default_factory=dict)  # 分组得分

    @property
    def total_score(self) -> int:
        return sum(d.score for d in self.dims)

    @property
    def max_score(self) -> int:
        return len(self.dims)

    @property
    def passed_dims(self) -> List[str]:
        return [d.name for d in self.dims if d.passed is True]

    @property
    def failed_dims(self) -> List[str]:
        return [d.name for d in self.dims if d.passed is False]

    @property
    def skipped_dims(self) -> List[str]:
        return [d.name for d in self.dims if d.passed is None]

    def group_score(self, group: str) -> str:
        dims = self.groups.get(group, [])
        passed = sum(1 for d in dims if d.passed is True)
        return f"{passed}/{len(dims)}"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "strategy": self.strategy,
            "name": self.name,
            "total_score": self.total_score,
            "max_score": self.max_score,
            "score_pct": round(self.total_score / self.max_score * 100, 1) if self.max_score > 0 else 0,
            "passed_dims": self.passed_dims,
            "failed_dims": self.failed_dims,
            "skipped_dims": self.skipped_dims,
            "group_scores": {g: self.group_score(g) for g in self.groups},
            "details": {d.name: {"passed": d.passed, "value": str(d.value)[:50], "detail": d.detail}
                        for d in self.dims},
        }


# ============================================================
# 策略一：强势多头突破/接力策略
# ============================================================

class Strategy1:
    """
    强势多头突破/接力策略

    适用场景：市场情绪较好，做强势股和龙头股

    5大模块分组：
    A. 前置过滤（基本面+板块）
    B. 趋势条件（均线+底分型+头肩底）
    C. 动能条件（MACD+KDJ+DMI）
    D. 主力量价（博弈长阳+量能放大）
    E. 日内触发（高开+强分时+量比+换手率+资金流入）
    """

    NAME = "策略一：强势多头突破/接力"

    def run(self, code: str, df=None) -> StrategyResult:
        result = StrategyResult(code=code, strategy=self.NAME)
        if df is None:
            df = get_daily_df(code, days=100)

        sector = get_stock_sector(code)

        # ---- A. 前置过滤 ----
        grp_a = []
        for name, r in [
            ("基本面-PE", check_pe(code)),
            ("基本面-业绩增长", check_profit_growth(code)),
            ("板块前五", check_sector_top5(code, sector)),
            ("板块涨停", check_sector_limitup(code, sector)),
        ]:
            d = DimResult(name=name, passed=r["passed"], value=r["value"], detail=r["detail"])
            result.dims.append(d)
            grp_a.append(d)
        result.groups["A.前置过滤"] = grp_a

        # ---- B. 趋势条件 ----
        grp_b = []
        for name, r in [
            ("均线多头排列", check_ma_bull(df)),
            ("缠论底分型", check_chan_bottom_pattern(df)),
            ("头肩底形态", check_head_shoulder_bottom(df)),
        ]:
            d = DimResult(name=name, passed=r["passed"], value=r["value"], detail=r["detail"])
            result.dims.append(d)
            grp_b.append(d)
        result.groups["B.趋势条件"] = grp_b

        # ---- C. 动能条件 ----
        grp_c = []
        for name, r in [
            ("MACD金叉>MA20", check_macd_golden_above_ma20(df)),
            ("KDJ>50", check_kdj_above50(df)),
            ("DMI手拉手", check_dmi(df)),
        ]:
            d = DimResult(name=name, passed=r["passed"], value=r["value"], detail=r["detail"])
            result.dims.append(d)
            grp_c.append(d)
        result.groups["C.动能条件"] = grp_c

        # ---- D. 主力量价 ----
        grp_d = []
        for name, r in [
            ("博弈长阳", check_battle_long(df)),
            ("量能放大", check_volume_expand(df)),
            ("九五之尊", check_jiuyu_zhizun(df)),
        ]:
            d = DimResult(name=name, passed=r["passed"], value=r["value"], detail=r["detail"])
            result.dims.append(d)
            grp_d.append(d)
        result.groups["D.主力量价"] = grp_d

        # ---- E. 日内触发 ----
        grp_e = []
        for name, r in [
            ("高开", check_high_open(df)),
            ("强分时", check_intraday_strong(code)),
            ("量比>1", check_volume_ratio(df, threshold=1.0)),
            ("换手率>3%", check_turnover(df, threshold=3.0)),
            ("资金流入", check_fund_flow(code)),
        ]:
            d = DimResult(name=name, passed=r["passed"], value=r["value"], detail=r["detail"])
            result.dims.append(d)
            grp_e.append(d)
        result.groups["E.日内触发"] = grp_e

        return result


# ============================================================
# 策略二：缠论深度抄底策略
# ============================================================

class Strategy2:
    """
    缠论深度抄底策略（高胜率反弹）

    适用场景：标的经历大幅下跌，寻找大级别转折点

    3大模块：
    A. 大级别信号（MACD底背离）
    B. 极度超卖（CYS+CD40）
    C. 底分型确认（不见底分型不抄底）
    """

    NAME = "策略二：缠论深度抄底"

    def run(self, code: str, df=None) -> StrategyResult:
        result = StrategyResult(code=code, strategy=self.NAME)
        if df is None:
            df = get_daily_df(code, days=120)

        # ---- A. 大级别背离信号 ----
        grp_a = []
        # 日线背离
        r_div = check_macd_divergence(df, use_weekly=False)
        d = DimResult(name="日线MACD底背离", passed=r_div["passed"], value=r_div["value"], detail=r_div["detail"])
        result.dims.append(d)
        grp_a.append(d)

        # 周线背离（用日线数据转化为周线近似）
        if df is not None and len(df) >= 50:
            weekly_df = df.resample("W", on="date").agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum"
            }).dropna().reset_index()
            r_wdiv = check_macd_divergence(weekly_df)
            detail_w = r_wdiv["detail"].replace("底背驰", "周线底背驰").replace("无背离", "周线无背离")
            d_w = DimResult(name="周线MACD底背离", passed=r_wdiv["passed"], value=r_wdiv["value"], detail=detail_w)
        else:
            d_w = DimResult(name="周线MACD底背离", passed=None, value=None, detail="数据不足")
        result.dims.append(d_w)
        grp_a.append(d_w)
        result.groups["A.大级别背离"] = grp_a

        # ---- B. 极度超卖 ----
        grp_b = []
        for name, r in [
            ("CYS<-15(超跌)", check_cys(df, threshold=-15.0)),
            ("CD40<-20(动量超跌)", check_cd40(df, threshold=-20.0)),
        ]:
            d = DimResult(name=name, passed=r["passed"], value=r["value"], detail=r["detail"])
            result.dims.append(d)
            grp_b.append(d)
        result.groups["B.极度超卖"] = grp_b

        # ---- C. 底分型确认 ----
        grp_c = []
        r_bp = check_chan_bottom_pattern(df)
        d = DimResult(name="底分型确认", passed=r_bp["passed"], value=r_bp["value"], detail=r_bp["detail"])
        result.dims.append(d)
        grp_c.append(d)

        # 补充：资金开始流入（抄底后开始有资金介入）
        r_ff = check_fund_flow(code)
        d2 = DimResult(name="资金流入", passed=r_ff["passed"], value=r_ff["value"], detail=r_ff["detail"])
        result.dims.append(d2)
        grp_c.append(d2)
        result.groups["C.底分型确认"] = grp_c

        return result


# ============================================================
# 批量筛选
# ============================================================

def run_strategy1_batch(codes: List[str], min_score: int = 9) -> List[StrategyResult]:
    """批量跑策略一，按总分降序返回"""
    s = Strategy1()
    # 预加载板块
    get_top5_sectors()
    get_limitup_sector()

    results = []
    for code in codes:
        try:
            time.sleep(0.5)
            r = s.run(code)
            logger.info(f"[策略一] {code}: {r.total_score}/{r.max_score} {r.passed_dims}")
            results.append(r)
        except Exception as e:
            logger.error(f"[策略一] {code} 出错: {e}")
    results.sort(key=lambda x: x.total_score, reverse=True)
    return [r for r in results if r.total_score >= min_score]


def run_strategy2_batch(codes: List[str], min_score: int = 3) -> List[StrategyResult]:
    """批量跑策略二，按总分降序返回"""
    s = Strategy2()
    results = []
    for code in codes:
        try:
            time.sleep(0.5)
            r = s.run(code)
            logger.info(f"[策略二] {code}: {r.total_score}/{r.max_score} {r.passed_dims}")
            results.append(r)
        except Exception as e:
            logger.error(f"[策略二] {code} 出错: {e}")
    results.sort(key=lambda x: x.total_score, reverse=True)
    return [r for r in results if r.total_score >= min_score]
