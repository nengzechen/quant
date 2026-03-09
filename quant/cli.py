# -*- coding: utf-8 -*-
"""
===================================
量化交易系统 - 命令行入口
===================================

提供完整的命令行操作接口。

使用方式：
    # 查看账户状态
    python -m quant.cli account

    # 运行量化分析（dry-run，不下单）
    python -m quant.cli run --stocks 600519,000858 --dry-run

    # 实际下单（模拟盘）
    python -m quant.cli run --stocks 600519,000858 --broker paper

    # 查看持仓
    python -m quant.cli positions

    # 查看交易记录
    python -m quant.cli trades --limit 20

    # 手动下单
    python -m quant.cli order --action buy --stock 600519 --quantity 100 --price 1800

    # 重置模拟账户
    python -m quant.cli reset --capital 1000000

    # 止损检查
    python -m quant.cli stop-loss-check
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中（从任意位置运行都能找到 src 模块）
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 加载 .env 环境变量（与 main.py 保持一致）
try:
    from src.config import setup_env
    setup_env()
except Exception:
    pass

logger = logging.getLogger(__name__)


def _setup_logging(debug: bool = False) -> None:
    """配置日志输出"""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # 降低第三方库噪音
    for noisy in ["httpx", "httpcore", "urllib3", "requests", "akshare"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _get_broker(broker_type: str = "paper", config=None):
    """根据类型创建 Broker 实例"""
    from quant.config import QuantConfig
    cfg = config or QuantConfig.from_env()

    if broker_type == "futu":
        try:
            from quant.broker.futu_broker import FutuBroker
            return FutuBroker(
                host=cfg.futu_host,
                port=cfg.futu_port,
                trade_env=cfg.futu_trade_env,
                max_positions=cfg.max_positions,
                risk_per_trade_pct=cfg.risk_per_trade_pct,
            )
        except Exception as e:
            print(f"富途 Broker 初始化失败: {e}")
            print("降级使用模拟盘...")

    from quant.broker.paper_broker import PaperBroker
    return PaperBroker(
        account_path=cfg.paper_account_path,
        initial_capital=cfg.initial_capital,
        max_positions=cfg.max_positions,
        risk_per_trade_pct=cfg.risk_per_trade_pct,
    )


def _print_account(account_info: dict) -> None:
    """格式化输出账户信息"""
    print("\n===== 账户状态 =====")
    print(f"  总资产:    ¥{account_info.get('total_assets', 0):>14,.2f}")
    print(f"  可用现金:  ¥{account_info.get('available_cash', 0):>14,.2f}")
    print(f"  持仓市值:  ¥{account_info.get('market_value', 0):>14,.2f}")
    total_pnl = account_info.get('total_pnl', 0)
    pnl_pct = account_info.get('pnl_pct', 0)
    pnl_sign = "+" if total_pnl >= 0 else ""
    print(f"  总盈亏:    {pnl_sign}¥{total_pnl:,.2f}  ({pnl_sign}{pnl_pct:.2f}%)")
    print(f"  持仓数量:  {account_info.get('position_count', 0)} / "
          f"{account_info.get('max_positions', 10)}")
    print()


def _print_positions(positions) -> None:
    """格式化输出持仓信息"""
    if not positions:
        print("\n  （暂无持仓）\n")
        return

    print("\n===== 当前持仓 =====")
    print(f"{'代码':<10} {'名称':<12} {'数量':>8} {'成本':>10} {'现价':>10} "
          f"{'市值':>12} {'盈亏':>10} {'盈亏%':>8}")
    print("-" * 88)

    for p in positions:
        code = p.stock_code if isinstance(p, object) and hasattr(p, 'stock_code') else p.get('stock_code', '')
        name = p.stock_name if isinstance(p, object) and hasattr(p, 'stock_name') else p.get('stock_name', '')
        qty = p.quantity if isinstance(p, object) and hasattr(p, 'quantity') else p.get('quantity', 0)
        cost = p.avg_cost if isinstance(p, object) and hasattr(p, 'avg_cost') else p.get('avg_cost', 0)
        price = p.current_price if isinstance(p, object) and hasattr(p, 'current_price') else p.get('current_price', 0)
        mval = p.market_value if isinstance(p, object) and hasattr(p, 'market_value') else p.get('market_value', 0)
        pnl = p.pnl if isinstance(p, object) and hasattr(p, 'pnl') else p.get('pnl', 0)
        pnl_pct = p.pnl_pct if isinstance(p, object) and hasattr(p, 'pnl_pct') else p.get('pnl_pct', 0)

        pnl_sign = "+" if pnl >= 0 else ""
        print(f"{code:<10} {str(name):<12} {qty:>8,} {cost:>10.3f} {price:>10.3f} "
              f"{mval:>12,.2f} {pnl_sign}{pnl:>9,.2f} {pnl_sign}{pnl_pct:>7.2f}%")
    print()


def _print_trades(records, limit: int = 20) -> None:
    """格式化输出交易记录"""
    if not records:
        print("\n  （暂无交易记录）\n")
        return

    print(f"\n===== 最近 {min(len(records), limit)} 条交易记录 =====")
    print(f"{'时间':<20} {'代码':<10} {'动作':<6} {'数量':>8} {'价格':>10} "
          f"{'手续费':>8} {'状态':<10}")
    print("-" * 80)

    for r in records[:limit]:
        ts = r.timestamp if isinstance(r, object) and hasattr(r, 'timestamp') else r.get('timestamp', '')
        code = r.stock_code if isinstance(r, object) and hasattr(r, 'stock_code') else r.get('stock_code', '')
        action = r.action.value if hasattr(r, 'action') and hasattr(r.action, 'value') else str(r.get('action', ''))
        qty = r.quantity if isinstance(r, object) and hasattr(r, 'quantity') else r.get('quantity', 0)
        price = r.price if isinstance(r, object) and hasattr(r, 'price') else r.get('price', 0)
        comm = r.commission if isinstance(r, object) and hasattr(r, 'commission') else r.get('commission', 0)
        status = r.status.value if hasattr(r, 'status') and hasattr(r.status, 'value') else str(r.get('status', ''))

        ts_short = str(ts)[:19]
        action_colored = f"[买入]" if action == "BUY" else "[卖出]"
        print(f"{ts_short:<20} {code:<10} {action_colored:<6} {qty:>8,} "
              f"{price:>10.3f} {comm:>8.2f} {status:<10}")
    print()


def cmd_account(args) -> None:
    """查看账户状态"""
    broker = _get_broker(getattr(args, 'broker', 'paper'))
    account_info = broker.get_account_info()
    _print_account(account_info)


def cmd_positions(args) -> None:
    """查看当前持仓"""
    broker = _get_broker(getattr(args, 'broker', 'paper'))
    positions = broker.get_positions()
    _print_positions(positions)


def cmd_trades(args) -> None:
    """查看交易记录"""
    broker = _get_broker(getattr(args, 'broker', 'paper'))
    limit = getattr(args, 'limit', 20)
    records = broker.get_trade_records(limit=limit)
    _print_trades(records, limit=limit)


def cmd_run(args) -> None:
    """运行量化分析流程"""
    from quant.config import QuantConfig
    from quant.orchestrator import QuantOrchestrator

    stock_codes_raw = getattr(args, 'stocks', '') or ''
    stock_codes = [s.strip() for s in stock_codes_raw.split(',') if s.strip()]

    if not stock_codes:
        # 从配置加载默认股票池
        try:
            from src.config import get_config
            src_config = get_config()
            stock_codes = src_config.stock_list or []
        except Exception:
            pass

    if not stock_codes:
        print("错误: 请通过 --stocks 指定股票代码，例如: --stocks 600519,000858")
        sys.exit(1)

    dry_run = getattr(args, 'dry_run', False)
    broker_type = getattr(args, 'broker', 'paper')

    cfg = QuantConfig.from_env()
    cfg.broker_type = broker_type
    broker = _get_broker(broker_type, cfg)

    print(f"\n{'[DRY RUN] ' if dry_run else ''}开始量化分析...")
    print(f"  股票池: {', '.join(stock_codes)}")
    print(f"  券商类型: {broker_type}")
    print(f"  仓位计算: {cfg.sizing_method}")
    print()

    orchestrator = QuantOrchestrator(broker=broker, config=cfg)
    report = orchestrator.run(stock_codes=stock_codes, dry_run=dry_run)

    # 打印信号汇总
    print(f"\n===== 信号汇总 =====")
    buy_signals = report.get('buy_signals', [])
    sell_signals = report.get('sell_signals', [])
    print(f"  买入信号: {len(buy_signals)} 个")
    for s in buy_signals:
        print(f"    [{s['stock_code']}] {s['stock_name']} "
              f"评分={s['sentiment_score']:.0f} "
              f"买入价={s['ideal_buy_price']:.2f} "
              f"止损={s['stop_loss_price']:.2f}")

    print(f"  卖出信号: {len(sell_signals)} 个")
    for s in sell_signals:
        print(f"    [{s['stock_code']}] {s['stock_name']} "
              f"评分={s['sentiment_score']:.0f}")

    # 打印执行结果
    executed = report.get('executed_trades', [])
    if executed:
        print(f"\n===== 执行结果 =====")
        print(f"  共执行 {len(executed)} 笔交易")
        for t in executed:
            action = t.get('action', '')
            print(f"    [{t.get('stock_code', '')}] {action} "
                  f"{t.get('quantity', 0)}股 @ {t.get('price', 0):.2f} "
                  f"状态={t.get('status', '')}")

    # 打印账户状态
    summary = report.get('portfolio_summary', {})
    if summary and 'account' in summary:
        _print_account(summary['account'])

    if report.get('errors'):
        print(f"\n警告: 运行中发生 {len(report['errors'])} 个错误:")
        for err in report['errors']:
            print(f"  - {err}")

    print(f"\n完成！耗时 {report.get('elapsed_seconds', 0)} 秒")


def cmd_order(args) -> None:
    """手动下单"""
    action = getattr(args, 'action', '').upper()
    stock = getattr(args, 'stock', '')
    quantity = getattr(args, 'quantity', 0)
    price = getattr(args, 'price', 0.0)
    broker_type = getattr(args, 'broker', 'paper')

    if not stock:
        print("错误: 请指定股票代码 --stock")
        sys.exit(1)
    if quantity <= 0:
        print("错误: 请指定有效的交易数量 --quantity")
        sys.exit(1)
    if price <= 0:
        print("错误: 请指定有效的交易价格 --price")
        sys.exit(1)
    if action not in ('BUY', 'SELL'):
        print("错误: --action 必须为 buy 或 sell")
        sys.exit(1)

    broker = _get_broker(broker_type)

    print(f"\n手动下单: {action} {stock} x{quantity}股 @ ¥{price:.3f}")
    confirm = input("确认执行? (yes/no): ").strip().lower()
    if confirm not in ('yes', 'y'):
        print("已取消")
        return

    record = broker.place_order(
        stock_code=stock,
        action=action,
        quantity=quantity,
        price=price,
    )

    print(f"\n下单结果:")
    print(f"  订单ID:  {record.record_id}")
    print(f"  状态:    {record.status.value}")
    print(f"  手续费:  ¥{record.commission:.2f}")
    print(f"  总金额:  ¥{record.total_amount:.2f}")
    print(f"  原因:    {record.reason}")
    print()


def cmd_reset(args) -> None:
    """重置模拟账户"""
    capital = getattr(args, 'capital', 1_000_000)
    broker_type = getattr(args, 'broker', 'paper')

    if broker_type != 'paper':
        print("错误: reset 命令仅适用于模拟盘 (paper)")
        sys.exit(1)

    broker = _get_broker('paper')

    print(f"\n警告: 即将重置模拟账户！")
    print(f"  新初始资金: ¥{capital:,.2f}")
    print(f"  所有持仓和交易记录将被清空")

    confirm = input("确认重置? (yes/no): ").strip().lower()
    if confirm not in ('yes', 'y'):
        print("已取消")
        return

    if hasattr(broker, 'reset'):
        broker.reset(initial_capital=float(capital))
        print(f"\n模拟账户已重置，初始资金: ¥{capital:,.2f}")
        account_info = broker.get_account_info()
        _print_account(account_info)
    else:
        print("错误: 当前 Broker 不支持 reset 操作")


def cmd_stop_loss_check(args) -> None:
    """运行止损检查"""
    from quant.config import QuantConfig
    from quant.orchestrator import QuantOrchestrator

    broker_type = getattr(args, 'broker', 'paper')
    cfg = QuantConfig.from_env()
    broker = _get_broker(broker_type, cfg)

    orchestrator = QuantOrchestrator(broker=broker, config=cfg)

    print("\n运行止损检查...")
    records = orchestrator.run_stop_loss_check()

    if not records:
        print("止损检查完成：无需止损\n")
    else:
        print(f"\n止损检查完成：触发 {len(records)} 笔止损")
        for r in records:
            print(f"  [{r.stock_code}] {r.action.value} {r.quantity}股 @ "
                  f"¥{r.price:.3f} 状态={r.status.value}")
        print()


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器"""
    parser = argparse.ArgumentParser(
        prog="python -m quant.cli",
        description="量化下单 Agent 系统 - 命令行工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m quant.cli account                          # 查看账户状态
  python -m quant.cli positions                        # 查看持仓
  python -m quant.cli trades --limit 20               # 查看最近20条交易
  python -m quant.cli run --stocks 600519,000858 --dry-run  # 仅分析不下单
  python -m quant.cli run --stocks 600519,000858      # 分析并下单（模拟盘）
  python -m quant.cli order --action buy --stock 600519 --quantity 100 --price 1800
  python -m quant.cli reset --capital 1000000         # 重置模拟账户
  python -m quant.cli stop-loss-check                 # 止损检查
        """
    )

    parser.add_argument('--debug', action='store_true', help='启用调试日志')
    parser.add_argument(
        '--broker',
        choices=['paper', 'futu'],
        default='paper',
        help='券商类型 (默认: paper 模拟盘)',
    )

    subparsers = parser.add_subparsers(dest='command', metavar='command')

    # account
    sub_account = subparsers.add_parser('account', help='查看账户状态')
    sub_account.set_defaults(func=cmd_account)

    # positions
    sub_positions = subparsers.add_parser('positions', help='查看当前持仓')
    sub_positions.set_defaults(func=cmd_positions)

    # trades
    sub_trades = subparsers.add_parser('trades', help='查看交易记录')
    sub_trades.add_argument('--limit', type=int, default=20, help='显示条数 (默认20)')
    sub_trades.set_defaults(func=cmd_trades)

    # run
    sub_run = subparsers.add_parser('run', help='运行量化分析流程')
    sub_run.add_argument(
        '--stocks',
        type=str,
        default='',
        help='股票代码列表（逗号分隔），例如: 600519,000858,AAPL',
    )
    sub_run.add_argument(
        '--dry-run',
        action='store_true',
        help='仅分析信号，不实际下单',
    )
    sub_run.set_defaults(func=cmd_run)

    # order
    sub_order = subparsers.add_parser('order', help='手动下单')
    sub_order.add_argument(
        '--action',
        choices=['buy', 'sell', 'BUY', 'SELL'],
        required=True,
        help='交易动作',
    )
    sub_order.add_argument('--stock', type=str, required=True, help='股票代码')
    sub_order.add_argument('--quantity', type=int, required=True, help='交易数量（股）')
    sub_order.add_argument('--price', type=float, required=True, help='交易价格')
    sub_order.set_defaults(func=cmd_order)

    # reset
    sub_reset = subparsers.add_parser('reset', help='重置模拟账户')
    sub_reset.add_argument(
        '--capital',
        type=float,
        default=1_000_000,
        help='新初始资金 (默认: 1000000)',
    )
    sub_reset.set_defaults(func=cmd_reset)

    # stop-loss-check
    sub_sl = subparsers.add_parser('stop-loss-check', help='运行止损检查')
    sub_sl.set_defaults(func=cmd_stop_loss_check)

    return parser


def main() -> None:
    """命令行主入口"""
    parser = build_parser()
    args = parser.parse_args()

    _setup_logging(debug=getattr(args, 'debug', False))

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if not hasattr(args, 'func'):
        parser.print_help()
        sys.exit(1)

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(0)
    except Exception as e:
        logger.error(f"执行失败: {e}", exc_info=True)
        print(f"\n错误: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
