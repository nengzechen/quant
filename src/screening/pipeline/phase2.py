# -*- coding: utf-8 -*-
"""
Phase2：盘中实时监控（种子池 → 买入信号）

执行逻辑（每轮）：
  1. 读取当日种子池（load_seed_pool）
  2. 过滤掉已触发的（避免重复推送）
  3. 对每只种子：通用触发检查 + 按模型专属触发检查
  4. 触发 → 标记 + 推送通知 + 更新 JSON
  5. sleep interval_seconds 进入下一轮

使用方式：
    python main.py --phase2                       # 循环 30 轮，每轮 60 秒
    python main.py --phase2 --phase2-rounds 1     # 单次扫描（测试用）
"""
import logging
import time
from datetime import datetime
from typing import List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from src.screening.pipeline.seed_pool import SeedEntry

logger = logging.getLogger(__name__)


def _check_universal_trigger(code: str, df) -> Tuple[bool, str]:
    """
    通用盘中触发：高开 + 量比>1 + 换手率>3%（三条全部满足）

    Returns:
        (triggered, reason_str)
    """
    from src.screening.indicators import check_high_open, check_volume_ratio, check_turnover
    r_ho = check_high_open(df)
    r_vr = check_volume_ratio(df, threshold=1.0)
    r_to = check_turnover(df, threshold=3.0)

    triggered = all(r["passed"] is True for r in [r_ho, r_vr, r_to])
    reason = (
        f"高开{r_ho.get('value', '?')} | "
        f"量比{r_vr.get('value', '?')} | "
        f"换手{r_to.get('value', '?')}%"
    )
    return triggered, reason


def _check_model_trigger(entry: "SeedEntry", code: str, df) -> Tuple[bool, str]:
    """
    按模型专属触发条件（Phase2 盘中调用）：

    BottomSwing   : 资金流入 + 强分时（近似 5 分钟底分型）
    StrongTrend   : 强分时 + 量比>1 + 大单净流入（三合一）
    LimitUpHunter : 九五之尊 + 强分时（或极高换手）
    """
    from src.screening.indicators import (
        check_fund_flow, check_intraday_strong,
        check_jiuyu_zhizun, check_turnover, check_volume_ratio,
    )

    model = entry.model

    if model == "BottomSwing":
        r_ff = check_fund_flow(code)
        r_is = check_intraday_strong(code)
        triggered = (r_ff["passed"] is True) and (r_is["passed"] is True)
        return triggered, "资金流入 + 强分时确认"

    elif model == "StrongTrend":
        r_is = check_intraday_strong(code)
        r_vr = check_volume_ratio(df, threshold=1.0)
        r_ff = check_fund_flow(code)
        triggered = all(r["passed"] is True for r in [r_is, r_vr, r_ff])
        return triggered, "强分时 + 量比>1 + 大单净流入"

    elif model == "LimitUpHunter":
        r_jyzz = check_jiuyu_zhizun(df)
        r_is = check_intraday_strong(code)
        r_to = check_turnover(df, threshold=3.0)
        triggered = (r_jyzz["passed"] is True) and any(
            r["passed"] is True for r in [r_is, r_to]
        )
        return triggered, "九五之尊 + 分时强度"

    return False, f"未知模型 {model}"


def _format_signals(triggered: List["SeedEntry"]) -> str:
    """格式化 Phase2 买入信号推送消息"""
    now = datetime.now().strftime("%H:%M")
    lines = [f"## 🚨 实时买入信号 [{now}]\n"]
    for entry in triggered:
        score_pct = int(entry.phase1_score / entry.max_score * 100) if entry.max_score else 0
        lines.append(
            f"**{entry.code} {entry.name}** "
            f"| {entry.model} "
            f"| Phase1得分 {entry.phase1_score}/{entry.max_score}({score_pct}%)"
        )
        lines.append(f"   触发条件: {entry.phase2_reason}")
        if entry.passed_dims:
            lines.append(f"   已通过: {' | '.join(entry.passed_dims[:6])}")
        lines.append("")
    lines.append("> 仅供参考，不构成投资建议")
    return "\n".join(lines)


def run_phase2_once(
    date_str: Optional[str] = None,
    notifier=None,
    send_notification: bool = True,
) -> List["SeedEntry"]:
    """
    Phase2 单轮扫描（供外部调用或测试）。
    """
    return run_phase2(
        date_str=date_str,
        notifier=notifier,
        send_notification=send_notification,
        interval_seconds=0,
        max_rounds=1,
    )


def _place_auto_order(broker, entry: "SeedEntry", df) -> None:
    """触发信号后自动下模拟买单（等权仓位）"""
    try:
        price = float(df.iloc[-1]["close"]) if df is not None and not df.empty else 0.0
        if price <= 0:
            logger.warning(f"[Phase2] {entry.code} 无法获取价格，跳过自动下单")
            return

        portfolio = broker.get_portfolio()
        max_pos = portfolio.get("max_positions", 10)
        total_cap = portfolio.get("total_capital", 1_000_000)
        avail_cash = portfolio.get("available_cash", 0.0)
        cur_positions = portfolio.get("position_count", 0)

        if cur_positions >= max_pos:
            logger.info(f"[Phase2] 持仓已满 ({cur_positions}/{max_pos})，跳过 {entry.code}")
            return

        # 等权仓位：总资金 / 最大持仓数，最小手=100股
        alloc = total_cap / max_pos
        quantity = int(alloc / price / 100) * 100
        if quantity <= 0:
            logger.warning(f"[Phase2] {entry.code} 计算手数为 0，跳过自动下单")
            return

        cost = price * quantity
        if cost > avail_cash:
            logger.warning(f"[Phase2] {entry.code} 资金不足 (需 {cost:.0f}，余 {avail_cash:.0f})")
            return

        record = broker.place_order(
            stock_code=entry.code,
            action="BUY",
            quantity=quantity,
            price=price,
        )
        logger.info(
            f"[Phase2] 自动下单: {entry.code} {entry.name} "
            f"×{quantity} 股 @{price:.2f} 状态={record.status}"
        )
    except Exception as e:
        logger.error(f"[Phase2] 自动下单失败 {entry.code}: {e}")


def run_phase2(
    date_str: Optional[str] = None,
    notifier=None,
    send_notification: bool = True,
    interval_seconds: int = 60,
    max_rounds: int = 30,
    broker=None,
) -> List["SeedEntry"]:
    """
    Phase2 主流程入口（盘中循环监控）

    Args:
        date_str          : 读取哪天的种子池（默认今天）
        notifier          : NotificationService 实例
        send_notification : 是否推送通知
        interval_seconds  : 每轮扫描间隔（秒）
        max_rounds        : 最大扫描轮数
        broker            : 券商实例（传入则触发时自动下模拟买单）

    Returns:
        全部已触发买入信号的 SeedEntry 列表
    """
    from src.screening.pipeline.seed_pool import load_seed_pool, save_seed_pool
    from src.screening.indicators import get_daily_df, clear_data_cache

    triggered_all: List["SeedEntry"] = []

    for round_i in range(max_rounds):
        logger.info(f"[Phase2] 第 {round_i + 1}/{max_rounds} 轮扫描 [{datetime.now().strftime('%H:%M:%S')}]")

        seeds = load_seed_pool(date_str)
        if not seeds:
            logger.warning("[Phase2] 种子池为空，请先运行 --phase1")
            break

        pending = [s for s in seeds if not s.phase2_triggered]
        if not pending:
            logger.info("[Phase2] 种子池内所有股票已触发")
            break

        logger.info(f"[Phase2] 待监控 {len(pending)} 只")
        triggered_this: List["SeedEntry"] = []

        try:
            for entry in pending:
                code = entry.code
                df = get_daily_df(code, days=30)

                ok_u, reason_u = _check_universal_trigger(code, df)
                if not ok_u:
                    continue

                ok_m, reason_m = _check_model_trigger(entry, code, df)
                if ok_m:
                    entry.phase2_triggered = True
                    entry.phase2_trigger_time = datetime.now().strftime("%H:%M:%S")
                    entry.phase2_reason = f"{reason_u} | {reason_m}"
                    triggered_this.append(entry)
                    logger.info(f"[Phase2] 买入信号: {code} {entry.name} | {entry.phase2_reason}")
                    if broker is not None:
                        _place_auto_order(broker, entry, df)
        finally:
            clear_data_cache()

        if triggered_this:
            save_seed_pool(seeds, date_str)   # 更新 JSON（标记触发状态）
            triggered_all.extend(triggered_this)

            if send_notification and notifier and notifier.is_available():
                msg = _format_signals(triggered_this)
                notifier.send(msg)
                logger.info(f"[Phase2] 已推送 {len(triggered_this)} 只买入信号")

        logger.info(
            f"[Phase2] 本轮触发 {len(triggered_this)} 只 | "
            f"累计触发 {len(triggered_all)} 只 | "
            f"剩余待监控 {len(pending) - len(triggered_this)} 只"
        )

        if round_i < max_rounds - 1 and interval_seconds > 0:
            time.sleep(interval_seconds)

    # 保存本次所有买入信号到 reports/signals/
    if triggered_all:
        _save_signal_report(triggered_all, date_str)

    return triggered_all


def _save_signal_report(entries: List["SeedEntry"], date_str: Optional[str] = None) -> None:
    """将 Phase2 买入信号追加保存到 reports/signals/phase2_YYYYMMDD.md"""
    import os
    today = date_str or datetime.now().strftime("%Y%m%d")
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    signals_dir = os.path.join(project_root, "reports", "signals")
    os.makedirs(signals_dir, exist_ok=True)
    report_path = os.path.join(signals_dir, f"phase2_{today}.md")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Phase2 买入信号 [{now_str}]\n"]
    for e in entries:
        score_pct = int(e.phase1_score / e.max_score * 100) if e.max_score else 0
        lines.append(
            f"- **{e.code} {e.name}** [{e.model}] "
            f"Phase1得分 {e.phase1_score}/{e.max_score}({score_pct}%) "
            f"| 触发时间 {e.phase2_trigger_time} "
            f"| 触发条件: {e.phase2_reason}"
        )
    lines.append("\n> 仅供参考，不构成投资建议")
    content = "\n".join(lines) + "\n"

    mode = "a" if os.path.exists(report_path) else "w"
    with open(report_path, mode, encoding="utf-8") as f:
        f.write(content)
    logger.info(f"[Phase2] 信号报告已保存: {report_path}")
