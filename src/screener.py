# -*- coding: utf-8 -*-
"""
===================================
多维度选股模块
===================================

选股10大维度：
1. 缠论结合MACD - 底背驰/买卖点判断
2. 板块涨幅前五 - 热门板块识别
3. 前五板块涨停家数 - 最强板块过滤
4. 形态识别 - 头肩底/V形底
5. 博弈长阳 - 主力控盘信号
6. 强分时 - 开盘强势判断
7. 基本面过滤 - PE合理 + 业绩增长
8. 资金流入 - 主力净流入
9. 均线多头排列 - MA5>MA10>MA20>MA60
10. 成交量连续放大 - 近5日量能递增

用法：
    screener = StockScreener()
    # 先获取热门板块（全局维度）
    await screener.prepare_sector_data()
    # 逐支股票打分
    result = screener.screen_single(code)
"""

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional, Set

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# 数据结构
# ============================================================

@dataclass
class ScreenResult:
    """选股结果"""
    code: str
    name: str = ""

    # 各维度得分（通过=1，不通过=0，跳过=-1）
    score_chan_macd: int = 0        # 缠论+MACD
    score_sector_top5: int = 0      # 板块前五
    score_sector_limitup: int = 0   # 前五板块涨停家数
    score_pattern: int = 0          # 形态识别
    score_battle_long: int = 0      # 博弈长阳
    score_intraday: int = 0         # 强分时
    score_fundamental: int = 0      # 基本面
    score_fund_flow: int = 0        # 资金流入
    score_ma_bull: int = 0          # 均线多头
    score_volume_expand: int = 0    # 量能放大

    # 详细信息
    details: dict = field(default_factory=dict)
    passed_dims: List[str] = field(default_factory=list)
    failed_dims: List[str] = field(default_factory=list)
    skipped_dims: List[str] = field(default_factory=list)

    @property
    def total_score(self) -> int:
        """总分（跳过的维度不计入）"""
        return sum([
            max(0, self.score_chan_macd),
            max(0, self.score_sector_top5),
            max(0, self.score_sector_limitup),
            max(0, self.score_pattern),
            max(0, self.score_battle_long),
            max(0, self.score_intraday),
            max(0, self.score_fundamental),
            max(0, self.score_fund_flow),
            max(0, self.score_ma_bull),
            max(0, self.score_volume_expand),
        ])

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "total_score": self.total_score,
            "scores": {
                "缠论MACD": self.score_chan_macd,
                "板块前五": self.score_sector_top5,
                "前五板块涨停": self.score_sector_limitup,
                "形态识别": self.score_pattern,
                "博弈长阳": self.score_battle_long,
                "强分时": self.score_intraday,
                "基本面": self.score_fundamental,
                "资金流入": self.score_fund_flow,
                "均线多头": self.score_ma_bull,
                "量能放大": self.score_volume_expand,
            },
            "passed_dims": self.passed_dims,
            "failed_dims": self.failed_dims,
            "skipped_dims": self.skipped_dims,
            "details": self.details,
        }


# ============================================================
# 辅助函数
# ============================================================

def _sleep(seconds: float = 0.5):
    """礼貌性延时，避免触发反爬"""
    time.sleep(seconds)


def _calc_macd(close: pd.Series, fast=12, slow=26, signal=9):
    """计算MACD"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    bar = (dif - dea) * 2
    return dif, dea, bar


def _get_stock_history(code: str, days: int = 90) -> Optional[pd.DataFrame]:
    """
    获取股票日线历史数据
    返回 DataFrame，包含 date/open/high/low/close/volume 列
    """
    try:
        import akshare as ak
        # 东方财富接口
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            adjust="qfq",
        )
        if df is None or df.empty:
            return None
        # 统一列名
        df = df.rename(columns={
            "日期": "date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
            "成交额": "amount",
            "涨跌幅": "pct_change",
            "涨跌额": "price_change",
        })
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df.tail(days)
    except Exception as e:
        logger.warning(f"获取{code}历史数据失败: {e}")
        return None


def _get_stock_name(code: str) -> str:
    """获取股票名称"""
    try:
        import akshare as ak
        df = ak.stock_individual_info_em(symbol=code)
        if df is not None and not df.empty:
            name_row = df[df.iloc[:, 0] == "股票简称"]
            if not name_row.empty:
                return str(name_row.iloc[0, 1])
    except Exception:
        pass
    return code


# ============================================================
# 全局缓存（板块数据按天缓存）
# ============================================================

_sector_cache: dict = {
    "top5_names": [],       # 涨幅前5板块名称
    "top5_data": None,      # 前5板块完整数据
    "limitup_sector": "",   # 前5中涨停家数最多的板块
    "timestamp": 0,
    "ttl": 3600,            # 1小时缓存
}


def _get_top5_sectors() -> List[str]:
    """获取今日涨幅前5的行业板块名称"""
    now = time.time()
    if now - _sector_cache["timestamp"] < _sector_cache["ttl"] and _sector_cache["top5_names"]:
        return _sector_cache["top5_names"]

    try:
        import akshare as ak
        # 东方财富行业板块涨跌排行
        df = ak.stock_board_industry_name_em()
        if df is None or df.empty:
            return []
        # 按涨跌幅降序，取前5
        if "涨跌幅" in df.columns:
            df = df.sort_values("涨跌幅", ascending=False)
        elif "change_pct" in df.columns:
            df = df.sort_values("change_pct", ascending=False)
        else:
            # 尝试第一个数值列
            num_cols = df.select_dtypes(include=[float, int]).columns
            if len(num_cols) > 0:
                df = df.sort_values(num_cols[0], ascending=False)

        top5 = df.head(5)
        names = []
        for col in ["板块名称", "name", "行业"]:
            if col in top5.columns:
                names = top5[col].tolist()
                break
        if not names and len(df.columns) > 0:
            names = top5.iloc[:, 0].tolist()

        _sector_cache["top5_names"] = [str(n) for n in names]
        _sector_cache["top5_data"] = top5
        _sector_cache["timestamp"] = now

        logger.info(f"今日涨幅前5板块: {_sector_cache['top5_names']}")
        return _sector_cache["top5_names"]
    except Exception as e:
        logger.warning(f"获取板块数据失败: {e}")
        return []


def _get_limitup_sector_in_top5() -> str:
    """在涨幅前5板块中，找涨停家数最多的板块名称"""
    if _sector_cache["limitup_sector"] and time.time() - _sector_cache["timestamp"] < _sector_cache["ttl"]:
        return _sector_cache["limitup_sector"]

    try:
        import akshare as ak
        top5_names = _get_top5_sectors()
        if not top5_names:
            return ""

        best_sector = ""
        best_count = -1

        for sector_name in top5_names:
            try:
                _sleep(0.3)
                # 获取板块成分股详情
                df = ak.stock_board_industry_cons_em(symbol=sector_name)
                if df is None or df.empty:
                    continue
                # 找涨停列
                limitup_count = 0
                for col in ["涨跌幅", "change_pct", "pct_chg"]:
                    if col in df.columns:
                        limitup_count = int((df[col] >= 9.9).sum())
                        break
                if limitup_count > best_count:
                    best_count = limitup_count
                    best_sector = sector_name
            except Exception as e:
                logger.debug(f"获取板块{sector_name}涨停数据失败: {e}")
                continue

        _sector_cache["limitup_sector"] = best_sector
        logger.info(f"前5板块中涨停家数最多: {best_sector}({best_count}家)")
        return best_sector
    except Exception as e:
        logger.warning(f"获取涨停板块失败: {e}")
        return ""


def _get_stock_sector(code: str) -> str:
    """获取股票所属行业板块"""
    try:
        import akshare as ak
        df = ak.stock_individual_info_em(symbol=code)
        if df is not None and not df.empty:
            for keyword in ["所属行业", "行业", "板块"]:
                row = df[df.iloc[:, 0].str.contains(keyword, na=False)]
                if not row.empty:
                    return str(row.iloc[0, 1])
    except Exception:
        pass
    return ""


# ============================================================
# 主选股类
# ============================================================

class StockScreener:
    """
    多维度股票筛选器

    使用方式：
        screener = StockScreener()
        result = screener.screen_single("600519")
        print(result.total_score, result.passed_dims)
    """

    def screen_single(self, code: str) -> ScreenResult:
        """
        对单支股票进行10维度评分

        Args:
            code: 股票代码（6位数字，不含市场前缀）

        Returns:
            ScreenResult 包含各维度得分和总分
        """
        result = ScreenResult(code=code)

        # 获取基础数据
        df = _get_stock_history(code, days=90)

        # 1. 缠论 + MACD
        result.score_chan_macd = self._check_chan_macd(code, df, result)

        # 2. 板块涨幅前五
        result.score_sector_top5 = self._check_sector_top5(code, result)

        # 3. 前五板块涨停家数最多
        result.score_sector_limitup = self._check_sector_limitup(code, result)

        # 4. 形态识别
        result.score_pattern = self._check_pattern(code, df, result)

        # 5. 博弈长阳（主力控盘）
        result.score_battle_long = self._check_battle_long(code, df, result)

        # 6. 强分时
        result.score_intraday = self._check_intraday(code, result)

        # 7. 基本面
        result.score_fundamental = self._check_fundamental(code, result)

        # 8. 资金流入
        result.score_fund_flow = self._check_fund_flow(code, result)

        # 9. 均线多头排列
        result.score_ma_bull = self._check_ma_bull(code, df, result)

        # 10. 成交量连续放大
        result.score_volume_expand = self._check_volume_expand(code, df, result)

        # 整理通过/未通过维度
        dim_map = {
            "缠论MACD": result.score_chan_macd,
            "板块前五": result.score_sector_top5,
            "前五板块涨停": result.score_sector_limitup,
            "形态识别": result.score_pattern,
            "博弈长阳": result.score_battle_long,
            "强分时": result.score_intraday,
            "基本面": result.score_fundamental,
            "资金流入": result.score_fund_flow,
            "均线多头": result.score_ma_bull,
            "量能放大": result.score_volume_expand,
        }
        for name, score in dim_map.items():
            if score == 1:
                result.passed_dims.append(name)
            elif score == 0:
                result.failed_dims.append(name)
            else:
                result.skipped_dims.append(name)

        return result

    def screen_batch(self, stock_list: List[str], min_score: int = 6) -> List[ScreenResult]:
        """
        批量筛选

        Args:
            stock_list: 股票代码列表
            min_score: 最低总分门槛

        Returns:
            通过筛选的股票列表，按总分降序
        """
        results = []
        # 预加载板块数据（全局一次）
        _get_top5_sectors()
        _get_limitup_sector_in_top5()

        for code in stock_list:
            try:
                _sleep(0.5)
                r = self.screen_single(code)
                if r.total_score >= min_score:
                    results.append(r)
                    logger.info(f"{code} 通过筛选，得分: {r.total_score}")
            except Exception as e:
                logger.error(f"筛选{code}时出错: {e}")
                continue

        results.sort(key=lambda x: x.total_score, reverse=True)
        return results

    # --------------------------------------------------------
    # 维度1：缠论 + MACD
    # --------------------------------------------------------
    def _check_chan_macd(self, code: str, df: Optional[pd.DataFrame], result: ScreenResult) -> int:
        """
        缠论结合MACD判断：
        - 底背驰：价格创新低但MACD绿柱面积缩小
        - 处于二买或三买位置（简化：价格在前低之上且MACD金叉）
        """
        dim = "缠论MACD"
        if df is None or len(df) < 30:
            result.skipped_dims.append(dim)
            return -1

        try:
            close = df["close"]
            dif, dea, bar = _calc_macd(close)

            # 取最近20根K线
            recent = 20
            close_r = close.tail(recent).values
            bar_r = bar.tail(recent).values

            # 底背驰判断：
            # 条件1：近期出现两个低点，第二个低点价格更低
            # 条件2：对应的MACD绿柱面积（负值绝对值）缩小
            passed = False

            # 找近期低点
            lows = []
            for i in range(1, len(close_r) - 1):
                if close_r[i] < close_r[i - 1] and close_r[i] < close_r[i + 1]:
                    lows.append((i, close_r[i], bar_r[i]))

            if len(lows) >= 2:
                p1 = lows[-2]  # 倒数第二个低点
                p2 = lows[-1]  # 最近一个低点
                # 价格新低
                price_new_low = p2[1] < p1[1]
                # MACD绿柱面积缩小（两个低点处的bar值，负值绝对值缩小）
                bar_shrink = abs(p2[2]) < abs(p1[2]) if p1[2] < 0 and p2[2] < 0 else False
                if price_new_low and bar_shrink:
                    passed = True
                    result.details[dim] = "底背驰信号"

            # 备选：MACD金叉（DIF上穿DEA）且价格在MA20以上（二买特征）
            if not passed:
                dif_r = dif.tail(5).values
                dea_r = dea.tail(5).values
                ma20 = close.rolling(20).mean().iloc[-1]
                golden_cross = dif_r[-1] > dea_r[-1] and dif_r[-2] <= dea_r[-2]
                above_ma20 = close.iloc[-1] > ma20
                if golden_cross and above_ma20:
                    passed = True
                    result.details[dim] = "MACD金叉+站上MA20（二买特征）"

            if not passed:
                result.details[dim] = "无缠论买点信号"

            return 1 if passed else 0
        except Exception as e:
            logger.warning(f"{code} 缠论MACD分析失败: {e}")
            result.skipped_dims.append(dim)
            return -1

    # --------------------------------------------------------
    # 维度2：板块涨幅前五
    # --------------------------------------------------------
    def _check_sector_top5(self, code: str, result: ScreenResult) -> int:
        """判断股票是否属于今日涨幅前5的板块"""
        dim = "板块前五"
        try:
            top5_names = _get_top5_sectors()
            if not top5_names:
                result.skipped_dims.append(dim)
                return -1

            stock_sector = _get_stock_sector(code)
            result.details["所属板块"] = stock_sector
            result.details["涨幅前五板块"] = top5_names

            # 模糊匹配（板块名称可能有细微差异）
            for sector in top5_names:
                if sector in stock_sector or stock_sector in sector:
                    result.details[dim] = f"所属板块'{stock_sector}'在涨幅前五"
                    return 1

            result.details[dim] = f"板块'{stock_sector}'未在涨幅前五"
            return 0
        except Exception as e:
            logger.warning(f"{code} 板块前五判断失败: {e}")
            result.skipped_dims.append(dim)
            return -1

    # --------------------------------------------------------
    # 维度3：前五板块涨停家数最多
    # --------------------------------------------------------
    def _check_sector_limitup(self, code: str, result: ScreenResult) -> int:
        """判断股票是否属于前五板块中涨停家数最多的板块"""
        dim = "前五板块涨停"
        try:
            best_sector = _get_limitup_sector_in_top5()
            if not best_sector:
                result.skipped_dims.append(dim)
                return -1

            stock_sector = result.details.get("所属板块", _get_stock_sector(code))
            result.details["涨停最多板块"] = best_sector

            if best_sector in stock_sector or stock_sector in best_sector:
                result.details[dim] = f"属于涨停最多板块'{best_sector}'"
                return 1

            result.details[dim] = f"不属于涨停最多板块'{best_sector}'"
            return 0
        except Exception as e:
            logger.warning(f"{code} 涨停板块判断失败: {e}")
            result.skipped_dims.append(dim)
            return -1

    # --------------------------------------------------------
    # 维度4：形态识别（头肩底/V形底）
    # --------------------------------------------------------
    def _check_pattern(self, code: str, df: Optional[pd.DataFrame], result: ScreenResult) -> int:
        """
        识别头肩底或V形底形态

        头肩底：左肩 < 头 < 右肩（价格），左右肩价格接近，头部是最低点
        V形底：急跌后快速反弹，形成V型
        """
        dim = "形态识别"
        if df is None or len(df) < 30:
            result.skipped_dims.append(dim)
            return -1

        try:
            close = df["close"].values
            n = len(close)

            # === V形底检测 ===
            # 近20根K线内，前半段跌幅>8%，后半段涨幅>8%
            recent = close[-20:]
            mid = len(recent) // 2
            first_half_drop = (recent[:mid].min() - recent[0]) / recent[0] * 100
            second_half_rise = (recent[-1] - recent[mid:].min()) / recent[mid:].min() * 100
            v_bottom = first_half_drop < -8 and second_half_rise > 8

            if v_bottom:
                result.details[dim] = "V形底形态"
                return 1

            # === 头肩底检测 ===
            # 在近60根K线中寻找三个低点：左肩、头（最低）、右肩
            if n < 60:
                window = close
            else:
                window = close[-60:]

            # 找所有局部低点
            local_lows = []
            for i in range(2, len(window) - 2):
                if (window[i] < window[i - 1] and window[i] < window[i - 2] and
                        window[i] < window[i + 1] and window[i] < window[i + 2]):
                    local_lows.append((i, window[i]))

            if len(local_lows) >= 3:
                # 取最近三个低点
                ls = local_lows[-3]  # 左肩
                head = local_lows[-2]  # 头
                rs = local_lows[-1]  # 右肩

                # 头肩底条件：
                # 1. 头部价格最低
                # 2. 左右肩价格接近（差距<5%）
                # 3. 右肩之后价格已开始上涨（突破颈线）
                head_lowest = head[1] < ls[1] and head[1] < rs[1]
                shoulders_close = abs(ls[1] - rs[1]) / ls[1] < 0.05
                neckline = (ls[1] + rs[1]) / 2  # 简化颈线为左右肩均值
                # 右肩之后价格应上穿颈线
                after_rs = window[rs[0]:]
                breakout = len(after_rs) > 0 and after_rs[-1] > neckline * 1.02

                if head_lowest and shoulders_close and breakout:
                    result.details[dim] = "头肩底形态（已突破颈线）"
                    return 1

            result.details[dim] = "未识别到头肩底/V形底"
            return 0
        except Exception as e:
            logger.warning(f"{code} 形态识别失败: {e}")
            result.skipped_dims.append(dim)
            return -1

    # --------------------------------------------------------
    # 维度5：博弈长阳（主力控盘）
    # --------------------------------------------------------
    def _check_battle_long(self, code: str, df: Optional[pd.DataFrame], result: ScreenResult) -> int:
        """
        博弈长阳指标：
        - 在近期下跌趋势末期出现大阳线（涨幅>5%）
        - 且当天成交量是5日均量的2倍以上
        - 说明主力开始建仓控盘
        """
        dim = "博弈长阳"
        if df is None or len(df) < 15:
            result.skipped_dims.append(dim)
            return -1

        try:
            close = df["close"].values
            volume = df["volume"].values
            n = len(close)

            # 检查近20根K线
            window = 20
            start = max(0, n - window)

            for i in range(start + 5, n):
                # 条件1：该K线涨幅>5%（大阳线）
                pct = (close[i] - close[i - 1]) / close[i - 1] * 100
                if pct < 5.0:
                    continue

                # 条件2：成交量是5日均量的2倍以上
                vol_ma5 = np.mean(volume[max(0, i - 5):i])
                if vol_ma5 <= 0 or volume[i] < vol_ma5 * 2:
                    continue

                # 条件3：该K线之前5-10根有下跌趋势
                prev_close = close[max(0, i - 10):i]
                if len(prev_close) >= 5:
                    trend_drop = (prev_close[-1] - prev_close[0]) / prev_close[0] * 100
                    if trend_drop < -3:  # 前期下跌超过3%
                        result.details[dim] = f"博弈长阳出现（涨幅{pct:.1f}%，量比{volume[i]/vol_ma5:.1f}倍）"
                        return 1

            result.details[dim] = "近期未出现博弈长阳信号"
            return 0
        except Exception as e:
            logger.warning(f"{code} 博弈长阳检测失败: {e}")
            result.skipped_dims.append(dim)
            return -1

    # --------------------------------------------------------
    # 维度6：强分时
    # --------------------------------------------------------
    def _check_intraday(self, code: str, result: ScreenResult) -> int:
        """
        强分时判断：
        - 当天开盘30分钟内涨幅>1%
        - 全天价格维持在均价线以上（简化：收盘>开盘，且最低价不低于开盘价的0.99倍）
        """
        dim = "强分时"
        try:
            import akshare as ak
            # 获取今日分时数据（1分钟级别）
            df_min = ak.stock_intraday_em(symbol=code)
            if df_min is None or df_min.empty:
                result.skipped_dims.append(dim)
                return -1

            # 统一列名
            col_map = {
                "时间": "time", "开盘": "open", "最高": "high",
                "最低": "low", "收盘": "close", "成交量": "volume",
                "price": "close", "open": "open", "high": "high",
                "low": "low", "volume": "volume",
            }
            df_min = df_min.rename(columns={k: v for k, v in col_map.items() if k in df_min.columns})

            if "close" not in df_min.columns:
                # 尝试第一个数值列作为价格
                num_cols = df_min.select_dtypes(include=[float, int]).columns
                if len(num_cols) > 0:
                    df_min = df_min.rename(columns={num_cols[0]: "close"})
                else:
                    result.skipped_dims.append(dim)
                    return -1

            prices = df_min["close"].dropna().values
            if len(prices) < 30:
                result.skipped_dims.append(dim)
                return -1

            open_price = prices[0]
            # 开盘30分钟内涨幅（前30根1分钟K线）
            first_30_high = prices[:30].max() if len(prices) >= 30 else prices.max()
            rise_30min = (first_30_high - open_price) / open_price * 100

            # 均价线（累计均价）
            avg_price = np.mean(prices)
            current_price = prices[-1]
            above_avg = current_price >= avg_price

            passed = rise_30min >= 1.0 and above_avg
            result.details[dim] = f"开盘30分钟涨幅{rise_30min:.1f}%，当前{'高于' if above_avg else '低于'}均价"

            return 1 if passed else 0
        except Exception as e:
            logger.warning(f"{code} 强分时判断失败: {e}")
            result.skipped_dims.append(dim)
            return -1

    # --------------------------------------------------------
    # 维度7：基本面过滤
    # --------------------------------------------------------
    def _check_fundamental(self, code: str, result: ScreenResult) -> int:
        """
        基本面过滤：
        - PE > 0 且 PE < 100（合理区间）
        - 近两期净利润同比增长均为正
        - 排除最新季度净利润同比降幅>50%的股票
        """
        dim = "基本面"
        try:
            import akshare as ak

            # 获取实时行情（含PE）
            df_info = ak.stock_individual_info_em(symbol=code)
            pe = None
            if df_info is not None and not df_info.empty:
                for keyword in ["市盈率", "PE", "pe"]:
                    row = df_info[df_info.iloc[:, 0].astype(str).str.contains(keyword, na=False)]
                    if not row.empty:
                        try:
                            pe = float(str(row.iloc[0, 1]).replace(",", "").replace("--", "nan"))
                        except Exception:
                            pe = None
                        break

            # PE合理性检查
            pe_ok = pe is not None and not np.isnan(pe) and 0 < pe < 100
            result.details["PE"] = pe

            # 获取业绩数据（利润增长）
            growth_ok = True  # 默认通过（数据获取失败时不惩罚）
            try:
                _sleep(0.5)
                df_profit = ak.stock_profit_sheet_by_quarterly_em(symbol=code)
                if df_profit is not None and not df_profit.empty:
                    # 找净利润同比增长率列
                    for col in df_profit.columns:
                        col_str = str(col)
                        if "净利润" in col_str and "同比" in col_str:
                            growth_vals = df_profit[col].dropna().head(3).tolist()
                            if len(growth_vals) >= 2:
                                # 近两期均为正增长
                                both_positive = all(float(str(v).replace("%", "").replace(",", "") or 0) > 0
                                                    for v in growth_vals[:2])
                                # 最新期降幅不超过50%
                                latest_growth = float(str(growth_vals[0]).replace("%", "").replace(",", "") or 0)
                                no_big_loss = latest_growth > -50
                                growth_ok = both_positive and no_big_loss
                                result.details["净利润增长近两期"] = [str(v) for v in growth_vals[:2]]
                            break
            except Exception as e:
                logger.debug(f"{code} 业绩数据获取失败: {e}")

            passed = pe_ok and growth_ok
            result.details[dim] = f"PE={pe}, 业绩增长={'正常' if growth_ok else '异常'}"
            return 1 if passed else 0
        except Exception as e:
            logger.warning(f"{code} 基本面判断失败: {e}")
            result.skipped_dims.append(dim)
            return -1

    # --------------------------------------------------------
    # 维度8：资金流入
    # --------------------------------------------------------
    def _check_fund_flow(self, code: str, result: ScreenResult) -> int:
        """
        资金流入：近5日主力净流入为正
        """
        dim = "资金流入"
        try:
            import akshare as ak
            # 个股资金流向
            df_flow = ak.stock_individual_fund_flow(stock=code, market="sh" if code.startswith("6") else "sz")
            if df_flow is None or df_flow.empty:
                result.skipped_dims.append(dim)
                return -1

            # 找主力净流入列
            inflow_col = None
            for col in df_flow.columns:
                if "主力" in str(col) and "净" in str(col):
                    inflow_col = col
                    break

            if inflow_col is None:
                result.skipped_dims.append(dim)
                return -1

            recent5 = df_flow[inflow_col].dropna().head(5).tolist()
            if not recent5:
                result.skipped_dims.append(dim)
                return -1

            # 近5日累计净流入为正
            total_inflow = sum(float(str(v).replace(",", "")) for v in recent5 if str(v) not in ["", "nan"])
            result.details[dim] = f"近5日主力净流入: {total_inflow/1e8:.2f}亿"
            result.details["主力净流入5日"] = total_inflow

            return 1 if total_inflow > 0 else 0
        except Exception as e:
            logger.warning(f"{code} 资金流入判断失败: {e}")
            result.skipped_dims.append(dim)
            return -1

    # --------------------------------------------------------
    # 维度9：均线多头排列
    # --------------------------------------------------------
    def _check_ma_bull(self, code: str, df: Optional[pd.DataFrame], result: ScreenResult) -> int:
        """
        均线多头排列：MA5 > MA10 > MA20 > MA60
        """
        dim = "均线多头"
        if df is None or len(df) < 60:
            result.skipped_dims.append(dim)
            return -1

        try:
            close = df["close"]
            ma5 = close.rolling(5).mean().iloc[-1]
            ma10 = close.rolling(10).mean().iloc[-1]
            ma20 = close.rolling(20).mean().iloc[-1]
            ma60 = close.rolling(60).mean().iloc[-1]

            bull = ma5 > ma10 > ma20 > ma60
            result.details[dim] = f"MA5={ma5:.2f} MA10={ma10:.2f} MA20={ma20:.2f} MA60={ma60:.2f}"
            result.details["MA5"] = round(float(ma5), 2)
            result.details["MA10"] = round(float(ma10), 2)
            result.details["MA20"] = round(float(ma20), 2)
            result.details["MA60"] = round(float(ma60), 2)

            return 1 if bull else 0
        except Exception as e:
            logger.warning(f"{code} 均线多头判断失败: {e}")
            result.skipped_dims.append(dim)
            return -1

    # --------------------------------------------------------
    # 维度10：成交量连续放大
    # --------------------------------------------------------
    def _check_volume_expand(self, code: str, df: Optional[pd.DataFrame], result: ScreenResult) -> int:
        """
        近5个交易日成交量连续放大（至少4/5天递增）
        """
        dim = "量能放大"
        if df is None or len(df) < 6:
            result.skipped_dims.append(dim)
            return -1

        try:
            vol5 = df["volume"].tail(5).values
            # 统计递增天数
            increase_count = sum(1 for i in range(1, len(vol5)) if vol5[i] > vol5[i - 1])
            passed = increase_count >= 4  # 5日中至少4日递增

            vol_trend = " > ".join([f"{v/1e6:.1f}M" for v in vol5])
            result.details[dim] = f"近5日量能: {vol_trend}，递增{increase_count}/4天"

            return 1 if passed else 0
        except Exception as e:
            logger.warning(f"{code} 量能放大判断失败: {e}")
            result.skipped_dims.append(dim)
            return -1
