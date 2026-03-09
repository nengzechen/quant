# -*- coding: utf-8 -*-
"""
===================================
量化交易系统 - 配置管理
===================================

独立于主配置系统的量化交易专用配置。
支持从环境变量加载，与现有 src.config.Config 解耦。
"""

import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class QuantConfig:
    """
    量化交易系统配置类。

    所有配置均可通过环境变量覆盖，格式为 QUANT_<字段名大写>。
    例如：QUANT_INITIAL_CAPITAL=500000

    字段说明：
    - initial_capital: 初始资金（模拟盘）
    - broker_type: 券商类型 "paper"（模拟）或 "futu"（富途真实）
    - paper_account_path: 模拟账户 JSON 文件路径
    - max_positions: 最大持仓数量
    - risk_per_trade_pct: 单笔最大风险比例（2% = 0.02）
    - sizing_method: 仓位计算方法
    - fixed_fraction: 固定分数法仓位比例
    - kelly_win_rate: Kelly 公式胜率假设
    - kelly_win_loss_ratio: Kelly 公式盈亏比
    - blacklist: 黑名单股票代码列表
    - futu_host: 富途 OpenD 地址
    - futu_port: 富途 OpenD 端口
    - futu_trade_env: 富途交易环境 SIMULATE/REAL
    - max_signal_workers: 信号聚合并发数
    """
    initial_capital: float = 1_000_000
    broker_type: str = "paper"
    paper_account_path: str = os.path.expanduser("~/.stock_quant/paper_account.json")
    max_positions: int = 10
    risk_per_trade_pct: float = 0.02
    sizing_method: str = "atr_based"
    fixed_fraction: float = 0.10
    kelly_win_rate: float = 0.55
    kelly_win_loss_ratio: float = 1.5
    blacklist: List[str] = field(default_factory=list)
    futu_host: str = "127.0.0.1"
    futu_port: int = 11111
    futu_trade_env: str = "SIMULATE"
    max_signal_workers: int = 3

    @classmethod
    def from_env(cls) -> "QuantConfig":
        """
        从环境变量加载配置。

        支持以下环境变量：
        QUANT_INITIAL_CAPITAL, QUANT_BROKER_TYPE, QUANT_PAPER_ACCOUNT_PATH,
        QUANT_MAX_POSITIONS, QUANT_RISK_PER_TRADE_PCT, QUANT_SIZING_METHOD,
        QUANT_FIXED_FRACTION, QUANT_KELLY_WIN_RATE, QUANT_KELLY_WIN_LOSS_RATIO,
        QUANT_BLACKLIST (逗号分隔), QUANT_FUTU_HOST, QUANT_FUTU_PORT,
        QUANT_FUTU_TRADE_ENV, QUANT_MAX_SIGNAL_WORKERS
        """
        blacklist_str = os.getenv("QUANT_BLACKLIST", "")
        blacklist = [c.strip().upper() for c in blacklist_str.split(",") if c.strip()]

        return cls(
            initial_capital=float(os.getenv("QUANT_INITIAL_CAPITAL", "1000000")),
            broker_type=os.getenv("QUANT_BROKER_TYPE", "paper"),
            paper_account_path=os.path.expanduser(
                os.getenv("QUANT_PAPER_ACCOUNT_PATH", "~/.stock_quant/paper_account.json")
            ),
            max_positions=int(os.getenv("QUANT_MAX_POSITIONS", "10")),
            risk_per_trade_pct=float(os.getenv("QUANT_RISK_PER_TRADE_PCT", "0.02")),
            sizing_method=os.getenv("QUANT_SIZING_METHOD", "atr_based"),
            fixed_fraction=float(os.getenv("QUANT_FIXED_FRACTION", "0.10")),
            kelly_win_rate=float(os.getenv("QUANT_KELLY_WIN_RATE", "0.55")),
            kelly_win_loss_ratio=float(os.getenv("QUANT_KELLY_WIN_LOSS_RATIO", "1.5")),
            blacklist=blacklist,
            futu_host=os.getenv("QUANT_FUTU_HOST", "127.0.0.1"),
            futu_port=int(os.getenv("QUANT_FUTU_PORT", "11111")),
            futu_trade_env=os.getenv("QUANT_FUTU_TRADE_ENV", "SIMULATE"),
            max_signal_workers=int(os.getenv("QUANT_MAX_SIGNAL_WORKERS", "3")),
        )
