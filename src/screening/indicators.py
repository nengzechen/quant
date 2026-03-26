# -*- coding: utf-8 -*-
"""
===================================
量化指标模块池 (Indicators Pool)
===================================

所有指标函数统一返回格式：
    {
        "passed": bool,      # 是否通过该指标
        "value": any,        # 计算出的核心数值
        "detail": str,       # 可读描述
    }

模块分类：
    1. 市场情绪模块   - KDJ、涨跌家数比
    2. 板块轮动模块   - Top5板块、板块涨停
    3. 基本面过滤模块 - PE、净利润连续增长
    4. 资金盘口模块   - 高开、强分时、量比、换手率、大单净流入、量能放大
    5. 传统技术模块   - 均线多头、MACD金叉>MA20、KDJ>50、DMI手拉手、头肩底
    6. 缠论特征模块   - 底分型、MACD底背离
    7. 特色主力模块   - 博弈长阳、九五之尊、CYS、CD40
"""

import logging
import threading
import time
from typing import Optional, Dict, Any, List, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ============================================================
# 数据缓存（批量分析时同一股票不重复拉取数据）
# ============================================================
_DF_CACHE: Dict[str, Any] = {}       # key: "code_days", value: {"df": df, "ts": timestamp}
_RT_CACHE: Dict[str, Any] = {}       # key: code, value: {"data": dict, "ts": timestamp}
_CACHE_TTL = 1800                     # 30分钟有效期（日内复用）

# ============================================================
# baostock 会话管理（避免多线程重复 login/logout）
# ============================================================
_BS_LOCK = threading.Lock()
_BS_LOGGED_IN = False

def _bs_ensure_login():
    """确保 baostock 处于登录状态（线程安全，只登录一次）"""
    global _BS_LOGGED_IN
    if _BS_LOGGED_IN:
        return True
    with _BS_LOCK:
        if _BS_LOGGED_IN:
            return True
        try:
            import baostock as bs
            lg = bs.login()
            if lg.error_code == "0":
                _BS_LOGGED_IN = True
                logger.debug("baostock 已登录")
                return True
        except Exception as e:
            logger.warning(f"baostock login 失败: {e}")
    return False

def bs_logout():
    """登出 baostock（Phase1 结束后调用）"""
    global _BS_LOGGED_IN
    if not _BS_LOGGED_IN:
        return
    with _BS_LOCK:
        if not _BS_LOGGED_IN:
            return
        try:
            import baostock as bs
            bs.logout()
            _BS_LOGGED_IN = False
            logger.debug("baostock 已登出")
        except Exception:
            pass

# AKShare 自适应可用性：连续超时超过阈值后自动切换到 baostock
_AKSHARE_OK: Optional[bool] = None   # None=未测试
_AKSHARE_TIMEOUT_COUNT: int = 0      # 连续超时次数
_AKSHARE_TIMEOUT_LIMIT: int = 3      # 超过3次连续超时即关闭 AKShare


def _check_akshare_available() -> bool:
    """
    自适应检测：初次测试一只股票（5秒）；连续超时3次后永久切换到 baostock。
    """
    global _AKSHARE_OK
    if _AKSHARE_OK is False:
        return False
    if _AKSHARE_OK is None:
        try:
            import akshare as ak
            def _test():
                return ak.stock_zh_a_daily(symbol="sh600000", adjust="qfq")
            result = _run_with_timeout(_test, timeout_sec=5)
            _AKSHARE_OK = result is not None and not result.empty
        except Exception:
            _AKSHARE_OK = False
        logger.info(f"[AKShare] {'可达' if _AKSHARE_OK else '不可达，走 baostock'}")
    return bool(_AKSHARE_OK)


def _akshare_report_timeout():
    """AKShare 单次超时时调用；连续超时超限后禁用 AKShare。"""
    global _AKSHARE_OK, _AKSHARE_TIMEOUT_COUNT
    _AKSHARE_TIMEOUT_COUNT += 1
    if _AKSHARE_TIMEOUT_COUNT >= _AKSHARE_TIMEOUT_LIMIT:
        _AKSHARE_OK = False
        logger.warning(f"[AKShare] 连续超时 {_AKSHARE_TIMEOUT_COUNT} 次，已切换到 baostock")


def _akshare_report_success():
    """AKShare 成功一次后重置连续超时计数器。"""
    global _AKSHARE_TIMEOUT_COUNT
    _AKSHARE_TIMEOUT_COUNT = 0


def _df_cache_key(code: str, days: int) -> str:
    return f"{code}_{days}"

def _cache_get(cache: dict, key: str) -> Optional[Any]:
    """从缓存读取，过期返回 None"""
    entry = cache.get(key)
    if entry and time.time() - entry["ts"] < _CACHE_TTL:
        return entry["data"]
    return None

def _cache_set(cache: dict, key: str, data: Any):
    cache[key] = {"data": data, "ts": time.time()}

def clear_data_cache():
    """手动清空缓存（批次结束后可调用）"""
    _DF_CACHE.clear()
    _RT_CACHE.clear()
    _SNAPSHOT_CACHE["df"] = None
    _SNAPSHOT_CACHE["ts"] = 0
    _SNAPSHOT_CACHE["fetched"] = False
    _TENCENT_SNAPSHOT_CACHE["df"] = None
    _TENCENT_SNAPSHOT_CACHE["ts"] = 0
    _SECTOR_CACHE["fetched"] = False
    _SECTOR_CACHE["ts"] = 0
    logger.debug("数据缓存已清空")


# ============================================================
# 全市场快照缓存（全量扫描时只拉一次）
# ============================================================
_SNAPSHOT_CACHE: Dict[str, Any] = {"df": None, "ts": 0, "ttl": 600, "fetched": False}  # 10分钟有效
_TENCENT_SNAPSHOT_CACHE: Dict[str, Any] = {"df": None, "ts": 0}  # 腾讯快照缓存，20分钟有效


def _is_trading_hours() -> bool:
    """简单判断是否在交易时段（9:15-15:30，工作日）"""
    from datetime import datetime
    now = datetime.now()
    if now.weekday() >= 5:  # 周六、周日
        return False
    t = now.hour * 100 + now.minute
    return 915 <= t <= 1530


def get_market_snapshot() -> Optional[Any]:
    """
    获取全市场实时快照（stock_zh_a_spot_em），缓存 10 分钟。
    非交易时段直接跳过（stock_zh_a_spot_em 使用 mini_racer，非交易时段调用会崩溃）。

    返回 DataFrame，包含字段：
      代码、名称、最新价、涨跌幅、成交额、换手率、量比 等
    """
    now = time.time()
    if _SNAPSHOT_CACHE["fetched"] and now - _SNAPSHOT_CACHE["ts"] < _SNAPSHOT_CACHE["ttl"]:
        return _SNAPSHOT_CACHE["df"]
    if not _is_trading_hours():
        logger.debug("[市场快照] 非交易时段，跳过快照拉取")
        _SNAPSHOT_CACHE["fetched"] = True
        _SNAPSHOT_CACHE["ts"] = now
        return None
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        _SNAPSHOT_CACHE["df"] = df
        _SNAPSHOT_CACHE["ts"] = now
        _SNAPSHOT_CACHE["fetched"] = True
        logger.info(f"[市场快照] 已拉取 {len(df)} 只股票实时数据")
        return df
    except Exception as e:
        logger.warning(f"[市场快照] 获取失败: {e}")
        _SNAPSHOT_CACHE["fetched"] = True
        _SNAPSHOT_CACHE["ts"] = now
        return None


def get_market_snapshot_tencent(codes: List[str]) -> Optional[pd.DataFrame]:
    """
    通过腾讯行情批量接口获取全市场快照，替代 stock_zh_a_spot_em。
    每批 200 只，约 28 批 × 0.3s ≈ 8s 完成全量，无需 JS 引擎，收盘后可用。
    字段(~分隔): [3]=当前价 [4]=昨收 [32]=涨跌幅 [37]=成交额(万元) [38]=换手率 [39]=量比
    """
    # 20分钟内复用上次结果（Phase1 连续调用 s1/s2 时只查一次）
    cached = _TENCENT_SNAPSHOT_CACHE.get("df")
    if cached is not None and time.time() - _TENCENT_SNAPSHOT_CACHE.get("ts", 0) < 1200:
        logger.debug("[腾讯快照] 使用缓存")
        return cached

    import requests

    def _tc(code: str) -> str:
        return ("sh" if code.startswith(("6", "5")) else "sz") + code

    rows = []
    hdrs = {"Referer": "https://finance.qq.com", "User-Agent": "Mozilla/5.0"}
    for i in range(0, len(codes), 200):
        batch = codes[i: i + 200]
        try:
            r = requests.get(
                "https://qt.gtimg.cn/q=" + ",".join(_tc(c) for c in batch),
                timeout=10, headers=hdrs,
            )
            for line in r.text.strip().split("\n"):
                if "=" not in line or "~" not in line:
                    continue
                raw = line.split("=", 1)[1].strip().strip('"').strip(";")
                p = raw.split("~")
                if len(p) < 40:
                    continue
                try:
                    cur = float(p[3]) if p[3] else 0.0
                    if cur <= 0:
                        continue
                    prev = float(p[4]) if p[4] else cur
                    pct = float(p[32]) if p[32] else (
                        (cur - prev) / prev * 100 if prev > 0 else 0.0)
                    rows.append({
                        "代码": p[2].zfill(6),
                        "名称": p[1],
                        "最新价": cur,
                        "涨跌幅": round(pct, 2),
                        "成交额": float(p[37]) * 10000 if p[37] else 0.0,
                        "换手率": float(p[38]) if p[38] else 0.0,
                        "量比":   float(p[39]) if len(p) > 39 and p[39] else 1.0,
                    })
                except (ValueError, IndexError):
                    continue
        except Exception as e:
            logger.warning(f"[腾讯快照] 批次 {i // 200 + 1} 失败: {e}")

    if not rows:
        return None
    df = pd.DataFrame(rows)
    _TENCENT_SNAPSHOT_CACHE["df"] = df
    _TENCENT_SNAPSHOT_CACHE["ts"] = time.time()
    logger.info(f"[腾讯快照] 批量获取 {len(df)} 只股票数据")
    return df


def prefilter_from_snapshot(strategy: str = "s1", codes: Optional[List[str]] = None) -> List[str]:
    """
    第一阶段：利用全市场快照做廉价粗筛，一次请求过滤 ~5000 → ~200-500 只。

    策略一（强势突破）筛选条件：
        - 排除北交所（代码以 8 开头）
        - 排除 ST / 退市
        - 最新价 ≥ 5 元（过滤仙股）
        - 成交额 ≥ 1 亿（过滤僵尸股）
        - 换手率 ≥ 2%（有活跃度）
        - 量比 ≥ 1（放量）
        - 涨跌幅 ≥ -8%（非跌停）

    策略二（缠论抄底）筛选条件：
        - 排除北交所 / ST / 退市
        - 最新价 ≥ 3 元
        - 成交额 ≥ 5000 万
        - 涨跌幅 在 [-10%, -1%] 之间（有明显下跌但未跌停）

    Returns:
        符合条件的股票代码列表
    """
    df = get_market_snapshot()
    if (df is None or df.empty) and codes:
        logger.info("[快照预筛] stock_zh_a_spot_em 不可用，切换到腾讯批量接口")
        df = get_market_snapshot_tencent(codes)
    if df is None or df.empty:
        logger.warning("[快照预筛] 快照数据为空（腾讯接口也失败），返回空列表")
        return []

    code_col = "代码" if "代码" in df.columns else df.columns[1]
    name_col = "名称" if "名称" in df.columns else None

    codes_series = df[code_col].astype(str).str.zfill(6)
    mask = pd.Series([True] * len(df), index=df.index)

    # 通用过滤：排除北交所、ST、退市
    mask &= ~codes_series.str.startswith("8")
    if name_col and name_col in df.columns:
        names = df[name_col].astype(str)
        mask &= ~names.str.contains("ST|退市|退", case=False, na=False)

    if strategy == "s2":
        # 策略二：抄底，要求有明显下跌
        if "最新价" in df.columns:
            mask &= pd.to_numeric(df["最新价"], errors="coerce").fillna(0) >= 3.0
        if "成交额" in df.columns:
            mask &= pd.to_numeric(df["成交额"], errors="coerce").fillna(0) >= 5e7
        if "涨跌幅" in df.columns:
            pct = pd.to_numeric(df["涨跌幅"], errors="coerce").fillna(0)
            mask &= pct < -1.0    # 有下跌才值得抄底
            mask &= pct > -10.0   # 跌停不碰
    else:
        # 策略一：强势突破，要求放量活跃
        if "最新价" in df.columns:
            mask &= pd.to_numeric(df["最新价"], errors="coerce").fillna(0) >= 5.0
        if "成交额" in df.columns:
            mask &= pd.to_numeric(df["成交额"], errors="coerce").fillna(0) >= 1e8
        if "换手率" in df.columns:
            mask &= pd.to_numeric(df["换手率"], errors="coerce").fillna(0) >= 2.0
        if "量比" in df.columns:
            mask &= pd.to_numeric(df["量比"], errors="coerce").fillna(0) >= 1.0
        if "涨跌幅" in df.columns:
            mask &= pd.to_numeric(df["涨跌幅"], errors="coerce").fillna(0) >= -8.0

    result = codes_series[mask].tolist()
    logger.info(f"[快照预筛-{strategy}] {len(df)} 只 → 候选 {len(result)} 只")
    return result

# ============================================================
# 类型别名
# ============================================================
IndicatorResult = Dict[str, Any]  # {"passed": bool, "value": any, "detail": str}


def _ok(value=None, detail="") -> IndicatorResult:
    return {"passed": True, "value": value, "detail": detail}


def _fail(value=None, detail="") -> IndicatorResult:
    return {"passed": False, "value": value, "detail": detail}


def _skip(detail="数据不足或接口失败") -> IndicatorResult:
    return {"passed": None, "value": None, "detail": detail}


# ============================================================
# 辅助函数
# ============================================================

def _sleep(s: float = 0.5):
    time.sleep(s)


def _get_daily_df_baostock(code: str, days: int = 100) -> Optional[pd.DataFrame]:
    """baostock 备用数据源（akshare 失败时使用，共享登录会话，线程安全）"""
    if not _bs_ensure_login():
        return None
    try:
        import baostock as bs
        from datetime import date, timedelta
        # baostock 代码格式: sh.600001 / sz.000001
        bs_code = f"sh.{code}" if code.startswith("6") else f"sz.{code}"
        end_date = date.today().strftime("%Y-%m-%d")
        start_dt = date.today() - timedelta(days=max(days * 2, 365))
        start_date = start_dt.strftime("%Y-%m-%d")
        # baostock 共享 socket，需加锁避免并发读写冲突
        with _BS_LOCK:
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,amount,turn,pctChg",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2",   # 前复权
            )
            if rs.error_code != "0":
                return None
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                          "volume", "amount", "turnover", "pct_change"])
        for col in ["open", "high", "low", "close", "volume", "amount", "turnover", "pct_change"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df.tail(days)
    except Exception as e:
        logger.debug(f"baostock 获取{code}日线数据失败: {e}")
        return None


def get_daily_df(code: str, days: int = 100) -> Optional[pd.DataFrame]:
    """获取日线数据（带缓存，akshare 失败自动回退 baostock）"""
    cache_key = _df_cache_key(code, days)
    cached = _cache_get(_DF_CACHE, cache_key)
    if cached is not None:
        logger.debug(f"[cache hit] {code} 日线数据")
        return cached
    # 优先用 akshare sina 源（自适应：连续超时3次后永久切换到 baostock）
    if _check_akshare_available():
        try:
            import akshare as ak
            prefix = "sh" if code.startswith("6") else "sz"

            def _fetch_ak():
                return ak.stock_zh_a_daily(symbol=f"{prefix}{code}", adjust="qfq")

            df = _run_with_timeout(_fetch_ak, timeout_sec=8)
            if df is None:
                _akshare_report_timeout()
                raise ValueError("timeout")
            if df.empty:
                raise ValueError("empty")
            _akshare_report_success()
            # turnover 是小数（0.003），转换为百分比（0.3%）→ 乘以 100
            if "turnover" in df.columns:
                df["turnover"] = df["turnover"] * 100
            # 补充 pct_change 列
            if "pct_change" not in df.columns and "close" in df.columns:
                df["pct_change"] = df["close"].pct_change() * 100
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            result = df.tail(days)
            _cache_set(_DF_CACHE, cache_key, result)
            return result
        except Exception as e:
            logger.debug(f"akshare sina 获取{code}日线数据失败({e})，尝试 baostock")

    # 回退：baostock（需登录，线程安全锁序列化）
    result = _get_daily_df_baostock(code, days)
    if result is not None:
        _cache_set(_DF_CACHE, cache_key, result)
    return result


def get_realtime_info(code: str) -> Optional[Dict]:
    """获取实时行情（带缓存，PE/换手率等）"""
    cached = _cache_get(_RT_CACHE, code)
    if cached is not None:
        logger.debug(f"[cache hit] {code} 实时行情")
        return cached
    try:
        import akshare as ak
        df = _run_with_timeout(
            lambda: ak.stock_individual_info_em(symbol=code),
            timeout_sec=8,
        )
        if df is None or df.empty:
            return None
        result = {}
        for _, row in df.iterrows():
            key = str(row.iloc[0])
            val = row.iloc[1]
            result[key] = val
        _cache_set(_RT_CACHE, code, result)
        return result
    except Exception as e:
        logger.debug(f"获取{code}实时信息失败: {e}")
        return None


def _calc_macd(close: pd.Series, fast=12, slow=26, signal=9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """返回 DIF, DEA, BAR"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    bar = (dif - dea) * 2
    return dif, dea, bar


def _calc_kdj(df: pd.DataFrame, n=9, m1=3, m2=3) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """返回 K, D, J"""
    low_n = df["low"].rolling(n).min()
    high_n = df["high"].rolling(n).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    K = rsv.ewm(com=m1 - 1, adjust=False).mean()
    D = K.ewm(com=m2 - 1, adjust=False).mean()
    J = 3 * K - 2 * D
    return K, D, J


def _calc_dmi(df: pd.DataFrame, n=14, m=6) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """返回 PDI(DI+), MDI(DI-), ADX"""
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(n).mean()

    up_move = high - high.shift()
    down_move = low.shift() - low
    pdm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    mdm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    pdi = pd.Series(pdm, index=df.index).rolling(n).mean() / atr * 100
    mdi = pd.Series(mdm, index=df.index).rolling(n).mean() / atr * 100
    dx = (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan) * 100
    adx = dx.rolling(m).mean()
    return pdi, mdi, adx


# ============================================================
# 1. 市场情绪与环境模块
# ============================================================

def check_kdj_market(df: pd.DataFrame) -> IndicatorResult:
    """
    大盘KDJ判断市场情绪：
    K>50且J>50为多头情绪
    """
    if df is None or len(df) < 9:
        return _skip("数据不足")
    try:
        K, D, J = _calc_kdj(df)
        k_val, d_val, j_val = K.iloc[-1], D.iloc[-1], J.iloc[-1]
        passed = k_val > 50 and j_val > 50
        return (
            _ok({"K": round(k_val, 1), "D": round(d_val, 1), "J": round(j_val, 1)},
                f"KDJ: K={k_val:.1f} D={d_val:.1f} J={j_val:.1f}，{'多头情绪' if passed else '偏弱'}")
            if passed else
            _fail({"K": round(k_val, 1), "D": round(d_val, 1), "J": round(j_val, 1)},
                  f"KDJ: K={k_val:.1f} D={d_val:.1f} J={j_val:.1f}，偏弱")
        )
    except Exception as e:
        return _skip(str(e))


def check_market_breadth() -> IndicatorResult:
    """
    涨跌家数比：上涨家数/下跌家数 > 1 为多头市场
    """
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return _skip("市场行情获取失败")
        up = int((df["涨跌幅"] > 0).sum()) if "涨跌幅" in df.columns else 0
        down = int((df["涨跌幅"] < 0).sum()) if "涨跌幅" in df.columns else 1
        ratio = up / max(down, 1)
        passed = ratio > 1.2  # 上涨家数是下跌家数1.2倍以上
        detail = f"上涨{up}家/下跌{down}家，比值{ratio:.2f}"
        return _ok(ratio, detail) if passed else _fail(ratio, detail)
    except Exception as e:
        return _skip(str(e))


# ============================================================
# 2. 板块轮动模块
# ============================================================

# 缓存：fetched=True 表示已尝试过（无论成功与否），避免非交易时段重复请求
_SECTOR_CACHE: Dict = {"top5": [], "limitup": "", "ts": 0, "ttl": 3600, "fetched": False}


def get_top5_sectors() -> List[str]:
    """获取今日涨幅前5行业板块"""
    if _SECTOR_CACHE["fetched"] and time.time() - _SECTOR_CACHE["ts"] < _SECTOR_CACHE["ttl"]:
        return _SECTOR_CACHE["top5"]
    try:
        import akshare as ak
        df = ak.stock_board_industry_name_em()
        if df is None or df.empty:
            _SECTOR_CACHE["fetched"] = True
            _SECTOR_CACHE["ts"] = time.time()
            return []
        col = next((c for c in ["涨跌幅", "change_pct"] if c in df.columns), None)
        if col:
            df = df.sort_values(col, ascending=False)
        name_col = next((c for c in ["板块名称", "name"] if c in df.columns), df.columns[0])
        top5 = [str(v) for v in df[name_col].head(5).tolist()]
        _SECTOR_CACHE["top5"] = top5
        _SECTOR_CACHE["ts"] = time.time()
        _SECTOR_CACHE["fetched"] = True
        return top5
    except Exception as e:
        logger.warning(f"板块数据失败: {e}")
        _SECTOR_CACHE["fetched"] = True
        _SECTOR_CACHE["ts"] = time.time()
        return []


def get_limitup_sector() -> str:
    """前5板块中涨停家数最多的板块"""
    if _SECTOR_CACHE["fetched"] and time.time() - _SECTOR_CACHE["ts"] < _SECTOR_CACHE["ttl"]:
        return _SECTOR_CACHE["limitup"]
    try:
        import akshare as ak
        top5 = get_top5_sectors()
        best, best_cnt = "", -1
        for name in top5:
            try:
                _sleep(0.3)
                df = ak.stock_board_industry_cons_em(symbol=name)
                col = next((c for c in ["涨跌幅", "change_pct"] if c in df.columns), None)
                cnt = int((df[col] >= 9.9).sum()) if col else 0
                if cnt > best_cnt:
                    best_cnt, best = cnt, name
            except Exception:
                continue
        _SECTOR_CACHE["limitup"] = best
        _SECTOR_CACHE["fetched"] = True
        _SECTOR_CACHE["ts"] = time.time()
        return best
    except Exception as e:
        _SECTOR_CACHE["fetched"] = True
        _SECTOR_CACHE["ts"] = time.time()
        return ""


def get_stock_name(code: str) -> str:
    """获取股票名称（复用 realtime_info 缓存，不额外请求）"""
    try:
        info = get_realtime_info(code)
        if info:
            for k in ["股票简称", "名称", "简称"]:
                if k in info and info[k]:
                    return str(info[k])
    except Exception:
        pass
    return ""


def get_stock_sector(code: str) -> str:
    """获取股票所属行业"""
    try:
        info = get_realtime_info(code)
        if info:
            for k in ["所属行业", "行业", "板块"]:
                if k in info:
                    return str(info[k])
    except Exception:
        pass
    return ""


def check_sector_top5(code: str, sector: str = "") -> IndicatorResult:
    """是否属于今日涨幅前五板块"""
    top5 = get_top5_sectors()
    if not top5:
        return _skip("板块数据获取失败")
    if not sector:
        sector = get_stock_sector(code)
    for s in top5:
        if s in sector or sector in s:
            return _ok(sector, f"所属板块'{sector}'在前五：{top5}")
    return _fail(sector, f"板块'{sector}'不在前五")


def check_sector_limitup(code: str, sector: str = "") -> IndicatorResult:
    """是否属于前五中涨停家数最多的板块"""
    best = get_limitup_sector()
    if not best:
        return _skip("涨停板块获取失败")
    if not sector:
        sector = get_stock_sector(code)
    passed = best in sector or sector in best
    return (
        _ok(best, f"属于涨停最多板块'{best}'")
        if passed else
        _fail(best, f"不属于涨停最多板块'{best}'")
    )


# ============================================================
# 3. 基本面过滤模块
# ============================================================

def check_pe(code: str, pe_max: float = 100) -> IndicatorResult:
    """PE在合理区间 (0, pe_max)"""
    try:
        info = get_realtime_info(code)
        pe = None
        if info:
            for k in ["市盈率(动态)", "市盈率", "PE", "pe"]:
                if k in info:
                    try:
                        pe = float(str(info[k]).replace(",", "").replace("--", "nan"))
                    except Exception:
                        pass
                    break
        if pe is None or np.isnan(pe):
            return _skip(f"PE数据获取失败")
        passed = 0 < pe < pe_max
        return (
            _ok(pe, f"PE={pe:.1f}，在合理区间(0,{pe_max})")
            if passed else
            _fail(pe, f"PE={pe:.1f}，超出范围")
        )
    except Exception as e:
        return _skip(str(e))


def check_profit_growth(code: str) -> IndicatorResult:
    """净利润连续增长（近两期同比均为正，最新期降幅不超过50%）
    注：财务数据来自 eastmoney，非交易时段/无法访问时返回 skip
    """
    # eastmoney 财务接口使用 mini_racer，非交易时段不调用（防止 V8 崩溃）
    if not _is_trading_hours():
        return _skip("非交易时段跳过财务数据请求")
    try:
        import akshare as ak
        _sleep(0.5)
        df = ak.stock_profit_sheet_by_quarterly_em(symbol=code)
        if df is None or df.empty:
            return _skip("财务数据获取失败")
        growth_col = next((c for c in df.columns if "净利润" in str(c) and "同比" in str(c)), None)
        if not growth_col:
            return _skip("未找到净利润同比列")
        vals = df[growth_col].dropna().head(3).tolist()
        if len(vals) < 2:
            return _skip("数据期数不足")
        growths = []
        for v in vals[:2]:
            try:
                growths.append(float(str(v).replace("%", "").replace(",", "")))
            except Exception:
                growths.append(0.0)
        both_positive = all(g > 0 for g in growths)
        no_crash = growths[0] > -50
        passed = both_positive and no_crash
        detail = f"近两期净利润增速: {growths[0]:.1f}%/{growths[1]:.1f}%"
        return _ok(growths, detail) if passed else _fail(growths, detail)
    except Exception as e:
        return _skip(str(e))


# ============================================================
# 4. 资金与盘口模块
# ============================================================

def check_high_open(df: pd.DataFrame) -> IndicatorResult:
    """高开：今日开盘价高于昨日收盘价0.5%以上"""
    if df is None or len(df) < 2:
        return _skip()
    try:
        today = df.iloc[-1]
        yesterday_close = df.iloc[-2]["close"]
        open_pct = (today["open"] - yesterday_close) / yesterday_close * 100
        passed = open_pct >= 0.5
        return (
            _ok(open_pct, f"高开{open_pct:.2f}%")
            if passed else
            _fail(open_pct, f"开盘涨跌{open_pct:.2f}%，未高开")
        )
    except Exception as e:
        return _skip(str(e))


def _run_with_timeout(fn, timeout_sec: int = 10):
    """在子线程中执行 fn，超时后返回 None（防止 AKShare 无限阻塞）。"""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
    with ThreadPoolExecutor(max_workers=1) as exe:
        fut = exe.submit(fn)
        try:
            return fut.result(timeout=timeout_sec)
        except (FTimeout, Exception):
            return None


def check_intraday_strong(code: str) -> IndicatorResult:
    """
    强分时：开盘30分钟涨幅>1%，且当前价格高于均价
    """
    try:
        import akshare as ak
        df_min = _run_with_timeout(lambda: ak.stock_intraday_em(symbol=code), timeout_sec=10)
        if df_min is None or df_min.empty:
            return _skip("分时数据获取失败")
        # 找价格列
        price_col = next((c for c in ["收盘", "close", "price", "最新价"] if c in df_min.columns), None)
        if price_col is None:
            num_cols = df_min.select_dtypes(include=[float, int]).columns
            price_col = num_cols[0] if len(num_cols) > 0 else None
        if price_col is None:
            return _skip("无法识别价格列")
        prices = df_min[price_col].dropna().values
        if len(prices) < 30:
            return _skip("分时数据不足")
        open_p = prices[0]
        rise_30 = (prices[:30].max() - open_p) / open_p * 100
        avg_p = np.mean(prices)
        above_avg = prices[-1] >= avg_p
        passed = rise_30 >= 1.0 and above_avg
        detail = f"开盘30分涨幅{rise_30:.1f}%，当前{'高于' if above_avg else '低于'}均价"
        return _ok(rise_30, detail) if passed else _fail(rise_30, detail)
    except Exception as e:
        return _skip(str(e))


def check_volume_ratio(df: pd.DataFrame, threshold: float = 1.0) -> IndicatorResult:
    """量比：当日成交量/5日均量 > threshold"""
    if df is None or len(df) < 6:
        return _skip()
    try:
        vol_ma5 = df["volume"].iloc[-6:-1].mean()
        today_vol = df["volume"].iloc[-1]
        ratio = today_vol / vol_ma5 if vol_ma5 > 0 else 0
        passed = ratio > threshold
        detail = f"量比={ratio:.2f}（阈值>{threshold}）"
        return _ok(ratio, detail) if passed else _fail(ratio, detail)
    except Exception as e:
        return _skip(str(e))


def check_turnover(df: pd.DataFrame, threshold: float = 3.0) -> IndicatorResult:
    """换手率 > threshold%"""
    if df is None or len(df) < 1:
        return _skip()
    try:
        if "turnover" not in df.columns:
            return _skip("无换手率数据")
        turnover = df["turnover"].iloc[-1]
        passed = turnover >= threshold
        detail = f"换手率={turnover:.2f}%（阈值>{threshold}%）"
        return _ok(turnover, detail) if passed else _fail(turnover, detail)
    except Exception as e:
        return _skip(str(e))


def check_fund_flow(code: str) -> IndicatorResult:
    """近5日主力净流入为正"""
    try:
        import akshare as ak
        market = "sh" if code.startswith("6") else "sz"
        df = _run_with_timeout(
            lambda: ak.stock_individual_fund_flow(stock=code, market=market),
            timeout_sec=10,
        )
        if df is None or df.empty:
            return _skip("资金流向获取失败")
        col = next((c for c in df.columns if "主力" in str(c) and "净" in str(c)), None)
        if col is None:
            return _skip("未找到主力净流入列")
        vals = df[col].dropna().head(5).tolist()
        total = sum(float(str(v).replace(",", "")) for v in vals if str(v) not in ["", "nan"])
        passed = total > 0
        detail = f"近5日主力净流入: {total/1e8:.2f}亿"
        return _ok(total, detail) if passed else _fail(total, detail)
    except Exception as e:
        return _skip(str(e))


def check_volume_expand(df: pd.DataFrame, days: int = 5, min_days: int = 4) -> IndicatorResult:
    """近N日成交量连续放大（至少min_days天递增）"""
    if df is None or len(df) < days + 1:
        return _skip()
    try:
        vols = df["volume"].tail(days).values
        inc = sum(1 for i in range(1, len(vols)) if vols[i] > vols[i - 1])
        passed = inc >= min_days
        detail = f"近{days}日量能递增{inc}/{days - 1}天"
        return _ok(inc, detail) if passed else _fail(inc, detail)
    except Exception as e:
        return _skip(str(e))


# ============================================================
# 4b. 实时行情（腾讯接口）— Phase2 盘中专用
# ============================================================

def get_realtime_quote_tencent(code: str) -> dict:
    """
    通过腾讯行情接口获取单只股票实时行情。
    字段索引（~分隔）：
      [1]=名称 [2]=代码 [3]=当前价 [4]=昨收 [5]=今开
      [6]=成交量(手) [37]=成交金额(元) [38]=换手率(%)
    返回空 dict 表示获取失败。
    """
    import requests
    try:
        market = "sh" if code.startswith(("6", "5")) else "sz"
        url = f"https://qt.gtimg.cn/q={market}{code}"
        r = requests.get(url, timeout=5, headers={"Referer": "https://finance.qq.com"})
        for line in r.text.strip().split("\n"):
            if "=" not in line:
                continue
            raw = line.split("=", 1)[1].strip().strip('"').strip(";")
            parts = raw.split("~")
            if len(parts) < 39:
                continue
            current = float(parts[3]) if parts[3] else 0.0
            prev_close = float(parts[4]) if parts[4] else 0.0
            today_open = float(parts[5]) if parts[5] else 0.0
            vol_lot = float(parts[6]) if parts[6] else 0.0
            turnover_rate = float(parts[38]) if parts[38] else 0.0
            if current > 0:
                return {
                    "code": parts[2],
                    "current_price": current,
                    "prev_close": prev_close,
                    "today_open": today_open,
                    "volume_lot": vol_lot,
                    "turnover_rate": turnover_rate,
                }
    except Exception as e:
        logger.debug(f"[RT quote] {code} 获取失败: {e}")
    return {}


def check_high_open_rt(quote: dict) -> IndicatorResult:
    """高开（实时）：今日开盘价高于昨日收盘价 0.5% 以上"""
    try:
        today_open = quote.get("today_open", 0.0)
        prev_close = quote.get("prev_close", 0.0)
        if today_open <= 0 or prev_close <= 0:
            return _skip("实时行情数据不足")
        open_pct = (today_open - prev_close) / prev_close * 100
        passed = open_pct >= 0.5
        return (
            _ok(round(open_pct, 2), f"高开{open_pct:.2f}%")
            if passed else
            _fail(round(open_pct, 2), f"开盘涨跌{open_pct:.2f}%，未高开")
        )
    except Exception as e:
        return _skip(str(e))


def check_volume_ratio_rt(
    quote: dict, df: pd.DataFrame, threshold: float = 1.0
) -> IndicatorResult:
    """
    量比（实时，按时间比例折算）：
      量比 = 当前成交量 / (5日均量 * 已交易时间占比)
    """
    try:
        if df is None or len(df) < 6:
            return _skip("历史数据不足")
        vol_ma5 = df["volume"].iloc[-6:-1].mean()
        if vol_ma5 <= 0:
            return _skip("5日均量为0")

        current_vol = quote.get("volume_lot", 0.0)
        if current_vol <= 0:
            return _skip("当前成交量为0")

        from datetime import datetime, timezone, timedelta
        # 使用 CST (UTC+8) 时间计算 A股交易时段
        cst = timezone(timedelta(hours=8))
        now = datetime.now(cst)
        # 交易时段：09:30-11:30(120min) + 13:00-15:00(120min) = 240min
        open_dt = now.replace(hour=9, minute=30, second=0, microsecond=0)
        break_start = now.replace(hour=11, minute=30, second=0, microsecond=0)
        break_end = now.replace(hour=13, minute=0, second=0, microsecond=0)
        close_dt = now.replace(hour=15, minute=0, second=0, microsecond=0)
        total_minutes = 240
        if now <= open_dt:
            elapsed = 1
        elif now <= break_start:
            elapsed = (now - open_dt).total_seconds() / 60
        elif now <= break_end:
            elapsed = 120
        elif now <= close_dt:
            elapsed = 120 + (now - break_end).total_seconds() / 60
        else:
            elapsed = 240
        elapsed = max(1.0, min(elapsed, total_minutes))
        time_ratio = elapsed / total_minutes

        expected_vol = vol_ma5 * time_ratio
        ratio = current_vol / expected_vol if expected_vol > 0 else 0.0
        passed = ratio > threshold
        detail = f"量比={ratio:.2f}（已交易{elapsed:.0f}分钟，阈值>{threshold}）"
        return _ok(round(ratio, 2), detail) if passed else _fail(round(ratio, 2), detail)
    except Exception as e:
        return _skip(str(e))


def check_turnover_rt(quote: dict, threshold: float = 3.0) -> IndicatorResult:
    """换手率（实时）> threshold%"""
    try:
        turnover = quote.get("turnover_rate", 0.0)
        if turnover <= 0:
            return _skip("换手率数据不可用")
        passed = turnover >= threshold
        detail = f"换手率={turnover:.2f}%（阈值>{threshold}%）"
        return _ok(round(turnover, 2), detail) if passed else _fail(round(turnover, 2), detail)
    except Exception as e:
        return _skip(str(e))


# ============================================================
# 5. 传统技术与形态模块
# ============================================================

def check_ma_bull(df: pd.DataFrame) -> IndicatorResult:
    """均线多头排列：MA5 > MA10 > MA20 > MA60"""
    if df is None or len(df) < 60:
        return _skip("数据不足60日")
    try:
        c = df["close"]
        ma5 = c.rolling(5).mean().iloc[-1]
        ma10 = c.rolling(10).mean().iloc[-1]
        ma20 = c.rolling(20).mean().iloc[-1]
        ma60 = c.rolling(60).mean().iloc[-1]
        passed = ma5 > ma10 > ma20 > ma60
        detail = f"MA5={ma5:.2f} MA10={ma10:.2f} MA20={ma20:.2f} MA60={ma60:.2f}"
        return (
            _ok({"ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60}, detail)
            if passed else
            _fail({"ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60}, detail)
        )
    except Exception as e:
        return _skip(str(e))


def check_macd_golden_above_ma20(df: pd.DataFrame) -> IndicatorResult:
    """
    MACD金叉且价格>MA20：
    - DIF上穿DEA（金叉）
    - 当前收盘价 > MA20
    """
    if df is None or len(df) < 26:
        return _skip()
    try:
        c = df["close"]
        dif, dea, bar = _calc_macd(c)
        ma20 = c.rolling(20).mean().iloc[-1]
        golden = dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2]
        above_ma20 = c.iloc[-1] > ma20
        passed = golden and above_ma20
        detail = (f"MACD{'金叉' if golden else '未金叉'}，"
                  f"收盘{'>' if above_ma20 else '<'}MA20({ma20:.2f})")
        return _ok({"dif": round(dif.iloc[-1], 4), "dea": round(dea.iloc[-1], 4)}, detail) \
            if passed else _fail(None, detail)
    except Exception as e:
        return _skip(str(e))


def check_kdj_above50(df: pd.DataFrame) -> IndicatorResult:
    """KDJ三线均>50"""
    if df is None or len(df) < 9:
        return _skip()
    try:
        K, D, J = _calc_kdj(df)
        k, d, j = K.iloc[-1], D.iloc[-1], J.iloc[-1]
        passed = k > 50 and d > 50 and j > 50
        detail = f"K={k:.1f} D={d:.1f} J={j:.1f}"
        return _ok({"K": k, "D": d, "J": j}, detail) if passed else _fail({"K": k, "D": d, "J": j}, detail)
    except Exception as e:
        return _skip(str(e))


def check_dmi(df: pd.DataFrame) -> IndicatorResult:
    """
    DMI手拉手：
    - PDI > MDI（多头占优）
    - ADX > 20（趋势确立）
    - PDI和ADX均在上升
    """
    if df is None or len(df) < 20:
        return _skip()
    try:
        pdi, mdi, adx = _calc_dmi(df)
        p, m, a = pdi.iloc[-1], mdi.iloc[-1], adx.iloc[-1]
        p_prev, a_prev = pdi.iloc[-2], adx.iloc[-2]
        hand_in_hand = p > m and a > 20 and p > p_prev and a > a_prev
        detail = f"PDI={p:.1f} MDI={m:.1f} ADX={a:.1f}，{'手拉手↑' if hand_in_hand else '未达标'}"
        return _ok({"pdi": p, "mdi": m, "adx": a}, detail) if hand_in_hand else _fail(None, detail)
    except Exception as e:
        return _skip(str(e))


def check_head_shoulder_bottom(df: pd.DataFrame) -> IndicatorResult:
    """头肩底形态：左肩→头（最低）→右肩，右肩后突破颈线"""
    if df is None or len(df) < 30:
        return _skip()
    try:
        window = df["close"].tail(60).values
        lows = [(i, window[i]) for i in range(2, len(window) - 2)
                if window[i] < window[i-1] and window[i] < window[i-2]
                and window[i] < window[i+1] and window[i] < window[i+2]]
        if len(lows) < 3:
            return _fail(None, "低点不足，未识别头肩底")
        ls, head, rs = lows[-3], lows[-2], lows[-1]
        head_lowest = head[1] < ls[1] and head[1] < rs[1]
        shoulders_close = abs(ls[1] - rs[1]) / ls[1] < 0.05
        neckline = (ls[1] + rs[1]) / 2
        breakout = window[rs[0]:].max() > neckline * 1.02 if rs[0] < len(window) - 1 else False
        passed = head_lowest and shoulders_close and breakout
        detail = f"{'头肩底已突破颈线' if passed else '未完整识别头肩底'}"
        return _ok(neckline, detail) if passed else _fail(None, detail)
    except Exception as e:
        return _skip(str(e))


# ============================================================
# 6. 缠论特征模块
# ============================================================

def check_chan_bottom_pattern(df: pd.DataFrame) -> IndicatorResult:
    """
    底分型：三根K线，中间K线的最低价低于两侧（底分型结构）
    近期5根K线中出现底分型即通过
    """
    if df is None or len(df) < 5:
        return _skip()
    try:
        highs = df["high"].tail(10).values
        lows = df["low"].tail(10).values
        found = False
        for i in range(1, len(lows) - 1):
            if (lows[i] < lows[i-1] and lows[i] < lows[i+1]
                    and highs[i] < highs[i-1] and highs[i] < highs[i+1]):
                found = True
        detail = "近期出现底分型" if found else "近期无底分型"
        return _ok(True, detail) if found else _fail(False, detail)
    except Exception as e:
        return _skip(str(e))


def check_macd_divergence(df: pd.DataFrame, use_weekly: bool = False) -> IndicatorResult:
    """
    MACD底背离：价格创新低但MACD绿柱面积缩小（底背驰）
    use_weekly=True时判断是否也有周线背离（取日线最后30根模拟周线）
    """
    if df is None or len(df) < 30:
        return _skip()
    try:
        close = df["close"]
        dif, dea, bar = _calc_macd(close)
        n = 20
        close_r = close.tail(n).values
        bar_r = bar.tail(n).values

        # 找低点
        lows = [(i, close_r[i], bar_r[i])
                for i in range(1, len(close_r) - 1)
                if close_r[i] < close_r[i-1] and close_r[i] < close_r[i+1]]

        if len(lows) < 2:
            return _fail(None, "低点不足，无法判断背离")

        p1, p2 = lows[-2], lows[-1]
        price_new_low = p2[1] < p1[1]
        bar_shrink = (p1[2] < 0 and p2[2] < 0 and abs(p2[2]) < abs(p1[2]))
        passed = price_new_low and bar_shrink
        detail = (f"{'底背驰' if passed else '无背离'}：价格{'新低' if price_new_low else '未新低'}，"
                  f"MACD绿柱{'收缩' if bar_shrink else '未收缩'}")
        return _ok(True, detail) if passed else _fail(False, detail)
    except Exception as e:
        return _skip(str(e))


# ============================================================
# 7. 特色主力控盘模块
# ============================================================

def check_battle_long(df: pd.DataFrame) -> IndicatorResult:
    """
    博弈长阳：下跌末期出现大阳线（涨幅>5%）且量比>2倍
    """
    if df is None or len(df) < 15:
        return _skip()
    try:
        close = df["close"].values
        volume = df["volume"].values
        n = len(close)
        found = False
        detail = "近期未出现博弈长阳"
        for i in range(max(0, n - 20) + 5, n):
            pct = (close[i] - close[i-1]) / close[i-1] * 100
            if pct < 5.0:
                continue
            vol_ma5 = np.mean(volume[max(0, i-5):i])
            if vol_ma5 <= 0 or volume[i] < vol_ma5 * 2:
                continue
            prev = close[max(0, i-10):i]
            if len(prev) >= 5 and (prev[-1] - prev[0]) / prev[0] * 100 < -3:
                found = True
                detail = f"博弈长阳：涨幅{pct:.1f}%，量比{volume[i]/vol_ma5:.1f}倍"
                break
        return _ok(True, detail) if found else _fail(False, detail)
    except Exception as e:
        return _skip(str(e))


def check_jiuyu_zhizun(df: pd.DataFrame) -> IndicatorResult:
    """
    九五之尊：价格站上MA9和MA5，且近期连续3根阳线
    （主力拉升初期特征）
    """
    if df is None or len(df) < 9:
        return _skip()
    try:
        c = df["close"]
        o = df["open"]
        ma5 = c.rolling(5).mean().iloc[-1]
        ma9 = c.rolling(9).mean().iloc[-1]
        above = c.iloc[-1] > ma5 and c.iloc[-1] > ma9
        # 近3根阳线
        yang3 = all(c.iloc[i] > o.iloc[i] for i in [-3, -2, -1])
        passed = above and yang3
        detail = (f"价格{'高于' if above else '低于'}MA5({ma5:.2f})/MA9({ma9:.2f})，"
                  f"近3根{'均为阳线' if yang3 else '非全阳'}")
        return _ok(True, detail) if passed else _fail(False, detail)
    except Exception as e:
        return _skip(str(e))


def check_cys(df: pd.DataFrame, threshold: float = -15.0) -> IndicatorResult:
    """
    CYS市场盈亏指标（近似）：
    (收盘价 - MA40) / MA40 * 100 < threshold 视为深度超跌
    CYS < -15 → 价格比40日均线低15%以上，极度超跌
    """
    if df is None or len(df) < 40:
        return _skip("数据不足40日")
    try:
        c = df["close"]
        ma40 = c.rolling(40).mean().iloc[-1]
        cys = (c.iloc[-1] - ma40) / ma40 * 100
        passed = cys < threshold
        detail = f"CYS={cys:.2f}（阈值<{threshold}，{'深度超跌' if passed else '未超跌'}）"
        return _ok(cys, detail) if passed else _fail(cys, detail)
    except Exception as e:
        return _skip(str(e))


def check_cd40(df: pd.DataFrame, threshold: float = -20.0) -> IndicatorResult:
    """
    CD40动量指标（近似）：
    40日价格变化率 = (今日收盘 - 40日前收盘) / 40日前收盘 * 100
    CD40 < -20 → 40日内跌幅超过20%，处于深度下跌动量区
    """
    if df is None or len(df) < 41:
        return _skip("数据不足41日")
    try:
        c = df["close"]
        cd40 = (c.iloc[-1] - c.iloc[-41]) / c.iloc[-41] * 100
        passed = cd40 < threshold
        detail = f"CD40={cd40:.2f}%（阈值<{threshold}%，{'深度下跌动量' if passed else '未达标'}）"
        return _ok(cd40, detail) if passed else _fail(cd40, detail)
    except Exception as e:
        return _skip(str(e))


def check_cys_rising(df: pd.DataFrame, threshold: float = -15.0) -> IndicatorResult:
    """
    CYS<阈值 且 开始上行：
    - 当前 CYS < threshold（深度超跌）
    - CYS 近 5 日处于上升趋势（当前 > 5 日前）
    适用于抄底模型：超跌后开始修复
    """
    if df is None or len(df) < 45:
        return _skip("数据不足")
    try:
        c = df["close"]
        ma40 = c.rolling(40).mean()
        cys = (c - ma40) / ma40 * 100
        cur = cys.iloc[-1]
        prev = cys.iloc[-6]
        oversold = cur < threshold
        rising = cur > prev
        passed = oversold and rising
        detail = (f"CYS={cur:.2f}（阈值<{threshold}），"
                  f"{'上行↑' if rising else '下行↓'}（5日前={prev:.2f}）")
        return _ok(cur, detail) if passed else _fail(cur, detail)
    except Exception as e:
        return _skip(str(e))


def check_cys_positive(df: pd.DataFrame, threshold: float = 9.5) -> IndicatorResult:
    """
    CYS13 > 阈值（指南针指标强势区）：
    (收盘价 - MA40) / MA40 * 100 > threshold
    CYS13 > 9.5 → 价格比40日均线高9.5%，处于强势区
    """
    if df is None or len(df) < 40:
        return _skip("数据不足40日")
    try:
        c = df["close"]
        ma40 = c.rolling(40).mean().iloc[-1]
        cys = (c.iloc[-1] - ma40) / ma40 * 100
        passed = cys > threshold
        detail = f"CYS13={cys:.2f}（阈值>{threshold}，{'强势区' if passed else '未达标'}）"
        return _ok(cys, detail) if passed else _fail(cys, detail)
    except Exception as e:
        return _skip(str(e))


def check_kdj_cross(df: pd.DataFrame) -> IndicatorResult:
    """
    KDJ金叉：近5根K线内 K 上穿 D，且 J > 0
    适用于抄底/波段模型：动能启动信号
    """
    if df is None or len(df) < 15:
        return _skip()
    try:
        K, D, J = _calc_kdj(df)
        j = J.iloc[-1]
        j_positive = j > 0
        # 近5根内 K 上穿 D
        cross = False
        for i in range(-5, 0):
            if K.iloc[i] > D.iloc[i] and K.iloc[i - 1] <= D.iloc[i - 1]:
                cross = True
                break
        passed = cross and j_positive
        k, d = K.iloc[-1], D.iloc[-1]
        detail = (f"K={k:.1f} D={d:.1f} J={j:.1f}，"
                  f"{'近期金叉' if cross else '未金叉'}，J{'>' if j_positive else '<'}0")
        return _ok({"K": k, "D": d, "J": j}, detail) if passed else _fail(None, detail)
    except Exception as e:
        return _skip(str(e))


def check_kdj_above90(df: pd.DataFrame) -> IndicatorResult:
    """
    KDJ强势：J > 90（或 K、D 均 > 80）表示强势动能
    适用于强势模型
    """
    if df is None or len(df) < 9:
        return _skip()
    try:
        K, D, J = _calc_kdj(df)
        k, d, j = K.iloc[-1], D.iloc[-1], J.iloc[-1]
        passed = j > 90 or (k > 80 and d > 80)
        detail = f"K={k:.1f} D={d:.1f} J={j:.1f}（{'强势' if passed else '未达强势'}）"
        return _ok({"K": k, "D": d, "J": j}, detail) if passed else _fail({"K": k, "D": d, "J": j}, detail)
    except Exception as e:
        return _skip(str(e))


def check_dmi_strong(df: pd.DataFrame) -> IndicatorResult:
    """
    DMI强势手拉手（适用于强势模型）：
    - PDI > MDI（多头占优）
    - ADX > 25
    - MDI > 25（双方力量均强，但多头占优，高波动强趋势）
    - PDI 和 ADX 均在上升
    """
    if df is None or len(df) < 20:
        return _skip()
    try:
        pdi, mdi, adx = _calc_dmi(df)
        p, m, a = pdi.iloc[-1], mdi.iloc[-1], adx.iloc[-1]
        p_prev, a_prev = pdi.iloc[-2], adx.iloc[-2]
        passed = (p > m and a > 25 and m > 25 and p > p_prev and a > a_prev)
        detail = f"PDI={p:.1f} MDI={m:.1f} ADX={a:.1f}，{'强势手拉手' if passed else '未达标'}"
        return _ok({"pdi": p, "mdi": m, "adx": a}, detail) if passed else _fail(None, detail)
    except Exception as e:
        return _skip(str(e))
