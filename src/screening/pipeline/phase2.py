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

# ===== 自动卖出参数 =====
_STOP_LOSS_PCT = {
    "LimitUpHunter": -0.03,   # 追涨停风险高，止损更严
    "default":       -0.05,   # 其余模型统一 -5%
}
_TAKE_PROFIT_PCT = {
    "BottomSwing":   0.10,
    "StrongTrend":   0.20,
    "LimitUpHunter": 0.06,
    "default":       0.15,
}
_TRAILING_STOP_PCT = {          # 从最高价回落多少触发移动止损
    "BottomSwing": 0.07,
    "StrongTrend": 0.10,
}
_MAX_HOLD_DAYS = 14             # 持有超过 14 天且浮盈 < 2% → 时间止损


def _check_sell_signals(broker) -> int:
    """
    检查所有持仓是否触发卖出条件，触发则自动下卖单。
    优先级：硬止损 > 止盈 > 移动止损 > 时间止损

    Returns:
        本轮实际卖出数量
    """
    from src.screening.indicators import get_realtime_quote_tencent

    positions = broker.get_positions()
    if not positions:
        return 0

    # 批量获取实时价，同时更新 highest_price
    price_map = {}
    for pos in positions:
        quote = get_realtime_quote_tencent(pos.stock_code)
        price = quote.get("current_price", 0.0) if quote else 0.0
        if price > 0:
            price_map[pos.stock_code] = price
    if price_map:
        broker.update_position_prices(price_map)

    # 重新读取更新后的持仓
    positions = broker.get_positions()
    sold = 0

    for pos in positions:
        code = pos.stock_code
        model = pos.model or "default"
        current_price = pos.current_price
        avg_cost = pos.avg_cost
        if avg_cost <= 0 or current_price <= 0:
            continue

        pnl_pct = (current_price - avg_cost) / avg_cost

        sell_reason = None

        # 1. 硬止损
        stop_pct = _STOP_LOSS_PCT.get(model, _STOP_LOSS_PCT["default"])
        if pnl_pct <= stop_pct:
            sell_reason = f"硬止损 {pnl_pct*100:.1f}% ≤ {stop_pct*100:.0f}%"

        # 2. 止盈
        if sell_reason is None:
            tp_pct = _TAKE_PROFIT_PCT.get(model, _TAKE_PROFIT_PCT["default"])
            if pnl_pct >= tp_pct:
                sell_reason = f"止盈 {pnl_pct*100:.1f}% ≥ {tp_pct*100:.0f}%"

        # 3. 移动止损（仅适用特定模型，且已盈利过）
        if sell_reason is None and model in _TRAILING_STOP_PCT:
            trail_pct = _TRAILING_STOP_PCT[model]
            hp = pos.highest_price
            if hp and hp > avg_cost and current_price <= hp * (1 - trail_pct):
                from_peak = (current_price - hp) / hp
                sell_reason = f"移动止损 从最高{hp:.2f}回落{from_peak*100:.1f}% ≤ -{trail_pct*100:.0f}%"

        # 4. 时间止损
        if sell_reason is None and pos.open_time:
            try:
                open_dt = datetime.fromisoformat(pos.open_time)
                hold_days = (datetime.now() - open_dt).days
                if hold_days >= _MAX_HOLD_DAYS and pnl_pct < 0.02:
                    sell_reason = f"时间止损 持有{hold_days}天收益{pnl_pct*100:.1f}%<2%"
            except Exception:
                pass

        if sell_reason:
            record = broker.place_order(
                stock_code=code,
                action="SELL",
                quantity=pos.quantity,
                price=current_price,
                stock_name=pos.stock_name,
            )
            if record.status.value == "FILLED":
                logger.info(
                    f"[Phase2] 自动卖出: {code} {pos.stock_name} "
                    f"@{current_price:.2f} | {sell_reason}"
                )
                sold += 1
            else:
                logger.warning(f"[Phase2] 卖出失败: {code} {record.status}")

    return sold


def _check_universal_trigger(code: str, df) -> Tuple[bool, str]:
    """
    通用盘中触发：高开 + 量比>1 + 换手率>3%
    优先使用腾讯实时行情；获取失败时 fallback 到日线数据。
    """
    from src.screening.indicators import (
        get_realtime_quote_tencent,
        check_high_open_rt, check_volume_ratio_rt, check_turnover_rt,
        check_high_open, check_volume_ratio, check_turnover,
    )

    quote = get_realtime_quote_tencent(code)
    if quote:
        r_ho = check_high_open_rt(quote)
        r_vr = check_volume_ratio_rt(quote, df, threshold=1.0)
        r_to = check_turnover_rt(quote, threshold=3.0)
        source = "实时"
    else:
        logger.debug(f"[Phase2] {code} 实时行情获取失败，fallback 到日线数据")
        r_ho = check_high_open(df)
        r_vr = check_volume_ratio(df, threshold=1.0)
        r_to = check_turnover(df, threshold=3.0)
        source = "日线"

    triggered = all(r["passed"] is True for r in [r_ho, r_vr, r_to])
    reason = (
        f"[{source}]高开{r_ho.get('value', '?')} | "
        f"量比{r_vr.get('value', '?')} | "
        f"换手{r_to.get('value', '?')}%"
    )
    return triggered, reason


def _check_model_trigger(entry: "SeedEntry", code: str, df) -> Tuple[bool, str]:
    """
    按模型专属触发条件（Phase2 盘中调用，仅用实时+日线数据，不依赖 AKShare）：

    BottomSwing   : 实时量比>1.2（底部反弹放量确认）
    StrongTrend   : 实时量比>1.2（强趋势续量）
    LimitUpHunter : 九五之尊形态 OR 日线换手率>3%
    """
    from src.screening.indicators import (
        get_realtime_quote_tencent,
        check_volume_ratio_rt, check_jiuyu_zhizun, check_turnover,
    )

    model = entry.model
    quote = get_realtime_quote_tencent(code)

    if model in ("BottomSwing", "StrongTrend"):
        r_vr = check_volume_ratio_rt(quote, df, threshold=1.2)
        triggered = r_vr["passed"] is True
        return triggered, f"实时量比{r_vr.get('value', '?')}x放量确认"

    elif model == "LimitUpHunter":
        r_jyzz = check_jiuyu_zhizun(df)
        r_to = check_turnover(df, threshold=3.0)
        triggered = (r_jyzz["passed"] is True) or (r_to["passed"] is True)
        return triggered, "九五之尊形态或高换手确认"

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
    """触发信号后自动下模拟买单（等权仓位，优先实时价）"""
    try:
        from src.screening.indicators import get_realtime_quote_tencent
        quote = get_realtime_quote_tencent(entry.code)
        price = quote.get("current_price", 0.0) if quote else 0.0
        if price <= 0 and df is not None and not df.empty:
            price = float(df.iloc[-1]["close"])
        if price <= 0:
            logger.warning(f"[Phase2] {entry.code} 无法获取价格，跳过自动下单")
            return

        account = broker.get_account_info()
        max_pos = account.get("max_positions", 10)
        total_cap = account.get("total_capital", 1_000_000)
        avail_cash = account.get("available_cash", 0.0)
        cur_positions = account.get("position_count", 0)

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
            stock_name=entry.name,
            model=entry.model,
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

        # 每轮先检查持仓是否触发卖出
        if broker is not None:
            sold_count = _check_sell_signals(broker)
            if sold_count > 0:
                logger.info(f"[Phase2] 本轮自动卖出 {sold_count} 只")

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
                    entry.phase2_trigger_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
