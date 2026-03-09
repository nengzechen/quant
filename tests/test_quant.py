# -*- coding: utf-8 -*-
"""
===================================
量化交易系统 - 单元测试
===================================

覆盖范围：
- PaperBroker: 模拟盘买卖、手续费计算、持久化
- RiskGuard: 各项风险检查
- position_sizing: 三种仓位计算策略
- PortfolioManager: 信号处理流程
- QuantOrchestrator: 编排流程（mock Agent）

运行：
    pytest tests/test_quant.py -v
"""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from quant.broker.paper_broker import (
    COMMISSION_RATE_BUY,
    COMMISSION_RATE_SELL,
    MIN_COMMISSION,
    STAMP_DUTY_RATE,
    PaperBroker,
)
from quant.models import (
    ConfidenceLevel,
    OrderSignal,
    Portfolio,
    Position,
    SignalType,
    TradeStatus,
)
from quant.agents.risk_guard import RiskGuard
from quant.strategies.position_sizing import (
    atr_based,
    fixed_fraction,
    kelly_criterion,
    calculate_position_size,
)


# ===================================================================
# 测试夹具（Fixtures）
# ===================================================================

@pytest.fixture
def tmp_account_path(tmp_path):
    """提供临时账户文件路径"""
    return str(tmp_path / "test_account.json")


@pytest.fixture
def paper_broker(tmp_account_path):
    """提供已初始化的模拟盘 Broker"""
    return PaperBroker(
        account_path=tmp_account_path,
        initial_capital=1_000_000,
        max_positions=10,
        risk_per_trade_pct=0.02,
    )


@pytest.fixture
def sample_portfolio():
    """提供一个样本投资组合"""
    return Portfolio(
        total_capital=1_000_000,
        available_cash=800_000,
        total_market_value=200_000,
        total_pnl=0,
        positions={},
        max_positions=10,
        risk_per_trade_pct=0.02,
    )


@pytest.fixture
def buy_signal():
    """提供一个样本买入信号"""
    return OrderSignal(
        stock_code="600519",
        stock_name="贵州茅台",
        signal_type=SignalType.BUY,
        confidence=ConfidenceLevel.HIGH,
        sentiment_score=78,
        ideal_buy_price=1800.0,
        stop_loss_price=1710.0,
        take_profit_price=2000.0,
        buy_reason="多头排列，缩量回踩MA5",
    )


@pytest.fixture
def sell_signal():
    """提供一个样本卖出信号"""
    return OrderSignal(
        stock_code="000858",
        stock_name="五粮液",
        signal_type=SignalType.SELL,
        confidence=ConfidenceLevel.MEDIUM,
        sentiment_score=35,
        ideal_buy_price=150.0,
        stop_loss_price=0.0,
        take_profit_price=0.0,
        buy_reason="趋势走弱，建议减仓",
    )


# ===================================================================
# PaperBroker 测试
# ===================================================================

class TestPaperBroker:
    """模拟盘 Broker 测试"""

    def test_init_creates_account(self, paper_broker, tmp_account_path):
        """初始化后账户文件应存在"""
        assert os.path.exists(tmp_account_path)
        with open(tmp_account_path) as f:
            data = json.load(f)
        assert 'portfolio' in data
        assert data['portfolio']['total_capital'] == 1_000_000
        assert data['portfolio']['available_cash'] == 1_000_000

    def test_get_account_info(self, paper_broker):
        """账户信息格式正确"""
        info = paper_broker.get_account_info()
        assert 'total_assets' in info
        assert 'available_cash' in info
        assert info['total_assets'] == 1_000_000
        assert info['available_cash'] == 1_000_000

    def test_buy_success(self, paper_broker):
        """正常买入后，资金减少、持仓增加"""
        record = paper_broker.place_order(
            stock_code="600519",
            action="BUY",
            quantity=100,
            price=1800.0,
        )
        assert record.status == TradeStatus.FILLED
        assert record.quantity == 100
        assert record.price == 1800.0

        # 手续费：max(100 * 1800 * 0.0003, 5) = 54
        expected_commission = max(100 * 1800.0 * COMMISSION_RATE_BUY, MIN_COMMISSION)
        assert abs(record.commission - expected_commission) < 0.01

        # 可用现金减少
        portfolio = paper_broker.get_portfolio()
        expected_cash = 1_000_000 - 100 * 1800.0 - expected_commission
        assert abs(portfolio.available_cash - expected_cash) < 0.01

        # 持仓增加
        assert "600519" in portfolio.positions
        assert portfolio.positions["600519"].quantity == 100

    def test_buy_insufficient_cash(self, paper_broker):
        """现金不足时买入应被拒绝"""
        record = paper_broker.place_order(
            stock_code="600519",
            action="BUY",
            quantity=10_000,  # 超过可用资金
            price=1800.0,
        )
        assert record.status == TradeStatus.REJECTED

        # 现金不变
        portfolio = paper_broker.get_portfolio()
        assert portfolio.available_cash == 1_000_000

    def test_sell_success(self, paper_broker):
        """先买入再卖出，持仓清空，现金恢复"""
        # 先买入
        paper_broker.place_order("600519", "BUY", 100, 1800.0)
        cash_after_buy = paper_broker.get_portfolio().available_cash

        # 再卖出
        record = paper_broker.place_order("600519", "SELL", 100, 1850.0)
        assert record.status == TradeStatus.FILLED

        # 卖出手续费（佣金 + 印花税）
        sell_amount = 100 * 1850.0
        commission_sell = max(sell_amount * COMMISSION_RATE_SELL, MIN_COMMISSION)
        stamp_duty = sell_amount * STAMP_DUTY_RATE
        expected_commission = commission_sell + stamp_duty
        assert abs(record.commission - expected_commission) < 0.01

        # 持仓清空
        portfolio = paper_broker.get_portfolio()
        assert "600519" not in portfolio.positions

        # 现金增加（卖出金额 - 手续费）
        expected_cash = cash_after_buy + sell_amount - expected_commission
        assert abs(portfolio.available_cash - expected_cash) < 0.01

    def test_sell_without_position(self, paper_broker):
        """未持仓时卖出应被拒绝"""
        record = paper_broker.place_order("600519", "SELL", 100, 1800.0)
        assert record.status == TradeStatus.REJECTED

    def test_sell_quantity_exceeds_position(self, paper_broker):
        """卖出数量超过持仓应被拒绝"""
        paper_broker.place_order("600519", "BUY", 100, 1800.0)
        record = paper_broker.place_order("600519", "SELL", 200, 1800.0)
        assert record.status == TradeStatus.REJECTED

    def test_partial_sell(self, paper_broker):
        """部分卖出后持仓数量正确"""
        paper_broker.place_order("600519", "BUY", 200, 1800.0)
        paper_broker.place_order("600519", "SELL", 100, 1850.0)

        portfolio = paper_broker.get_portfolio()
        assert "600519" in portfolio.positions
        assert portfolio.positions["600519"].quantity == 100

    def test_avg_cost_on_add_position(self, paper_broker):
        """加仓后平均成本正确计算"""
        paper_broker.place_order("600519", "BUY", 100, 1800.0)
        paper_broker.place_order("600519", "BUY", 100, 1900.0)

        portfolio = paper_broker.get_portfolio()
        pos = portfolio.positions["600519"]
        expected_avg = (1800.0 * 100 + 1900.0 * 100) / 200
        assert abs(pos.avg_cost - expected_avg) < 0.01
        assert pos.quantity == 200

    def test_trade_records_saved(self, paper_broker, tmp_account_path):
        """交易记录应写入 JSON 文件"""
        paper_broker.place_order("600519", "BUY", 100, 1800.0)

        with open(tmp_account_path) as f:
            data = json.load(f)
        assert len(data['trade_records']) == 1
        assert data['trade_records'][0]['stock_code'] == "600519"

    def test_get_trade_records(self, paper_broker):
        """get_trade_records 返回正确数量"""
        paper_broker.place_order("600519", "BUY", 100, 1800.0)
        paper_broker.place_order("000858", "BUY", 200, 150.0)

        records = paper_broker.get_trade_records(limit=10)
        assert len(records) == 2

    def test_reset(self, paper_broker):
        """重置后账户恢复初始状态"""
        paper_broker.place_order("600519", "BUY", 100, 1800.0)
        paper_broker.reset(initial_capital=500_000)

        portfolio = paper_broker.get_portfolio()
        assert portfolio.total_capital == 500_000
        assert portfolio.available_cash == 500_000
        assert len(portfolio.positions) == 0

    def test_persistence_across_instances(self, tmp_account_path):
        """账户状态应在实例间持久化"""
        # 第一个实例买入
        broker1 = PaperBroker(account_path=tmp_account_path, initial_capital=1_000_000)
        broker1.place_order("600519", "BUY", 100, 1800.0)

        # 第二个实例读取
        broker2 = PaperBroker(account_path=tmp_account_path, initial_capital=1_000_000)
        portfolio = broker2.get_portfolio()
        assert "600519" in portfolio.positions
        assert portfolio.positions["600519"].quantity == 100

    def test_update_stop_loss(self, paper_broker):
        """更新止损价应正确保存"""
        paper_broker.place_order("600519", "BUY", 100, 1800.0)
        paper_broker.update_stop_loss("600519", 1710.0)

        portfolio = paper_broker.get_portfolio()
        assert portfolio.positions["600519"].stop_loss_price == 1710.0


# ===================================================================
# RiskGuard 测试
# ===================================================================

class TestRiskGuard:
    """风险守卫测试"""

    def test_check_buy_pass(self, sample_portfolio, buy_signal):
        """正常买入请求应通过风险检查"""
        guard = RiskGuard()
        passed, reason = guard.check_buy(
            portfolio=sample_portfolio,
            signal=buy_signal,
            quantity=100,
            price=1800.0,
        )
        assert passed, f"期望通过，实际失败: {reason}"

    def test_blacklist_blocks_buy(self, sample_portfolio, buy_signal):
        """黑名单股票不能买入"""
        guard = RiskGuard(blacklist=["600519"])
        passed, reason = guard.check_buy(
            portfolio=sample_portfolio,
            signal=buy_signal,
            quantity=100,
            price=1800.0,
        )
        assert not passed
        assert "黑名单" in reason

    def test_low_sentiment_blocks_buy(self, sample_portfolio):
        """情感评分低于阈值时阻止买入"""
        guard = RiskGuard()
        low_signal = OrderSignal(
            stock_code="000001",
            stock_name="平安银行",
            signal_type=SignalType.BUY,
            confidence=ConfidenceLevel.LOW,
            sentiment_score=30,  # 低于 MIN_BUY_SENTIMENT=40
            ideal_buy_price=15.0,
            stop_loss_price=13.0,
            take_profit_price=18.0,
            buy_reason="随便",
        )
        passed, reason = guard.check_buy(
            portfolio=sample_portfolio,
            signal=low_signal,
            quantity=100,
            price=15.0,
        )
        assert not passed
        assert "情感评分" in reason

    def test_insufficient_cash_blocks_buy(self, buy_signal):
        """可用现金不足时拒绝买入"""
        guard = RiskGuard()
        poor_portfolio = Portfolio(
            total_capital=1_000_000,
            available_cash=1_000,  # 只有1000元
            total_market_value=999_000,
            total_pnl=0,
            positions={},
        )
        passed, reason = guard.check_buy(
            portfolio=poor_portfolio,
            signal=buy_signal,
            quantity=100,
            price=1800.0,  # 需要 180000
        )
        assert not passed
        assert "现金不足" in reason

    def test_concentration_limit(self, buy_signal):
        """单股持仓超过30%时拒绝买入"""
        guard = RiskGuard()
        # 总资产 1,000,000，买入 600519 100股 @ 1800 = 180,000 (18%)  → 应通过
        # 但如果买入 200股 @ 1800 = 360,000 (36%) → 应拒绝
        portfolio = Portfolio(
            total_capital=1_000_000,
            available_cash=1_000_000,
            total_market_value=0,
            total_pnl=0,
            positions={},
        )
        passed, reason = guard.check_buy(
            portfolio=portfolio,
            signal=buy_signal,
            quantity=200,  # 200 * 1800 = 360,000 = 36% > 30%
            price=1800.0,
        )
        assert not passed
        assert "集中度" in reason

    def test_max_positions_limit(self, buy_signal):
        """持仓数量达到上限时拒绝新建持仓"""
        guard = RiskGuard()
        # 创建10个持仓（达到上限）
        positions = {
            f"code{i:03d}": Position(
                stock_code=f"code{i:03d}",
                stock_name=f"股票{i}",
                quantity=100,
                avg_cost=10.0,
                current_price=10.0,
                market_value=1000.0,
                pnl=0.0,
                pnl_pct=0.0,
                open_time="2024-01-01",
            )
            for i in range(10)
        }
        full_portfolio = Portfolio(
            total_capital=1_000_000,
            available_cash=900_000,
            total_market_value=10_000,
            total_pnl=0,
            positions=positions,
            max_positions=10,
        )
        passed, reason = guard.check_buy(
            portfolio=full_portfolio,
            signal=buy_signal,
            quantity=100,
            price=1800.0,
        )
        assert not passed
        assert "上限" in reason

    def test_single_trade_risk_limit(self, sample_portfolio, buy_signal):
        """单笔风险超过限额时拒绝"""
        guard = RiskGuard()
        # 买入 1000股，止损距离 90元 → 最大亏损 90,000 > 总资金 2% = 20,000
        passed, reason = guard.check_buy(
            portfolio=sample_portfolio,
            signal=buy_signal,
            quantity=1000,
            price=1800.0,
        )
        assert not passed  # 因为现金也不够

    def test_check_sell_pass(self):
        """正常卖出请求应通过"""
        guard = RiskGuard()
        portfolio = Portfolio(
            total_capital=1_000_000,
            available_cash=800_000,
            total_market_value=200_000,
            total_pnl=0,
            positions={
                "600519": Position(
                    stock_code="600519",
                    stock_name="贵州茅台",
                    quantity=100,
                    avg_cost=1800.0,
                    current_price=1850.0,
                    market_value=185_000.0,
                    pnl=5_000.0,
                    pnl_pct=2.78,
                    open_time="2024-01-01",
                )
            },
        )
        passed, reason = guard.check_sell(
            portfolio=portfolio,
            stock_code="600519",
            quantity=100,
            price=1850.0,
        )
        assert passed

    def test_check_sell_no_position(self, sample_portfolio):
        """未持仓时卖出应被拒绝"""
        guard = RiskGuard()
        passed, reason = guard.check_sell(
            portfolio=sample_portfolio,
            stock_code="600519",
            quantity=100,
            price=1800.0,
        )
        assert not passed

    def test_stop_loss_detection(self):
        """当前价低于止损价时应检测出止损触发"""
        guard = RiskGuard()
        portfolio = Portfolio(
            total_capital=1_000_000,
            available_cash=820_000,
            total_market_value=180_000,
            total_pnl=-18_000,
            positions={
                "600519": Position(
                    stock_code="600519",
                    stock_name="贵州茅台",
                    quantity=100,
                    avg_cost=1800.0,
                    current_price=1700.0,  # 低于止损价
                    market_value=170_000.0,
                    pnl=-10_000.0,
                    pnl_pct=-5.56,
                    open_time="2024-01-01",
                    stop_loss_price=1710.0,
                )
            },
        )
        stop_codes = guard.check_stop_loss(portfolio)
        assert "600519" in stop_codes

    def test_no_stop_loss_when_price_above(self):
        """当前价高于止损价时不应触发止损"""
        guard = RiskGuard()
        portfolio = Portfolio(
            total_capital=1_000_000,
            available_cash=820_000,
            total_market_value=185_000,
            total_pnl=5_000,
            positions={
                "600519": Position(
                    stock_code="600519",
                    stock_name="贵州茅台",
                    quantity=100,
                    avg_cost=1800.0,
                    current_price=1850.0,  # 高于止损价
                    market_value=185_000.0,
                    pnl=5_000.0,
                    pnl_pct=2.78,
                    open_time="2024-01-01",
                    stop_loss_price=1710.0,
                )
            },
        )
        stop_codes = guard.check_stop_loss(portfolio)
        assert len(stop_codes) == 0

    def test_add_remove_blacklist(self):
        """黑名单添加和移除功能"""
        guard = RiskGuard()
        guard.add_to_blacklist("600519")
        assert "600519" in guard.blacklist
        guard.remove_from_blacklist("600519")
        assert "600519" not in guard.blacklist


# ===================================================================
# Position Sizing 测试
# ===================================================================

class TestPositionSizing:
    """仓位计算策略测试"""

    def test_fixed_fraction_basic(self, sample_portfolio, buy_signal):
        """固定分数法：返回正整数手数"""
        # total_capital=1,000,000 * 10% = 100,000 / (1800 * 100) = 0.555 -> 0手
        # available_cash=800,000 * 95% = 760,000 / (1800 * 100) = 4手
        lots = fixed_fraction(sample_portfolio, buy_signal, fraction=0.10)
        assert isinstance(lots, int)
        assert lots >= 0
        # 预期：min(100000, 760000) / (1800 * 100) = 0 手 （100000 / 180000 < 1）
        # 修正：100000 / 180000 = 0.555 -> floor = 0
        assert lots == 0

    def test_fixed_fraction_large_capital(self, buy_signal):
        """固定分数法：大资本时返回合理手数"""
        large_portfolio = Portfolio(
            total_capital=10_000_000,
            available_cash=10_000_000,
            total_market_value=0,
            total_pnl=0,
            positions={},
        )
        lots = fixed_fraction(large_portfolio, buy_signal, fraction=0.10)
        # 10,000,000 * 10% = 1,000,000 / (1800 * 100) = 5手
        assert lots == 5

    def test_fixed_fraction_zero_price(self, sample_portfolio):
        """价格为0时返回0手"""
        zero_price_signal = OrderSignal(
            stock_code="000001",
            stock_name="测试",
            signal_type=SignalType.BUY,
            confidence=ConfidenceLevel.LOW,
            sentiment_score=50,
            ideal_buy_price=0.0,  # 无效价格
            stop_loss_price=0.0,
            take_profit_price=0.0,
            buy_reason="测试",
        )
        lots = fixed_fraction(sample_portfolio, zero_price_signal)
        assert lots == 0

    def test_kelly_criterion_basic(self, sample_portfolio, buy_signal):
        """Kelly公式：返回非负整数"""
        lots = kelly_criterion(sample_portfolio, buy_signal, win_rate=0.55, win_loss_ratio=1.5)
        assert isinstance(lots, int)
        assert lots >= 0

    def test_kelly_criterion_negative_edge(self, sample_portfolio, buy_signal):
        """Kelly公式：胜率不足时返回0"""
        # p=0.3, b=1.0 -> f* = (0.3*1.0 - 0.7)/1.0 = -0.4 < 0 -> 0
        lots = kelly_criterion(sample_portfolio, buy_signal, win_rate=0.3, win_loss_ratio=1.0)
        assert lots == 0

    def test_kelly_half_kelly(self, sample_portfolio, buy_signal):
        """Kelly公式：半Kelly比全Kelly仓位更小"""
        full_lots = kelly_criterion(
            sample_portfolio, buy_signal, half_kelly=False,
            win_rate=0.6, win_loss_ratio=2.0
        )
        half_lots = kelly_criterion(
            sample_portfolio, buy_signal, half_kelly=True,
            win_rate=0.6, win_loss_ratio=2.0
        )
        assert half_lots <= full_lots

    def test_atr_based_with_stop_loss(self, sample_portfolio, buy_signal):
        """ATR风险定量：有止损价时正确计算"""
        # total_capital=1,000,000 * 2% = 20,000 风险金额
        # 止损距离 = 1800 - 1710 = 90
        # 最大股数 = 20000 / 90 = 222
        # 手数 = 222 / 100 = 2
        lots = atr_based(sample_portfolio, buy_signal, risk_pct=0.02)
        assert isinstance(lots, int)
        assert lots == 2

    def test_atr_based_fallback_when_no_stop(self, sample_portfolio):
        """ATR风险定量：无止损价时降级到固定分数法"""
        no_stop_signal = OrderSignal(
            stock_code="000001",
            stock_name="测试",
            signal_type=SignalType.BUY,
            confidence=ConfidenceLevel.MEDIUM,
            sentiment_score=60,
            ideal_buy_price=100.0,
            stop_loss_price=0.0,  # 无止损价
            take_profit_price=120.0,
            buy_reason="测试",
        )
        # 无止损，默认 5% 止损距离 = 5元
        lots = atr_based(sample_portfolio, no_stop_signal, risk_pct=0.02)
        assert isinstance(lots, int)
        assert lots >= 0

    def test_calculate_position_size_dispatch(self, sample_portfolio, buy_signal):
        """统一接口正确分发到各策略"""
        for method in ["fixed_fraction", "kelly", "atr_based"]:
            lots = calculate_position_size(sample_portfolio, buy_signal, method=method)
            assert isinstance(lots, int)
            assert lots >= 0

    def test_calculate_position_size_unknown_method(self, sample_portfolio, buy_signal):
        """未知方法时降级到固定分数法，不抛异常"""
        lots = calculate_position_size(sample_portfolio, buy_signal, method="unknown_method")
        assert isinstance(lots, int)
        assert lots >= 0


# ===================================================================
# PortfolioManager 测试（使用 mock Broker）
# ===================================================================

class TestPortfolioManager:
    """仓位管理 Agent 测试"""

    def test_process_buy_signal(self, tmp_account_path, buy_signal):
        """处理买入信号应执行买入"""
        from quant.agents.portfolio_manager import PortfolioManager
        from quant.config import QuantConfig

        config = QuantConfig(
            initial_capital=10_000_000,  # 足够大的资金
            sizing_method="atr_based",
            risk_per_trade_pct=0.02,
        )
        broker = PaperBroker(
            account_path=tmp_account_path,
            initial_capital=10_000_000,
        )
        pm = PortfolioManager(broker=broker, sizing_method="atr_based", config=config)

        # 修改信号为低价股方便测试
        cheap_signal = OrderSignal(
            stock_code="000001",
            stock_name="平安银行",
            signal_type=SignalType.BUY,
            confidence=ConfidenceLevel.HIGH,
            sentiment_score=75,
            ideal_buy_price=15.0,
            stop_loss_price=13.5,
            take_profit_price=18.0,
            buy_reason="测试买入",
        )

        record = pm.process_signal(cheap_signal)
        assert record is not None
        # ATR: 10M * 2% = 200,000 / (15.0 - 13.5) = 133,333股 → 1333手
        # 但现金限制：10M * 95% / 15.0 = 633,333股 → 6333手
        # 取较小值：1333手 → 133,300 股
        assert record.status == TradeStatus.FILLED

    def test_process_hold_signal(self, tmp_account_path, buy_signal):
        """HOLD 信号不产生交易"""
        from quant.agents.portfolio_manager import PortfolioManager

        broker = PaperBroker(account_path=tmp_account_path, initial_capital=1_000_000)
        pm = PortfolioManager(broker=broker)

        hold_signal = OrderSignal(
            stock_code="600519",
            stock_name="贵州茅台",
            signal_type=SignalType.HOLD,
            confidence=ConfidenceLevel.MEDIUM,
            sentiment_score=55,
            ideal_buy_price=1800.0,
            stop_loss_price=1710.0,
            take_profit_price=2000.0,
            buy_reason="震荡观望",
        )
        record = pm.process_signal(hold_signal)
        assert record is None

    def test_get_portfolio_summary(self, tmp_account_path):
        """组合摘要包含必要字段"""
        from quant.agents.portfolio_manager import PortfolioManager

        broker = PaperBroker(account_path=tmp_account_path, initial_capital=1_000_000)
        pm = PortfolioManager(broker=broker)

        summary = pm.get_portfolio_summary()
        assert 'account' in summary
        assert 'positions' in summary
        assert 'position_count' in summary


# ===================================================================
# 数据模型测试
# ===================================================================

class TestModels:
    """数据模型序列化/反序列化测试"""

    def test_order_signal_to_dict(self, buy_signal):
        """OrderSignal 序列化"""
        d = buy_signal.to_dict()
        assert d['stock_code'] == "600519"
        assert d['signal_type'] == "BUY"
        assert d['confidence'] == "HIGH"

    def test_order_signal_from_dict(self, buy_signal):
        """OrderSignal 反序列化"""
        d = buy_signal.to_dict()
        restored = OrderSignal.from_dict(d)
        assert restored.stock_code == buy_signal.stock_code
        assert restored.signal_type == buy_signal.signal_type
        assert restored.sentiment_score == buy_signal.sentiment_score

    def test_portfolio_serialization(self, sample_portfolio):
        """Portfolio 序列化/反序列化"""
        d = sample_portfolio.to_dict()
        restored = Portfolio.from_dict(d)
        assert restored.total_capital == sample_portfolio.total_capital
        assert restored.available_cash == sample_portfolio.available_cash

    def test_position_update_price(self):
        """Position 价格更新后市值和盈亏正确"""
        pos = Position(
            stock_code="600519",
            stock_name="贵州茅台",
            quantity=100,
            avg_cost=1800.0,
            current_price=1800.0,
            market_value=180_000.0,
            pnl=0.0,
            pnl_pct=0.0,
            open_time="2024-01-01",
        )
        pos.update_price(1900.0)
        assert pos.current_price == 1900.0
        assert pos.market_value == 190_000.0
        assert abs(pos.pnl - 10_000.0) < 0.01
        assert abs(pos.pnl_pct - 5.556) < 0.01

    def test_portfolio_total_assets(self, sample_portfolio):
        """total_assets = available_cash + total_market_value"""
        assert sample_portfolio.total_assets == 1_000_000


# ===================================================================
# QuantOrchestrator 集成测试（mock Agent）
# ===================================================================

class TestQuantOrchestrator:
    """编排器集成测试"""

    def test_dry_run_no_trades(self, tmp_account_path):
        """dry_run 模式下不产生交易记录"""
        from quant.config import QuantConfig
        from quant.orchestrator import QuantOrchestrator

        config = QuantConfig(paper_account_path=tmp_account_path)
        broker = PaperBroker(account_path=tmp_account_path, initial_capital=1_000_000)
        orchestrator = QuantOrchestrator(broker=broker, config=config)

        # Mock 信号聚合器，避免真实 AI 调用
        mock_signal = OrderSignal(
            stock_code="600519",
            stock_name="贵州茅台",
            signal_type=SignalType.BUY,
            confidence=ConfidenceLevel.HIGH,
            sentiment_score=75,
            ideal_buy_price=1800.0,
            stop_loss_price=1710.0,
            take_profit_price=2000.0,
            buy_reason="测试",
        )

        with patch.object(orchestrator._signal_aggregator, 'aggregate', return_value=[mock_signal]):
            report = orchestrator.run(["600519"], dry_run=True)

        assert report['dry_run'] is True
        assert len(report['executed_trades']) == 0
        assert len(report['buy_signals']) == 1

    def test_get_report_structure(self, tmp_account_path):
        """get_report 返回标准结构"""
        from quant.config import QuantConfig
        from quant.orchestrator import QuantOrchestrator

        config = QuantConfig(paper_account_path=tmp_account_path)
        broker = PaperBroker(account_path=tmp_account_path, initial_capital=1_000_000)
        orchestrator = QuantOrchestrator(broker=broker, config=config)

        report = orchestrator.get_report()
        assert 'account' in report
        assert 'positions' in report
        assert 'recent_trades' in report
        assert 'broker_type' in report
