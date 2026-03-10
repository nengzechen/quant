# -*- coding: utf-8 -*-
"""
选股结果通知模块
================
把 Strategy1/Strategy2 的结果格式化后推送通知
"""

import logging
from datetime import datetime
from typing import List, Optional

from src.screening.screener import StrategyResult

logger = logging.getLogger(__name__)

# 评分对应的 emoji
def _score_emoji(score: int, max_score: int) -> str:
    ratio = score / max_score if max_score > 0 else 0
    if ratio >= 0.7:
        return "🔥"
    elif ratio >= 0.5:
        return "✅"
    elif ratio >= 0.3:
        return "⚠️"
    return "❌"


def format_strategy1_message(results: List[StrategyResult], top_n: int = 5) -> str:
    """格式化策略一（强势突破）通知消息"""
    now = datetime.now().strftime("%Y-%m-%d")
    lines = [f"## 📊 {now} 强势突破选股（策略一）\n"]

    if not results:
        lines.append("今日无股票达到门槛，建议观望。")
        return "\n".join(lines)

    for i, r in enumerate(results[:top_n], 1):
        emoji = _score_emoji(r.total_score, r.max_score)
        lines.append(f"**{i}. {r.code}** {emoji} {r.total_score}/{r.max_score}分")
        if r.passed_dims:
            lines.append(f"   ✓ {' | '.join(r.passed_dims)}")
        # 显示关键维度详情
        key_dims = ["MACD金叉>MA20", "均线多头排列", "资金流入", "缠论底分型"]
        for dim in key_dims:
            detail = r.dim_details.get(dim, {})
            if detail.get("passed") is True and detail.get("detail"):
                lines.append(f"   → {detail['detail']}")
                break
        lines.append("")

    lines.append(f"共 {len(results)} 只通过筛选，完整报告见 reports/screening/")
    lines.append("\n> 仅供参考，不构成投资建议")
    return "\n".join(lines)


def format_strategy2_message(results: List[StrategyResult], top_n: int = 5) -> str:
    """格式化策略二（缠论抄底）通知消息"""
    now = datetime.now().strftime("%Y-%m-%d")
    lines = [f"## 📉 {now} 缠论抄底候选（策略二）\n"]

    if not results:
        lines.append("今日无股票达到抄底信号门槛。")
        return "\n".join(lines)

    for i, r in enumerate(results[:top_n], 1):
        emoji = _score_emoji(r.total_score, r.max_score)
        lines.append(f"**{i}. {r.code}** {emoji} {r.total_score}/{r.max_score}分")
        if r.passed_dims:
            lines.append(f"   ✓ {' | '.join(r.passed_dims)}")
        lines.append("")

    lines.append(f"共 {len(results)} 只出现抄底信号，需结合盘面确认。")
    lines.append("\n> 仅供参考，不构成投资建议")
    return "\n".join(lines)


def run_and_notify_screening(
    stock_codes: List[str],
    notifier=None,
    send_notification: bool = True,
    s1_min_score: int = 7,
    s2_min_score: int = 3,
    save_report: bool = True,
) -> dict:
    """
    执行选股并推送通知

    Args:
        stock_codes: 待筛选股票列表
        notifier: NotificationService 实例
        send_notification: 是否推送通知
        s1_min_score: 策略一最低门槛
        s2_min_score: 策略二最低门槛
        save_report: 是否保存 Markdown 报告

    Returns:
        {"strategy1": [...], "strategy2": [...]}
    """
    from src.screening.screener import run_strategy1_batch, run_strategy2_batch
    from src.indicators import get_top5_sectors, get_limitup_sector

    logger.info(f"[选股] 开始筛选 {len(stock_codes)} 只股票...")

    # 预加载板块数据
    top5 = get_top5_sectors()
    limitup = get_limitup_sector()
    logger.info(f"[选股] 今日前五板块: {top5}，涨停最多: {limitup}")

    # 运行策略
    s1_results = run_strategy1_batch(stock_codes, min_score=s1_min_score)
    s2_results = run_strategy2_batch(stock_codes, min_score=s2_min_score)

    logger.info(f"[选股] 策略一通过: {len(s1_results)} 只，策略二通过: {len(s2_results)} 只")

    # 保存报告
    if save_report:
        _save_screening_report(s1_results, s2_results, top5, limitup)

    # 推送通知
    if send_notification and notifier and notifier.is_available():
        try:
            msg1 = format_strategy1_message(s1_results)
            notifier.send(msg1)
            logger.info("[选股] 策略一通知已推送")

            if s2_results:
                msg2 = format_strategy2_message(s2_results)
                notifier.send(msg2)
                logger.info("[选股] 策略二通知已推送")
        except Exception as e:
            logger.error(f"[选股] 推送通知失败: {e}")

    return {"strategy1": s1_results, "strategy2": s2_results, "top5": top5}


def _save_screening_report(s1_results, s2_results, top5, limitup):
    """保存选股报告到 reports/screening/"""
    import os
    from datetime import datetime

    try:
        today = datetime.now().strftime("%Y%m%d")
        report_dir = os.path.join(os.path.dirname(__file__), "..", "..", "reports", "screening")
        os.makedirs(report_dir, exist_ok=True)
        path = os.path.join(report_dir, f"daily_screen_{today}.md")

        lines = [
            f"# 每日选股报告 - {datetime.now().strftime('%Y年%m月%d日')}\n",
            f"> 今日前五板块：{' / '.join(top5) if top5 else '获取失败'}",
            f"> 涨停最多板块：{limitup or '—'}\n",
            "---\n",
            "## 策略一：强势多头突破\n",
        ]

        if s1_results:
            for r in s1_results:
                emoji = _score_emoji(r.total_score, r.max_score)
                lines.append(f"- {emoji} **{r.code}** {r.total_score}/{r.max_score}分 | {' | '.join(r.passed_dims)}")
        else:
            lines.append("- 今日无股票达到门槛")

        lines += ["\n---\n", "## 策略二：缠论深度抄底\n"]

        if s2_results:
            for r in s2_results:
                emoji = _score_emoji(r.total_score, r.max_score)
                lines.append(f"- {emoji} **{r.code}** {r.total_score}/{r.max_score}分 | {' | '.join(r.passed_dims)}")
        else:
            lines.append("- 今日无抄底信号")

        lines.append("\n\n> 仅供参考，不构成投资建议")

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        logger.info(f"[选股] 报告已保存: {path}")
    except Exception as e:
        logger.warning(f"[选股] 保存报告失败: {e}")
