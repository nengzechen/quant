# -*- coding: utf-8 -*-
"""
Phase1：收盘后离线运行（全市场 → 种子池）

执行顺序：
  1. 获取全量 A 股代码（ak.stock_info_a_code_name，一次请求）
  2. 过滤 ST / 北交所 / 退市，得到候选总池
  3. 用 prefilter_from_snapshot 分两路候选池（活跃股 / 超跌股）
     - 如快照拉取失败（非交易时段），直接使用全量代码
  4. 三个模型并发评分（BottomSwing 用超跌池；StrongTrend+LimitUpHunter 用活跃池）
  5. 各模型用自己的 is_qualified_seed() 判断是否进种子池
  6. 合并去重（同一股票取最高分模型）
  7. 截取 top N，保存到 data/seed_pool_YYYYMMDD.json

使用方式：
    python main.py --phase1
    python main.py --phase1 --phase1-target 100
"""
import logging
import time
from datetime import datetime
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


def _get_board_sector(code: str) -> str:
    """
    根据股票代码推断市场板块（不需要额外 API 请求）：
    6xxxxx → 沪主板；688xxx → 科创板；
    300xxx/301xxx → 创业板；002xxx/003xxx → 深中小板；
    000xxx/001xxx → 深主板；其余 → 其他
    """
    if code.startswith("688"):
        return "科创板"
    if code.startswith("6"):
        return "沪主板"
    if code.startswith("300") or code.startswith("301"):
        return "创业板"
    if code.startswith("002") or code.startswith("003"):
        return "深中小板"
    if code.startswith("000") or code.startswith("001"):
        return "深主板"
    return "其他"


def _select_top5_per_sector(entries: List, target_count: int = 100, top_n: int = 5) -> List:
    """
    按行业板块分组，每个板块取 top_n 只（按得分排序）。
    - 优先使用 SeedEntry 上已缓存的行业信息
    - 回退到按代码前缀推断市场板块
    - 不足 target_count 时按全局得分补足
    """
    from src.screening.indicators import get_stock_sector

    # 为每只股票打上板块标签
    sector_map: Dict[str, List] = {}
    for entry in entries:
        # 尝试从行业缓存取
        sector = ""
        try:
            sector = get_stock_sector(entry.code) or ""
        except Exception:
            pass
        if not sector:
            sector = _get_board_sector(entry.code)
        sector_map.setdefault(sector, []).append(entry)

    # 每个板块按得分降序取 top_n
    selected = []
    for sector, group in sorted(sector_map.items()):
        group_sorted = sorted(group, key=lambda x: x.phase1_score, reverse=True)
        selected.extend(group_sorted[:top_n])
        logger.info(f"[Phase1] 板块「{sector}」{len(group)} 只 → 取 {min(len(group), top_n)} 只")

    # 去重（同一支股票可能因同名板块重复）
    seen = set()
    unique = []
    for e in sorted(selected, key=lambda x: x.phase1_score, reverse=True):
        if e.code not in seen:
            seen.add(e.code)
            unique.append(e)

    # 不足 target_count 时，从剩余未入选股票按得分补足
    if len(unique) < target_count:
        selected_codes = {e.code for e in unique}
        remaining = sorted(
            [e for e in entries if e.code not in selected_codes],
            key=lambda x: x.phase1_score, reverse=True
        )
        unique.extend(remaining[:target_count - len(unique)])

    return unique


def _fetch_all_a_codes() -> List[str]:
    """
    获取全量 A 股代码列表（一次请求，约 5500 只）。
    过滤：北交所（8开头）、ST / 退市（名称含 ST 或"退"）。
    """
    try:
        import akshare as ak
        df = ak.stock_info_a_code_name()
        if df is None or df.empty:
            logger.warning("[Phase1] 获取全量代码失败，返回空列表")
            return []

        codes = []
        for _, row in df.iterrows():
            code = str(row.get("code", "")).zfill(6)
            name = str(row.get("name", ""))
            if code.startswith("8"):
                continue
            if "ST" in name.upper() or "退" in name:
                continue
            codes.append(code)

        logger.info(f"[Phase1] 全量代码：{len(df)} 只 → 过滤后 {len(codes)} 只")
        return codes
    except Exception as e:
        logger.error(f"[Phase1] 获取全量代码失败: {e}")
        return []


def _split_candidates(all_codes: List[str]):
    """
    利用快照预筛将全量代码分成两个候选池：
      - s1_pool：放量活跃股（适用于 StrongTrend / LimitUpHunter）
      - s2_pool：明显下跌股（适用于 BottomFishing / SwingTrading）

    若快照接口失败（非交易时段），两个池均使用 all_codes。
    """
    from src.screening.indicators import prefilter_from_snapshot

    s1_pool = prefilter_from_snapshot(strategy="s1")
    s2_pool = prefilter_from_snapshot(strategy="s2")

    if not s1_pool and not s2_pool:
        logger.info("[Phase1] 快照预筛为空（可能为非交易时段），两个候选池均使用全量代码")
        return all_codes, all_codes

    # 快照预筛结果与全量代码取交集，确保不引入无效代码
    all_set = set(all_codes)
    s1_pool = [c for c in s1_pool if c in all_set] or all_codes
    s2_pool = [c for c in s2_pool if c in all_set] or all_codes

    logger.info(f"[Phase1] 候选池 s1(活跃)={len(s1_pool)} 只，s2(超跌)={len(s2_pool)} 只")
    return s1_pool, s2_pool


def run_phase1(
    target_count: int = 80,
    max_workers: int = 3,
    save: bool = True,
) -> List:
    """
    Phase1 主流程入口

    Args:
        target_count : 种子池目标数量（50-100）
        max_workers  : 并发线程数（建议 3，避免 API 限流）
        save         : 是否保存到 JSON 文件

    Returns:
        SeedEntry 列表
    """
    from src.screening.models import BottomSwing, StrongTrend, LimitUpHunter
    from src.screening.pipeline.seed_pool import SeedEntry, save_seed_pool
    from src.screening.indicators import (
        get_daily_df, get_market_snapshot, get_top5_sectors, get_limitup_sector,
        clear_data_cache, bs_logout,
    )

    logger.info("=" * 50)
    logger.info("[Phase1] 开始：全市场扫描 → 种子池")
    logger.info("=" * 50)

    # Step 1: 获取全量代码
    all_codes = _fetch_all_a_codes()
    if not all_codes:
        logger.error("[Phase1] 无法获取股票代码列表，终止")
        return []

    # Step 2: 分两路候选池
    s1_pool, s2_pool = _split_candidates(all_codes)

    # Step 3: 预热板块缓存（仅交易时段才预热，非交易时段 eastmoney 无法访问会触发 mini_racer 崩溃）
    snapshot = get_market_snapshot()
    if snapshot is not None:
        get_top5_sectors()
        get_limitup_sector()

    # 模型 → (候选池, 模型实例)
    models_config = [
        (BottomSwing(),   s2_pool),
        (StrongTrend(),   s1_pool),
        (LimitUpHunter(), s1_pool),
    ]

    all_results: List = []

    def _score_one(model_instance, code: str):
        """单线程任务：对一只股票运行一个模型"""
        try:
            df = get_daily_df(code, days=120)
            result = model_instance.run(code, df=df)
            if model_instance.is_qualified_seed(result):
                return SeedEntry.from_model_result(result)
        except Exception as e:
            logger.debug(f"[Phase1] {code}/{model_instance.NAME} 评分异常: {e}")
        return None

    # Step 4: 并发评分（四个模型共用线程池，但各自的候选池可能不同）
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for model, pool in models_config:
                for code in pool:
                    f = executor.submit(_score_one, model, code)
                    futures[f] = code
                    time.sleep(0.01)   # 轻微限速，避免触发 API 频率限制

            done = 0
            total = len(futures)
            for f in as_completed(futures):
                done += 1
                if done % 100 == 0:
                    logger.info(f"[Phase1] 进度 {done}/{total}")
                entry = f.result()
                if entry:
                    all_results.append(entry)
    finally:
        clear_data_cache()
        bs_logout()

    logger.info(f"[Phase1] 原始入选 {len(all_results)} 条")

    # Step 5: 去重（同一股票只保留得分最高的模型）
    dedup: Dict[str, object] = {}
    for entry in all_results:
        if entry.code not in dedup or entry.phase1_score > dedup[entry.code].phase1_score:
            dedup[entry.code] = entry

    # Step 6: 按行业板块分组，每个板块取 top5，确保多元化
    seeds = _select_top5_per_sector(list(dedup.values()), target_count)
    logger.info(f"[Phase1] 去重后 {len(dedup)} 只 → 按板块top5筛选后 {len(seeds)} 只进入种子池")

    # Step 7: 保存 JSON
    if save:
        path = save_seed_pool(seeds)
        logger.info(f"[Phase1] 种子池已保存: {path}")

    # 打印摘要
    model_counts: Dict[str, int] = {}
    for e in seeds:
        model_counts[e.model] = model_counts.get(e.model, 0) + 1
    for model_name, cnt in model_counts.items():
        logger.info(f"[Phase1]   {model_name}: {cnt} 只")

    return seeds
