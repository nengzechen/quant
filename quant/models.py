# -*- coding: utf-8 -*-
"""
===================================
量化交易系统 - 数据模型定义
===================================

定义核心数据结构：
- OrderSignal: 交易信号
- Position: 持仓记录
- Portfolio: 投资组合
- TradeRecord: 交易记录
"""

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional
from enum import Enum


class SignalType(str, Enum):
    """交易信号类型"""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class ConfidenceLevel(str, Enum):
    """信心等级"""
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class TradeAction(str, Enum):
    """交易动作"""
    BUY = "BUY"
    SELL = "SELL"


class TradeStatus(str, Enum):
    """交易状态"""
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass
class OrderSignal:
    """
    交易信号 - 由信号聚合 Agent 生成，携带完整的交易决策信息。

    字段说明：
    - stock_code: 股票代码（如 "600519"）
    - stock_name: 股票名称（如 "贵州茅台"）
    - signal_type: 信号类型 BUY/SELL/HOLD
    - confidence: 信心等级 HIGH/MEDIUM/LOW
    - sentiment_score: 情感评分 0-100
    - ideal_buy_price: 理想买入价（来自 battle_plan.sniper_points）
    - stop_loss_price: 止损价格
    - take_profit_price: 获利目标价
    - buy_reason: 操作理由
    - timestamp: 信号生成时间
    """
    stock_code: str
    stock_name: str
    signal_type: SignalType
    confidence: ConfidenceLevel
    sentiment_score: float
    ideal_buy_price: float
    stop_loss_price: float
    take_profit_price: float
    buy_reason: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        """序列化为字典"""
        d = asdict(self)
        d['signal_type'] = self.signal_type.value
        d['confidence'] = self.confidence.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> 'OrderSignal':
        """从字典反序列化"""
        data = dict(data)
        data['signal_type'] = SignalType(data.get('signal_type', 'HOLD'))
        data['confidence'] = ConfidenceLevel(data.get('confidence', 'LOW'))
        return cls(**data)


@dataclass
class Position:
    """
    持仓记录 - 记录当前持有的某只股票的完整信息。

    字段说明：
    - stock_code: 股票代码
    - stock_name: 股票名称
    - quantity: 持仓数量（股）
    - avg_cost: 平均成本价
    - current_price: 当前市价
    - market_value: 当前市值
    - pnl: 盈亏金额
    - pnl_pct: 盈亏百分比
    - open_time: 建仓时间
    - stop_loss_price: 止损价（可选，用于自动止损）
    - take_profit_price: 止盈价（可选）
    """
    stock_code: str
    stock_name: str
    quantity: int
    avg_cost: float
    current_price: float
    market_value: float
    pnl: float
    pnl_pct: float
    open_time: str
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None

    def to_dict(self) -> dict:
        """序列化为字典"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'Position':
        """从字典反序列化"""
        return cls(**data)

    def update_price(self, price: float) -> None:
        """
        更新当前价格并重新计算市值和盈亏。

        Args:
            price: 最新价格
        """
        self.current_price = price
        self.market_value = self.quantity * price
        self.pnl = (price - self.avg_cost) * self.quantity
        self.pnl_pct = (price - self.avg_cost) / self.avg_cost * 100 if self.avg_cost > 0 else 0.0


@dataclass
class Portfolio:
    """
    投资组合 - 记录整体账户状态。

    字段说明：
    - total_capital: 总资金（初始资金）
    - available_cash: 可用现金
    - total_market_value: 持仓总市值
    - total_pnl: 总盈亏
    - positions: 持仓字典 {stock_code: Position}
    - max_positions: 最大持仓数量（风控参数）
    - risk_per_trade_pct: 单笔最大风险比例（风控参数，默认 2%）
    """
    total_capital: float
    available_cash: float
    total_market_value: float
    total_pnl: float
    positions: Dict[str, Position] = field(default_factory=dict)
    max_positions: int = 10
    risk_per_trade_pct: float = 0.02

    def to_dict(self) -> dict:
        """序列化为字典"""
        return {
            'total_capital': self.total_capital,
            'available_cash': self.available_cash,
            'total_market_value': self.total_market_value,
            'total_pnl': self.total_pnl,
            'positions': {k: v.to_dict() for k, v in self.positions.items()},
            'max_positions': self.max_positions,
            'risk_per_trade_pct': self.risk_per_trade_pct,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Portfolio':
        """从字典反序列化"""
        positions = {
            k: Position.from_dict(v)
            for k, v in data.get('positions', {}).items()
        }
        return cls(
            total_capital=data['total_capital'],
            available_cash=data['available_cash'],
            total_market_value=data['total_market_value'],
            total_pnl=data['total_pnl'],
            positions=positions,
            max_positions=data.get('max_positions', 10),
            risk_per_trade_pct=data.get('risk_per_trade_pct', 0.02),
        )

    @property
    def total_assets(self) -> float:
        """总资产 = 可用现金 + 持仓市值"""
        return self.available_cash + self.total_market_value

    @property
    def pnl_pct(self) -> float:
        """总盈亏百分比"""
        if self.total_capital <= 0:
            return 0.0
        return self.total_pnl / self.total_capital * 100

    def recalculate(self) -> None:
        """重新计算总市值和总盈亏（根据所有持仓）"""
        self.total_market_value = sum(p.market_value for p in self.positions.values())
        self.total_pnl = sum(p.pnl for p in self.positions.values())


@dataclass
class TradeRecord:
    """
    交易记录 - 记录每笔交易的完整信息。

    字段说明：
    - record_id: 唯一记录ID
    - stock_code: 股票代码
    - action: 交易动作 BUY/SELL
    - quantity: 交易数量（股）
    - price: 成交价格
    - commission: 手续费
    - timestamp: 交易时间
    - reason: 交易原因
    - status: 交易状态 PENDING/FILLED/REJECTED/CANCELLED
    - order_id: 券商订单ID（可选）
    - total_amount: 交易总金额（含手续费）
    """
    record_id: str
    stock_code: str
    action: TradeAction
    quantity: int
    price: float
    commission: float
    timestamp: str
    reason: str
    status: TradeStatus
    order_id: Optional[str] = None
    total_amount: Optional[float] = None

    def __post_init__(self):
        """初始化后计算总金额"""
        if self.total_amount is None:
            base = self.price * self.quantity
            if self.action == TradeAction.BUY:
                self.total_amount = base + self.commission
            else:
                self.total_amount = base - self.commission

    def to_dict(self) -> dict:
        """序列化为字典"""
        d = asdict(self)
        d['action'] = self.action.value
        d['status'] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> 'TradeRecord':
        """从字典反序列化"""
        data = dict(data)
        data['action'] = TradeAction(data.get('action', 'BUY'))
        data['status'] = TradeStatus(data.get('status', 'PENDING'))
        return cls(**data)

    @classmethod
    def create(
        cls,
        stock_code: str,
        action: TradeAction,
        quantity: int,
        price: float,
        commission: float,
        reason: str = "",
        status: TradeStatus = TradeStatus.PENDING,
    ) -> 'TradeRecord':
        """工厂方法，快速创建交易记录"""
        return cls(
            record_id=str(uuid.uuid4()),
            stock_code=stock_code,
            action=action,
            quantity=quantity,
            price=price,
            commission=commission,
            timestamp=datetime.now().isoformat(),
            reason=reason,
            status=status,
        )
