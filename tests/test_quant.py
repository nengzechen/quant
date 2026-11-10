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
from dataclasses import replace
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
    TradeAction,
    TradeRecord,
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
    """提供已初始化的模拟盘 Broker（关闭 T+1，便于同日买卖的基础用例）"""
    return PaperBroker(
        account_path=tmp_account_path,
        initial_capital=1_000_000,
        max_positions=10,
        risk_per_trade_pct=0.02,
        t_plus_one=False,
    )


@pytest.fixture
def t1_broker(tmp_account_path):
    """开启 T+1 的模拟盘 Broker（默认行为）"""
    return PaperBroker(account_path=tmp_account_path, initial_capital=1_000_000)


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
        assert info['total_pnl'] == 0
        assert info['unrealized_pnl'] == 0

    def test_account_pnl_includes_realized_after_close(self, paper_broker):
        """清仓后账户总盈亏保留已实现盈亏（扣手续费），浮动盈亏归零"""
        paper_broker.place_order("600519", "BUY", 100, 1800.0)
        paper_broker._portfolio.positions["600519"].last_buy_date = "2000-01-01"
        paper_broker.place_order("600519", "SELL", 100, 1900.0)

        info = paper_broker.get_account_info()
        # 买入佣金 54 + 卖出佣金 57 + 印花税 95 = 206
        assert abs(info['total_pnl'] - (10_000 - 206)) < 0.01
        assert abs(info['pnl_pct'] - (10_000 - 206) / 1_000_000 * 100) < 1e-6
        assert info['unrealized_pnl'] == 0

    def test_account_pnl_counts_buy_commission_while_holding(self, paper_broker):
        """持仓未动时账户总盈亏 = 浮动盈亏 - 已付买入佣金"""
        paper_broker.place_order("600519", "BUY", 100, 1800.0)
        paper_broker.update_position_prices({"600519": 1850.0})

        info = paper_broker.get_account_info()
        assert abs(info['unrealized_pnl'] - 5_000) < 0.01
        assert abs(info['total_pnl'] - (5_000 - 54)) < 0.01

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

    def test_sell_stamp_duty_halved_rate(self, paper_broker):
        """印花税按 2023-08-28 起的万分之五计：卖出 10 万元 = 佣金 30 + 印花税 50"""
        paper_broker.place_order("600519", "BUY", 100, 1000.0)
        record = paper_broker.place_order("600519", "SELL", 100, 1000.0)
        assert record.status == TradeStatus.FILLED
        assert abs(record.commission - 80.0) < 0.01

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

    def test_lowercase_code_stored_uppercase(self, paper_broker):
        """小写代码（如 hk00700）的持仓按大写存键，后续更新 / 卖出用任意大小写都能命中"""
        paper_broker.place_order("hk00700", "BUY", 100, 400.0)
        portfolio = paper_broker.get_portfolio()
        assert list(portfolio.positions) == ["HK00700"]
        assert portfolio.positions["HK00700"].stock_code == "HK00700"

        paper_broker.update_stop_loss("hk00700", 380.0)
        paper_broker.update_take_profit("hk00700", 450.0)
        paper_broker.update_position_prices({"hk00700": 390.0})
        pos = paper_broker.get_portfolio().positions["HK00700"]
        assert pos.stop_loss_price == 380.0
        assert pos.take_profit_price == 450.0
        assert pos.current_price == 390.0

        record = paper_broker.place_order("hk00700", "SELL", 100, 390.0)
        assert record.status == TradeStatus.FILLED
        assert paper_broker.get_portfolio().positions == {}

    def test_lowercase_code_stop_loss_executes(self, paper_broker):
        """小写代码建仓后跌破止损能真正卖出（之前风控按大写查不到持仓，止损卖不掉）"""
        from quant.agents.portfolio_manager import PortfolioManager

        paper_broker.place_order("hk00700", "BUY", 100, 400.0)
        paper_broker._portfolio.positions["HK00700"].last_buy_date = "2000-01-01"
        paper_broker.update_stop_loss("hk00700", 380.0)
        paper_broker.update_position_prices({"HK00700": 370.0})
        pm = PortfolioManager(broker=paper_broker)

        records = pm.check_and_stop_loss()
        assert len(records) == 1
        assert records[0].status == TradeStatus.FILLED
        assert "HK00700" not in paper_broker.get_portfolio().positions

    def test_legacy_lowercase_keys_migrated_on_load(self, tmp_account_path):
        """旧账户文件中的小写持仓键加载时转大写，大小写重复的持仓合并"""
        broker = PaperBroker(account_path=tmp_account_path, initial_capital=1_000_000)
        broker.place_order("HK00700", "BUY", 100, 400.0)
        with open(tmp_account_path, encoding="utf-8") as f:
            data = json.load(f)
        upper = data["portfolio"]["positions"]["HK00700"]
        lower = dict(upper, stock_code="hk00700", quantity=200, avg_cost=370.0,
                     stop_loss_price=360.0)
        upper["stop_loss_price"] = 350.0
        data["portfolio"]["positions"] = {"hk00700": lower, "HK00700": upper}
        with open(tmp_account_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        reloaded = PaperBroker(account_path=tmp_account_path, initial_capital=1_000_000)
        positions = reloaded.get_portfolio().positions
        assert list(positions) == ["HK00700"]
        pos = positions["HK00700"]
        assert pos.quantity == 300
        assert abs(pos.avg_cost - 380.0) < 1e-6
        assert pos.stop_loss_price == 360.0

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

    def test_corrupt_account_file_backed_up_before_reinit(self, tmp_account_path):
        """账户文件损坏时应先备份再重新初始化，不能直接覆盖掉原数据"""
        from pathlib import Path

        Path(tmp_account_path).write_text('{"portfolio": {"total_cap', encoding='utf-8')

        broker = PaperBroker(account_path=tmp_account_path, initial_capital=1_000_000)

        assert broker.get_portfolio().available_cash == 1_000_000
        backups = list(Path(tmp_account_path).parent.glob("test_account.json.corrupt-*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding='utf-8') == '{"portfolio": {"total_cap'

    def test_save_is_atomic_on_write_failure(self, paper_broker, tmp_account_path, monkeypatch):
        """写入中途失败时原账户文件应保持完整，且不残留临时文件"""
        from pathlib import Path

        paper_broker.place_order("600519", "BUY", 100, 1800.0)
        before = Path(tmp_account_path).read_text(encoding='utf-8')

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("quant.broker.paper_broker.json.dump", boom)
        paper_broker.update_stop_loss("600519", 1710.0)  # _save 内部吞掉异常

        assert Path(tmp_account_path).read_text(encoding='utf-8') == before
        leftovers = [p for p in Path(tmp_account_path).parent.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []
        reloaded = PaperBroker(account_path=tmp_account_path)
        assert reloaded.get_portfolio().positions["600519"].quantity == 100

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

    def test_single_trade_risk_uses_drawdown_capital(self, buy_signal):
        """回撤后单笔风险限额按总资产收缩，max_buy_quantity 同口径"""
        guard = RiskGuard()
        guard.max_single_stock_ratio = 1.0  # 排除集中度干扰
        drawn = Portfolio(
            total_capital=1_000_000, available_cash=500_000,
            total_market_value=0, total_pnl=0, risk_per_trade_pct=0.02,
        )
        # 限额 500,000 * 2% = 10,000；200 股 * 90 = 18,000 超限（按初始资金 20,000 会放行）
        passed, reason = guard._check_single_trade_risk(drawn, buy_signal, 200, 1800.0)
        assert not passed
        assert "10000.00" in reason
        assert guard._check_single_trade_risk(drawn, buy_signal, 100, 1800.0)[0]
        # 10,000 / 90 = 111 股 → 100 股
        assert guard.max_buy_quantity(drawn, buy_signal, 1800.0) == 100

    @pytest.mark.parametrize("stop_loss", [1800.0, 1850.0])
    def test_inverted_stop_loss_rejected(self, sample_portfolio, buy_signal, stop_loss):
        """止损价不低于买入价时拒绝买入（成交后会立刻触发止损）"""
        guard = RiskGuard()
        buy_signal.stop_loss_price = stop_loss
        passed, reason = guard.check_buy(
            portfolio=sample_portfolio,
            signal=buy_signal,
            quantity=100,
            price=1800.0,
        )
        assert not passed
        assert "止损价无效" in reason

    def test_unknown_stop_loss_not_blocked(self, sample_portfolio, buy_signal):
        """止损价为 0（未知）时不因止损有效性检查被拒"""
        guard = RiskGuard()
        buy_signal.stop_loss_price = 0.0
        passed, _ = guard.check_buy(
            portfolio=sample_portfolio,
            signal=buy_signal,
            quantity=100,
            price=1800.0,
        )
        assert passed

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


class TestRiskGuardNonFinite:
    """NaN / inf 数值不能让风控检查被静默放行（NaN 比较恒为 False）"""

    @staticmethod
    def _nan_position(code="600519", qty=100, cost=1800.0):
        nan = float("nan")
        return Position(
            stock_code=code, stock_name="", quantity=qty, avg_cost=cost,
            current_price=nan, market_value=nan, pnl=nan, pnl_pct=nan,
            open_time="",
        )

    def test_buy_rejects_nan_and_inf_price(self, sample_portfolio, buy_signal):
        guard = RiskGuard()
        for bad in (float("nan"), float("inf")):
            passed, reason = guard.check_buy(sample_portfolio, buy_signal, 100, bad)
            assert not passed
            assert "价格无效" in reason
        assert guard.max_buy_quantity(sample_portfolio, buy_signal, float("nan")) == 0

    def test_sell_rejects_nan_price(self, sample_portfolio):
        guard = RiskGuard()
        sample_portfolio.positions["600519"] = self._nan_position()
        passed, reason = guard.check_sell(sample_portfolio, "600519", 100, float("nan"))
        assert not passed
        assert "价格无效" in reason

    def test_nan_cash_rejects_buy(self, sample_portfolio, buy_signal):
        guard = RiskGuard()
        sample_portfolio.available_cash = float("nan")
        passed, reason = guard.check_buy(sample_portfolio, buy_signal, 100, 1800.0)
        assert not passed
        assert guard.max_buy_quantity(sample_portfolio, buy_signal, 1800.0) == 0

    def test_nan_market_value_uses_cost_for_concentration(self, sample_portfolio, buy_signal):
        """富途行情缺失时持仓市值为 NaN，加仓仍要按成本计入 30% 集中度"""
        guard = RiskGuard()
        # 已持有 1500 股 * 1800 = 270 万成本，远超总资产 100 万的 30%
        sample_portfolio.positions["600519"] = self._nan_position(qty=1500)
        passed, reason = guard.check_buy(sample_portfolio, buy_signal, 100, 1800.0)
        assert not passed
        assert "集中度" in reason
        assert guard.max_buy_quantity(sample_portfolio, buy_signal, 1800.0) == 0

    def test_nan_total_assets_rejects_buy(self, sample_portfolio, buy_signal):
        guard = RiskGuard()
        sample_portfolio.total_market_value = float("nan")
        passed, reason = guard.check_buy(sample_portfolio, buy_signal, 100, 1800.0)
        assert not passed
        assert guard.max_buy_quantity(sample_portfolio, buy_signal, 1800.0) == 0


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

    def test_fixed_fraction_shrinks_after_drawdown(self, buy_signal):
        """固定分数法：账户回撤后按总资产计算投入金额，不再按初始资金"""
        portfolio = Portfolio(
            total_capital=10_000_000,
            available_cash=5_000_000,  # 总资产回撤到 5,000,000
            total_market_value=0,
            total_pnl=0,
            positions={},
        )
        # 5,000,000 * 10% = 500,000 / 180,000 = 2 手
        # （按初始资金算 1,000,000 → 5 手，实际投入净值的 20%）
        assert fixed_fraction(portfolio, buy_signal, fraction=0.10) == 2

    def test_fixed_fraction_not_amplified_by_profit(self, buy_signal):
        """固定分数法：盈利后仍按初始资金计算，不放大仓位"""
        portfolio = Portfolio(
            total_capital=10_000_000,
            available_cash=20_000_000,
            total_market_value=0,
            total_pnl=0,
            positions={},
        )
        assert fixed_fraction(portfolio, buy_signal, fraction=0.10) == 5

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

    def test_kelly_shrinks_after_drawdown(self, buy_signal):
        """Kelly公式：回撤后按总资产计算，盈利后不放大"""
        # p=0.6, b=2 → f*=0.4，半Kelly 0.2
        portfolio = Portfolio(
            total_capital=10_000_000,
            available_cash=5_000_000,
            total_market_value=0,
            total_pnl=0,
            positions={},
        )
        # 5,000,000 * 0.2 = 1,000,000 / 180,000 = 5 手（按初始资金算是 11 手）
        assert kelly_criterion(portfolio, buy_signal, win_rate=0.6, win_loss_ratio=2.0) == 5
        portfolio.available_cash = 20_000_000
        # 10,000,000 * 0.2 = 2,000,000 / 180,000 = 11 手
        assert kelly_criterion(portfolio, buy_signal, win_rate=0.6, win_loss_ratio=2.0) == 11

    def test_atr_based_with_stop_loss(self, sample_portfolio, buy_signal):
        """ATR风险定量：有止损价时正确计算"""
        # total_capital=1,000,000 * 2% = 20,000 风险金额
        # 止损距离 = 1800 - 1710 = 90
        # 最大股数 = 20000 / 90 = 222
        # 手数 = 222 / 100 = 2
        lots = atr_based(sample_portfolio, buy_signal, risk_pct=0.02)
        assert isinstance(lots, int)
        assert lots == 2

    def test_atr_based_defaults_to_portfolio_risk_pct(self, sample_portfolio, buy_signal):
        """ATR风险定量：未传 risk_pct 时取组合的单笔风险比例，而非固定 2%"""
        sample_portfolio.risk_per_trade_pct = 0.04
        # 1,000,000 * 4% = 40,000 / 90 = 444 股 → 4 手
        assert atr_based(sample_portfolio, buy_signal) == 4
        sample_portfolio.risk_per_trade_pct = 0.01
        # 10,000 / 90 = 111 股 → 1 手
        assert atr_based(sample_portfolio, buy_signal) == 1

    def test_atr_based_shrinks_after_drawdown(self, sample_portfolio, buy_signal):
        """ATR风险定量：账户回撤后风险金额按总资产计算，不再按初始资金"""
        # 总资产回撤到 500,000：500,000 * 2% = 10,000 / 90 = 111 股 → 1 手
        # （按初始资金算 20,000 / 90 = 222 股 → 2 手，实际冒险 4% 净值；
        #  现金 475,000 / 1800 = 263 股，不构成约束）
        sample_portfolio.available_cash = 500_000
        sample_portfolio.total_market_value = 0
        assert atr_based(sample_portfolio, buy_signal) == 1

    def test_atr_based_not_amplified_by_profit(self, sample_portfolio, buy_signal):
        """ATR风险定量：盈利后仍按初始资金计算，不放大单笔风险"""
        sample_portfolio.available_cash = 1_800_000  # 总资产 2,000,000
        sample_portfolio.risk_per_trade_pct = 0.04
        assert atr_based(sample_portfolio, buy_signal) == 4

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

    def test_tight_stop_buy_capped_by_concentration(self, tmp_account_path):
        """止损很近时 ATR 仓位超过 30% 集中度，应下调到限额内成交而非整单拒绝"""
        from quant.agents.portfolio_manager import PortfolioManager

        broker = PaperBroker(account_path=tmp_account_path, initial_capital=1_000_000)
        pm = PortfolioManager(broker=broker, sizing_method="atr_based")
        signal = OrderSignal(
            stock_code="000001", stock_name="平安银行",
            signal_type=SignalType.BUY, confidence=ConfidenceLevel.HIGH,
            sentiment_score=75, ideal_buy_price=15.0, stop_loss_price=14.7,
            take_profit_price=18.0, buy_reason="测试紧止损",
        )
        # ATR: 20,000 / 0.3 = 66,666 股，现金上限 63,300 股，均远超 30% 集中度
        record = pm.process_signal(signal)
        assert record is not None
        assert record.status == TradeStatus.FILLED
        # 30% * 1,000,000 / 15 = 20,000 股
        assert record.quantity == 20_000

    def test_atr_sizing_uses_configured_risk_pct(self, tmp_account_path):
        """ATR 仓位应按配置的单笔风险比例计算，调高风险预算后仓位相应放大"""
        from quant.agents.portfolio_manager import PortfolioManager
        from quant.config import QuantConfig

        config = QuantConfig(
            initial_capital=1_000_000, sizing_method="atr_based",
            risk_per_trade_pct=0.03,
        )
        broker = PaperBroker(
            account_path=tmp_account_path, initial_capital=1_000_000,
            risk_per_trade_pct=0.03,
        )
        pm = PortfolioManager(broker=broker, sizing_method="atr_based", config=config)
        signal = OrderSignal(
            stock_code="000001", stock_name="平安银行",
            signal_type=SignalType.BUY, confidence=ConfidenceLevel.HIGH,
            sentiment_score=75, ideal_buy_price=15.0, stop_loss_price=13.0,
            take_profit_price=18.0, buy_reason="测试风险预算",
        )
        record = pm.process_signal(signal)
        assert record is not None
        assert record.status == TradeStatus.FILLED
        # 1,000,000 * 3% = 30,000 / 2 = 15,000 股（固定 2% 时只有 10,000 股）
        assert record.quantity == 15_000

    def test_inverted_stop_loss_signal_not_bought(self, tmp_account_path):
        """止损价倒挂的买入信号不应成交，避免建仓后立即被止损卖出"""
        from quant.agents.portfolio_manager import PortfolioManager

        broker = PaperBroker(account_path=tmp_account_path, initial_capital=1_000_000)
        pm = PortfolioManager(broker=broker, sizing_method="atr_based")
        signal = OrderSignal(
            stock_code="000001", stock_name="平安银行",
            signal_type=SignalType.BUY, confidence=ConfidenceLevel.HIGH,
            sentiment_score=75, ideal_buy_price=15.0, stop_loss_price=15.5,
            take_profit_price=18.0, buy_reason="测试止损倒挂",
        )
        assert pm.process_signal(signal) is None
        assert "000001" not in broker.get_portfolio().positions

    def test_sell_signal_uses_current_price(self, paper_broker, sell_signal):
        """卖出信号按持仓现价成交，而非信号里的买入狙击点 ideal_buy_price"""
        from quant.agents.portfolio_manager import PortfolioManager

        paper_broker.place_order("000858", "BUY", 1000, 160.0)
        paper_broker._portfolio.positions["000858"].last_buy_date = "2000-01-01"
        paper_broker.update_position_prices({"000858": 170.0})
        pm = PortfolioManager(broker=paper_broker)

        # sell_signal.ideal_buy_price=150，低于现价 170
        record = pm.process_signal(sell_signal)
        assert record.status == TradeStatus.FILLED
        assert record.price == 170.0
        assert record.quantity == 1000

    def test_sell_signal_falls_back_to_signal_price(self, paper_broker, sell_signal):
        """持仓现价缺失时才用信号价格兜底"""
        from quant.agents.portfolio_manager import PortfolioManager

        paper_broker.place_order("000858", "BUY", 1000, 160.0)
        paper_broker._portfolio.positions["000858"].last_buy_date = "2000-01-01"
        paper_broker._portfolio.positions["000858"].current_price = 0.0
        pm = PortfolioManager(broker=paper_broker)

        record = pm.process_signal(sell_signal)
        assert record.status == TradeStatus.FILLED
        assert record.price == 150.0

    def test_add_on_buy_capped_by_existing_position(self, tmp_account_path):
        """已持仓接近集中度上限时，加仓只买剩余额度；无额度则跳过"""
        from quant.agents.portfolio_manager import PortfolioManager

        broker = PaperBroker(account_path=tmp_account_path, initial_capital=1_000_000)
        broker.place_order("000001", "BUY", 19_000, 15.0)  # 285,000 ≈ 28.5%
        pm = PortfolioManager(broker=broker, sizing_method="fixed_fraction")
        signal = OrderSignal(
            stock_code="000001", stock_name="平安银行",
            signal_type=SignalType.BUY, confidence=ConfidenceLevel.HIGH,
            sentiment_score=75, ideal_buy_price=15.0, stop_loss_price=0.0,
            take_profit_price=18.0, buy_reason="测试加仓",
        )
        record = pm.process_signal(signal)
        assert record.status == TradeStatus.FILLED
        # 总资产 ≈ 999,914.5，30% 额度 ≈ 299,974 - 285,000 → 998 股 → 900 股
        assert record.quantity == 900

        assert pm.process_signal(signal) is None

    def test_max_buy_quantity_respects_cash_and_risk(self, sample_portfolio, buy_signal):
        """max_buy_quantity 同时受现金与单笔风险约束，并按整手取整"""
        guard = RiskGuard()
        # 集中度 300,000 / 1800 = 166 股，风险 20,000 / 90 = 222 股 → 取小 → 100 股
        assert guard.max_buy_quantity(sample_portfolio, buy_signal, 1800.0) == 100
        # 止损放宽到 1500：风险 20,000 / 300 = 66 股 → 不足一手
        wide = OrderSignal.from_dict({**buy_signal.to_dict(), "stop_loss_price": 1500.0})
        assert guard.max_buy_quantity(sample_portfolio, wide, 1800.0) == 0

        poor = Portfolio(
            total_capital=1_000_000, available_cash=100_000,
            total_market_value=900_000, total_pnl=0,
        )
        no_stop = OrderSignal.from_dict({**buy_signal.to_dict(), "stop_loss_price": 0.0})
        # 现金：95,000 / 1800 ≈ 52 股 → 不足一手
        assert guard.max_buy_quantity(poor, no_stop, 1800.0) == 0
        qty = guard.max_buy_quantity(poor, no_stop, 18.0)
        assert qty % 100 == 0
        assert guard._check_cash(poor, qty, 18.0)[0]
        assert not guard._check_cash(poor, qty + 100, 18.0)[0]

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

    def test_portfolio_risk_capital(self, sample_portfolio):
        """risk_capital = min(初始资金, 总资产)，总资产无效时退回初始资金"""
        assert sample_portfolio.risk_capital == 1_000_000
        sample_portfolio.available_cash = 500_000
        assert sample_portfolio.risk_capital == 700_000
        sample_portfolio.available_cash = 2_000_000
        assert sample_portfolio.risk_capital == 1_000_000
        sample_portfolio.available_cash = 0
        sample_portfolio.total_market_value = 0
        assert sample_portfolio.risk_capital == 1_000_000

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

    @staticmethod
    def _held_orchestrator(tmp_account_path, price_fetcher):
        """建一个持有 600519（成本 1800，止损 1710，止盈 2000，已过 T+1）的编排器"""
        from quant.config import QuantConfig
        from quant.orchestrator import QuantOrchestrator

        config = QuantConfig(paper_account_path=tmp_account_path)
        broker = PaperBroker(account_path=tmp_account_path, initial_capital=1_000_000)
        broker.place_order("600519", "BUY", 100, 1800.0)
        pos = broker._portfolio.positions["600519"]
        pos.last_buy_date = "2000-01-01"
        broker.update_stop_loss("600519", 1710.0)
        broker.update_take_profit("600519", 2000.0)
        return QuantOrchestrator(broker=broker, config=config, price_fetcher=price_fetcher), broker

    def test_run_refreshes_prices_before_stop_loss(self, tmp_account_path):
        """跑流程前先刷新持仓现价，跌破止损价时能触发止损（此前一直用买入价比较）"""
        fetcher = MagicMock(return_value={"600519": 1700.0})
        orchestrator, broker = self._held_orchestrator(tmp_account_path, fetcher)

        with patch.object(orchestrator._signal_aggregator, 'aggregate', return_value=[]):
            report = orchestrator.run(["600519"], dry_run=False)

        fetcher.assert_called_once_with(["600519"])
        assert "600519" not in broker.get_portfolio().positions
        assert len(report['executed_trades']) == 1
        assert report['executed_trades'][0]['price'] == 1700.0

    def test_run_refreshes_prices_before_take_profit(self, tmp_account_path):
        """刷新后现价达到止盈价时触发止盈"""
        orchestrator, broker = self._held_orchestrator(
            tmp_account_path, lambda codes: {"600519": 2050.0}
        )

        with patch.object(orchestrator._signal_aggregator, 'aggregate', return_value=[]):
            report = orchestrator.run(["600519"], dry_run=False)

        assert "600519" not in broker.get_portfolio().positions
        assert report['executed_trades'][0]['price'] == 2050.0

    def test_refresh_fetcher_failure_keeps_old_price(self, tmp_account_path):
        """行情获取异常时沿用旧价，不误触发止损，流程不报错"""
        def boom(codes):
            raise RuntimeError("network down")

        orchestrator, broker = self._held_orchestrator(tmp_account_path, boom)

        assert orchestrator.refresh_position_prices() == 0
        assert orchestrator.run_stop_loss_check() == []
        assert broker.get_portfolio().positions["600519"].current_price == 1800.0

    def test_dry_run_skips_price_refresh(self, tmp_account_path):
        """dry_run 不刷新行情（不需要止损止盈）"""
        fetcher = MagicMock(return_value={})
        orchestrator, _ = self._held_orchestrator(tmp_account_path, fetcher)

        with patch.object(orchestrator._signal_aggregator, 'aggregate', return_value=[]):
            orchestrator.run(["600519"], dry_run=True)

        fetcher.assert_not_called()

    def test_update_position_prices_ignores_invalid(self, paper_broker):
        """0 / 负数 / None / NaN 价格被忽略，避免停牌返回 0 时误触发止损"""
        paper_broker.place_order("600519", "BUY", 100, 1800.0)
        paper_broker.update_stop_loss("600519", 1710.0)

        paper_broker.update_position_prices({"600519": 0})
        paper_broker.update_position_prices({"600519": -1})
        paper_broker.update_position_prices({"600519": None})
        paper_broker.update_position_prices({"600519": float("nan")})

        pos = paper_broker.get_portfolio().positions["600519"]
        assert pos.current_price == 1800.0
        assert RiskGuard().check_stop_loss(paper_broker.get_portfolio()) == []

        paper_broker.update_position_prices({"600519": "1850.5"})
        assert paper_broker.get_portfolio().positions["600519"].current_price == 1850.5

    @staticmethod
    def _rebuy_signal(code="600519", price=1700.0):
        return OrderSignal(
            stock_code=code, stock_name="测试", signal_type=SignalType.BUY,
            confidence=ConfidenceLevel.HIGH, sentiment_score=80,
            ideal_buy_price=price, stop_loss_price=price * 0.95,
            take_profit_price=price * 1.2, buy_reason="同轮买回测试",
        )

    def test_no_rebuy_after_stop_loss_same_run(self, tmp_account_path):
        """同一轮止损卖出的股票，即使有买入信号也不当日买回"""
        orchestrator, broker = self._held_orchestrator(
            tmp_account_path, lambda codes: {"600519": 1700.0}
        )

        with patch.object(
            orchestrator._signal_aggregator, 'aggregate',
            return_value=[self._rebuy_signal()],
        ):
            report = orchestrator.run(["600519"], dry_run=False)

        assert "600519" not in broker.get_portfolio().positions
        assert [t['action'] for t in report['executed_trades']] == ["SELL"]
        assert report['blocked_rebuys'] == ["600519"]

    def test_no_rebuy_after_take_profit_same_run(self, tmp_account_path):
        """同一轮止盈卖出的股票也不当日买回"""
        orchestrator, broker = self._held_orchestrator(
            tmp_account_path, lambda codes: {"600519": 2050.0}
        )

        with patch.object(
            orchestrator._signal_aggregator, 'aggregate',
            return_value=[self._rebuy_signal(price=2050.0)],
        ):
            report = orchestrator.run(["600519"], dry_run=False)

        assert "600519" not in broker.get_portfolio().positions
        assert report['blocked_rebuys'] == ["600519"]

    def test_other_buy_signals_unaffected_by_exit(self, tmp_account_path):
        """止损只拦截离场股票本身，其他买入信号照常执行"""
        orchestrator, broker = self._held_orchestrator(
            tmp_account_path, lambda codes: {"600519": 1700.0}
        )

        with patch.object(
            orchestrator._signal_aggregator, 'aggregate',
            return_value=[self._rebuy_signal(), self._rebuy_signal("000001", 15.0)],
        ):
            report = orchestrator.run(["600519", "000001"], dry_run=False)

        assert report['blocked_rebuys'] == ["600519"]
        assert "000001" in broker.get_portfolio().positions

    def test_deferred_exit_does_not_block_buy(self, tmp_account_path):
        """T+1 顺延未成交的止损不算离场，不拦截买入（仍持仓，走正常加仓风控）"""
        orchestrator, broker = self._held_orchestrator(
            tmp_account_path, lambda codes: {"600519": 1700.0}
        )
        broker._portfolio.positions["600519"].last_buy_date = None
        broker._portfolio.positions["600519"].record_buy(100)

        with patch.object(
            orchestrator._signal_aggregator, 'aggregate',
            return_value=[self._rebuy_signal()],
        ):
            report = orchestrator.run(["600519"], dry_run=False)

        assert report['blocked_rebuys'] == []

    @staticmethod
    def _pending_sell_broker(broker):
        """让卖单像富途限价单一样返回 PENDING：持仓不变，记录所有卖单"""
        sells = []

        def place_order(stock_code, action, quantity, price, order_type="LIMIT", **kwargs):
            trade_action = TradeAction(action)
            if trade_action == TradeAction.SELL:
                sells.append(stock_code)
                return TradeRecord.create(
                    stock_code=stock_code, action=trade_action, quantity=quantity,
                    price=price, commission=0.0, status=TradeStatus.PENDING,
                )
            return PaperBroker.place_order(
                broker, stock_code, action, quantity, price, order_type, **kwargs
            )

        broker.place_order = place_order
        return sells

    def test_pending_stop_loss_blocks_rebuy(self, tmp_account_path):
        """止损卖单待成交（富途限价单）时持仓还在，同一轮也不能买回"""
        orchestrator, broker = self._held_orchestrator(
            tmp_account_path, lambda codes: {"600519": 1700.0}
        )
        sells = self._pending_sell_broker(broker)

        with patch.object(
            orchestrator._signal_aggregator, 'aggregate',
            return_value=[self._rebuy_signal()],
        ):
            report = orchestrator.run(["600519"], dry_run=False)

        assert sells == ["600519"]
        assert [t['action'] for t in report['executed_trades']] == ["SELL"]
        assert report['blocked_rebuys'] == ["600519"]

    def test_pending_stop_loss_not_sold_twice(self, tmp_account_path):
        """止损卖单待成交时，同一轮的止盈和卖出信号不再重复下卖单"""
        orchestrator, broker = self._held_orchestrator(
            tmp_account_path, lambda codes: {"600519": 1700.0}
        )
        broker._portfolio.positions["600519"].take_profit_price = 1650.0
        sells = self._pending_sell_broker(broker)
        sell_signal = replace(self._rebuy_signal(), signal_type=SignalType.SELL)

        with patch.object(
            orchestrator._signal_aggregator, 'aggregate', return_value=[sell_signal],
        ):
            orchestrator.run(["600519"], dry_run=False)

        assert sells == ["600519"]

    def test_rejected_stop_loss_does_not_block_buy(self, tmp_account_path):
        """止损卖单被拒时没有离场，不拦截后续卖出信号和买入"""
        orchestrator, broker = self._held_orchestrator(
            tmp_account_path, lambda codes: {"600519": 1700.0}
        )

        def reject(stock_code, action, quantity, price, order_type="LIMIT", **kwargs):
            return TradeRecord.create(
                stock_code=stock_code, action=TradeAction(action), quantity=quantity,
                price=price, commission=0.0, status=TradeStatus.REJECTED,
            )

        broker.place_order = reject

        with patch.object(
            orchestrator._signal_aggregator, 'aggregate',
            return_value=[self._rebuy_signal()],
        ):
            report = orchestrator.run(["600519"], dry_run=False)

        assert report['blocked_rebuys'] == []

    @staticmethod
    def _empty_orchestrator(tmp_account_path, price_fetcher):
        from quant.config import QuantConfig
        from quant.orchestrator import QuantOrchestrator

        config = QuantConfig(paper_account_path=tmp_account_path)
        broker = PaperBroker(account_path=tmp_account_path, initial_capital=1_000_000)
        return QuantOrchestrator(broker=broker, config=config, price_fetcher=price_fetcher), broker

    def test_buy_skipped_when_market_above_limit(self, tmp_account_path):
        """现价高于狙击点时限价单不成交，不按市场没到过的低价买入"""
        orchestrator, broker = self._empty_orchestrator(
            tmp_account_path, lambda codes: {"000001": 15.5}
        )

        with patch.object(
            orchestrator._signal_aggregator, 'aggregate',
            return_value=[self._rebuy_signal("000001", 15.0)],
        ):
            report = orchestrator.run(["000001"], dry_run=False)

        assert "000001" not in broker.get_portfolio().positions
        assert report['executed_trades'] == []
        assert report['unfilled_limit_buys'] == ["000001"]

    def test_buy_fills_at_market_when_below_limit(self, tmp_account_path):
        """现价低于狙击点时按现价成交，止损/止盈仍沿用信号价"""
        orchestrator, broker = self._empty_orchestrator(
            tmp_account_path, lambda codes: {"000001": 14.8}
        )

        with patch.object(
            orchestrator._signal_aggregator, 'aggregate',
            return_value=[self._rebuy_signal("000001", 15.0)],
        ):
            report = orchestrator.run(["000001"], dry_run=False)

        pos = broker.get_portfolio().positions["000001"]
        assert pos.avg_cost == 14.8
        assert pos.stop_loss_price == 15.0 * 0.95
        assert report['executed_trades'][0]['price'] == 14.8
        assert report['unfilled_limit_buys'] == []

    @pytest.mark.parametrize("prices", [{}, {"000001": 0}, {"000001": float("nan")}, None])
    def test_buy_falls_back_to_signal_price_without_quote(self, tmp_account_path, prices):
        """行情缺失 / 无效时沿用信号价买入，不阻断流程"""
        orchestrator, broker = self._empty_orchestrator(tmp_account_path, lambda codes: prices)

        with patch.object(
            orchestrator._signal_aggregator, 'aggregate',
            return_value=[self._rebuy_signal("000001", 15.0)],
        ):
            orchestrator.run(["000001"], dry_run=False)

        assert broker.get_portfolio().positions["000001"].avg_cost == 15.0

    def test_buy_quote_fetch_error_falls_back(self, tmp_account_path):
        """行情接口异常时沿用信号价买入"""
        def boom(codes):
            raise RuntimeError("network down")

        orchestrator, broker = self._empty_orchestrator(tmp_account_path, boom)

        with patch.object(
            orchestrator._signal_aggregator, 'aggregate',
            return_value=[self._rebuy_signal("000001", 15.0)],
        ):
            report = orchestrator.run(["000001"], dry_run=False)

        assert broker.get_portfolio().positions["000001"].avg_cost == 15.0
        assert report['unfilled_limit_buys'] == []


# ===================================================================
# A 股 T+1 规则
# ===================================================================

class TestTPlusOne:
    """当日买入的股份当日不可卖出"""

    def test_same_day_sell_rejected(self, t1_broker):
        t1_broker.place_order("600519", "BUY", 100, 1800.0)
        record = t1_broker.place_order("600519", "SELL", 100, 1850.0)
        assert record.status == TradeStatus.REJECTED
        assert "T+1" in record.reason
        assert t1_broker.get_portfolio().positions["600519"].quantity == 100

    def test_previous_day_position_sellable(self, t1_broker):
        t1_broker.place_order("600519", "BUY", 100, 1800.0)
        pos = t1_broker._portfolio.positions["600519"]
        pos.last_buy_date = "2000-01-01"
        record = t1_broker.place_order("600519", "SELL", 100, 1850.0)
        assert record.status == TradeStatus.FILLED

    def test_add_today_locks_only_new_shares(self, t1_broker):
        t1_broker.place_order("600519", "BUY", 100, 1800.0)
        t1_broker._portfolio.positions["600519"].last_buy_date = "2000-01-01"
        t1_broker.place_order("600519", "BUY", 200, 1800.0)

        pos = t1_broker.get_portfolio().positions["600519"]
        assert pos.sellable_quantity() == 100
        assert t1_broker.place_order("600519", "SELL", 200, 1850.0).status == TradeStatus.REJECTED
        assert t1_broker.place_order("600519", "SELL", 100, 1850.0).status == TradeStatus.FILLED
        assert t1_broker.get_portfolio().positions["600519"].quantity == 200

    def test_same_day_buys_accumulate(self):
        pos = Position(
            stock_code="600519", stock_name="", quantity=300, avg_cost=10.0,
            current_price=10.0, market_value=3000.0, pnl=0.0, pnl_pct=0.0,
            open_time="2026-01-05T10:00:00",
        )
        pos.record_buy(100, today="2026-01-05")
        pos.record_buy(100, today="2026-01-05")
        assert pos.locked_quantity("2026-01-05") == 200
        assert pos.sellable_quantity("2026-01-06") == 300

    def test_t_plus_one_persists_across_instances(self, tmp_account_path):
        PaperBroker(account_path=tmp_account_path).place_order("600519", "BUY", 100, 1800.0)
        reloaded = PaperBroker(account_path=tmp_account_path)
        record = reloaded.place_order("600519", "SELL", 100, 1850.0)
        assert record.status == TradeStatus.REJECTED

    def test_legacy_account_backfilled_from_open_time(self, tmp_account_path):
        from datetime import datetime
        broker = PaperBroker(account_path=tmp_account_path)
        broker.place_order("600519", "BUY", 100, 1800.0)
        with open(tmp_account_path, encoding="utf-8") as f:
            data = json.load(f)
        legacy = data["portfolio"]["positions"]["600519"]
        legacy.pop("last_buy_date")
        legacy.pop("today_buy_qty")
        legacy["open_time"] = datetime.now().isoformat()
        with open(tmp_account_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        reloaded = PaperBroker(account_path=tmp_account_path)
        assert reloaded.get_portfolio().positions["600519"].sellable_quantity() == 0

    def test_risk_guard_rejects_locked_shares(self, t1_broker):
        t1_broker.place_order("600519", "BUY", 100, 1800.0)
        passed, reason = RiskGuard().check_sell(t1_broker.get_portfolio(), "600519", 100, 1850.0)
        assert not passed
        assert "T+1" in reason

    def test_rebalance_sells_only_sellable_shares(self, t1_broker):
        """昨日持仓 100 股 + 今日加仓 200 股，亏损超 5% 时只卖出可卖的 100 股"""
        from quant.agents.portfolio_manager import PortfolioManager

        t1_broker.place_order("600519", "BUY", 100, 1800.0)
        t1_broker._portfolio.positions["600519"].last_buy_date = "2000-01-01"
        t1_broker.place_order("600519", "BUY", 200, 1800.0)
        t1_broker.update_position_prices({"600519": 1600.0})

        records = PortfolioManager(broker=t1_broker).rebalance()
        assert len(records) == 1
        assert records[0].status == TradeStatus.FILLED
        assert records[0].quantity == 100
        assert t1_broker.get_portfolio().positions["600519"].quantity == 200

    def test_rebalance_defers_fully_locked_position(self, t1_broker):
        """当日建仓即亏损超 5%，再平衡应顺延而不是下单被拒"""
        from quant.agents.portfolio_manager import PortfolioManager

        t1_broker.place_order("600519", "BUY", 100, 1800.0)
        t1_broker.update_position_prices({"600519": 1600.0})

        assert PortfolioManager(broker=t1_broker).rebalance() == []
        assert t1_broker.get_portfolio().positions["600519"].quantity == 100



# ===================================================================
# 止盈
# ===================================================================

class TestTakeProfit:
    """信号止盈价写入持仓，价格达到止盈价时自动卖出"""

    def _tp_signal(self, take_profit=18.0, buy_price=15.0):
        return OrderSignal(
            stock_code="000001", stock_name="平安银行",
            signal_type=SignalType.BUY, confidence=ConfidenceLevel.HIGH,
            sentiment_score=75, ideal_buy_price=buy_price, stop_loss_price=13.5,
            take_profit_price=take_profit, buy_reason="测试止盈",
        )

    def test_buy_fill_records_take_profit(self, paper_broker):
        from quant.agents.order_executor import OrderExecutor

        record = OrderExecutor(paper_broker).execute_buy(self._tp_signal(), 1000)
        assert record.status == TradeStatus.FILLED
        pos = paper_broker.get_portfolio().positions["000001"]
        assert pos.take_profit_price == 18.0
        assert pos.stop_loss_price == 13.5

    def test_invalid_take_profit_not_recorded(self, paper_broker):
        """止盈价不高于买入价时不写入，避免买入即止盈"""
        from quant.agents.order_executor import OrderExecutor

        OrderExecutor(paper_broker).execute_buy(self._tp_signal(take_profit=14.0), 1000)
        assert paper_broker.get_portfolio().positions["000001"].take_profit_price is None

    def test_rejected_add_on_keeps_existing_exit_prices(self, paper_broker):
        """加仓被拒时不能用新信号覆盖已有持仓的止损/止盈"""
        from quant.agents.order_executor import OrderExecutor

        executor = OrderExecutor(paper_broker)
        executor.execute_buy(self._tp_signal(), 1000)
        huge = OrderSignal.from_dict({
            **self._tp_signal().to_dict(), "stop_loss_price": 14.0, "take_profit_price": 30.0,
        })
        record = executor.execute_buy(huge, 10_000_000)  # 资金不足被拒
        assert record.status == TradeStatus.REJECTED
        pos = paper_broker.get_portfolio().positions["000001"]
        assert pos.stop_loss_price == 13.5
        assert pos.take_profit_price == 18.0

    def test_add_on_does_not_lower_stop_loss(self, paper_broker):
        """加仓信号止损更低时保留原止损，不把老仓位的止损一起下移"""
        from quant.agents.order_executor import OrderExecutor

        executor = OrderExecutor(paper_broker)
        executor.execute_buy(self._tp_signal(), 1000)  # 止损 13.5
        lower = OrderSignal.from_dict({**self._tp_signal().to_dict(), "stop_loss_price": 12.0})
        assert executor.execute_buy(lower, 1000).status == TradeStatus.FILLED
        pos = paper_broker.get_portfolio().positions["000001"]
        assert pos.quantity == 2000
        assert pos.stop_loss_price == 13.5

    def test_add_on_raises_stop_loss(self, paper_broker):
        """加仓信号止损更高时上移止损"""
        from quant.agents.order_executor import OrderExecutor

        executor = OrderExecutor(paper_broker)
        executor.execute_buy(self._tp_signal(), 1000)
        higher = OrderSignal.from_dict({**self._tp_signal().to_dict(), "stop_loss_price": 14.0})
        executor.execute_buy(higher, 1000)
        assert paper_broker.get_portfolio().positions["000001"].stop_loss_price == 14.0

    def test_add_on_rejected_when_existing_stop_above_price(self, paper_broker):
        """已有持仓止损价不低于加仓价时拒绝加仓，避免成交即止损"""
        paper_broker.place_order("000001", "BUY", 1000, 15.0)
        paper_broker.update_stop_loss("000001", 14.5)
        signal = self._tp_signal(buy_price=14.4)  # 新信号止损 13.5 本身有效
        passed, reason = RiskGuard().check_buy(paper_broker.get_portfolio(), signal, 100, 14.4)
        assert not passed
        assert "加仓无效" in reason

        passed, _ = RiskGuard().check_buy(paper_broker.get_portfolio(), signal, 100, 14.6)
        assert passed

    def test_risk_guard_detects_take_profit(self, paper_broker):
        paper_broker.place_order("000001", "BUY", 1000, 15.0)
        paper_broker.update_take_profit("000001", 18.0)
        guard = RiskGuard()

        paper_broker.update_position_prices({"000001": 17.9})
        assert guard.check_take_profit(paper_broker.get_portfolio()) == []
        paper_broker.update_position_prices({"000001": 18.0})
        assert guard.check_take_profit(paper_broker.get_portfolio()) == ["000001"]

    def test_check_and_take_profit_sells_position(self, paper_broker):
        from quant.agents.portfolio_manager import PortfolioManager

        paper_broker.place_order("000001", "BUY", 1000, 15.0)
        paper_broker._portfolio.positions["000001"].last_buy_date = "2000-01-01"
        paper_broker.update_take_profit("000001", 18.0)
        paper_broker.update_position_prices({"000001": 18.5})

        records = PortfolioManager(broker=paper_broker).check_and_take_profit()
        assert len(records) == 1
        assert records[0].status == TradeStatus.FILLED
        assert records[0].quantity == 1000
        assert "000001" not in paper_broker.get_portfolio().positions

    def test_take_profit_defers_under_t_plus_one(self, t1_broker):
        from quant.agents.portfolio_manager import PortfolioManager

        t1_broker.place_order("000001", "BUY", 1000, 15.0)
        t1_broker.update_take_profit("000001", 18.0)
        t1_broker.update_position_prices({"000001": 18.5})

        assert PortfolioManager(broker=t1_broker).check_and_take_profit() == []
        assert t1_broker.get_portfolio().positions["000001"].quantity == 1000


class TestTrailingStop:
    """移动止损：持仓浮盈后从最高价回撤 trailing_stop_pct 即止损"""

    def _position(self, current, highest, stop=13.5):
        return Position(
            stock_code="000001", stock_name="平安银行", quantity=1000,
            avg_cost=15.0, current_price=current, market_value=current * 1000,
            pnl=(current - 15.0) * 1000, pnl_pct=(current - 15.0) / 15.0 * 100,
            open_time="2000-01-01", stop_loss_price=stop, highest_price=highest,
            last_buy_date="2000-01-01", today_buy_qty=1000,
        )

    def _portfolio(self, pos):
        return Portfolio(
            total_capital=1_000_000, available_cash=985_000,
            total_market_value=pos.market_value, total_pnl=pos.pnl,
            positions={pos.stock_code: pos},
        )

    def test_disabled_by_default(self):
        guard = RiskGuard()
        pos = self._position(current=16.0, highest=20.0)
        assert guard.effective_stop_price(pos) == 13.5
        assert guard.check_stop_loss(self._portfolio(pos)) == []

    def test_triggers_after_pullback_from_high(self):
        guard = RiskGuard(trailing_stop_pct=0.10)
        pos = self._position(current=17.9, highest=20.0)
        assert guard.effective_stop_price(pos) == pytest.approx(18.0)
        assert guard.check_stop_loss(self._portfolio(pos)) == ["000001"]

    def test_no_trigger_within_pullback(self):
        guard = RiskGuard(trailing_stop_pct=0.10)
        pos = self._position(current=18.5, highest=20.0)
        assert guard.check_stop_loss(self._portfolio(pos)) == []

    def test_inactive_before_profit(self):
        """最高价未超过成本价时只用信号止损价，不提前收紧初始风险"""
        guard = RiskGuard(trailing_stop_pct=0.05)
        pos = self._position(current=14.0, highest=15.0)
        assert guard.effective_stop_price(pos) == 13.5
        assert guard.check_stop_loss(self._portfolio(pos)) == []

    def test_works_without_fixed_stop(self):
        guard = RiskGuard(trailing_stop_pct=0.10)
        pos = self._position(current=17.5, highest=20.0, stop=None)
        assert guard.check_stop_loss(self._portfolio(pos)) == ["000001"]

    def test_fixed_stop_wins_when_higher(self):
        guard = RiskGuard(trailing_stop_pct=0.20)
        pos = self._position(current=16.5, highest=16.0, stop=15.5)
        assert guard.effective_stop_price(pos) == 15.5

    def test_invalid_pct_disables(self):
        assert RiskGuard(trailing_stop_pct=1.5).trailing_stop_pct == 0.0
        assert RiskGuard(trailing_stop_pct=-0.1).trailing_stop_pct == 0.0

    @pytest.mark.parametrize("bad_price", [0.0, -1.0, float("nan"), None])
    def test_invalid_current_price_skips_stop_and_take_profit(self, bad_price):
        """富途行情缺失时现价为 0 / NaN，不能当成跌破止损或达到止盈"""
        guard = RiskGuard(trailing_stop_pct=0.10)
        pos = self._position(current=16.0, highest=20.0)
        pos.current_price = bad_price
        pos.take_profit_price = 18.0
        portfolio = self._portfolio(pos)
        assert guard.check_stop_loss(portfolio) == []
        assert guard.check_take_profit(portfolio) == []

    def test_invalid_price_does_not_block_other_positions(self):
        guard = RiskGuard()
        bad = self._position(current=16.0, highest=None)
        bad.current_price = 0.0
        good = self._position(current=13.0, highest=None)
        good.stock_code = "600000"
        portfolio = self._portfolio(bad)
        portfolio.positions["600000"] = good
        assert guard.check_stop_loss(portfolio) == ["600000"]

    def test_reads_pct_from_config(self, monkeypatch):
        from quant.config import QuantConfig

        monkeypatch.setenv("QUANT_TRAILING_STOP_PCT", "0.08")
        config = QuantConfig.from_env()
        assert config.trailing_stop_pct == 0.08
        assert RiskGuard(config=config).trailing_stop_pct == 0.08

    def test_check_and_stop_loss_sells_on_trailing(self, paper_broker):
        from quant.agents.portfolio_manager import PortfolioManager
        from quant.config import QuantConfig

        paper_broker.place_order("000001", "BUY", 1000, 15.0)
        paper_broker._portfolio.positions["000001"].last_buy_date = "2000-01-01"
        paper_broker.update_stop_loss("000001", 13.5)
        paper_broker.update_position_prices({"000001": 20.0})
        paper_broker.update_position_prices({"000001": 17.8})

        manager = PortfolioManager(
            broker=paper_broker, config=QuantConfig(trailing_stop_pct=0.10),
        )
        pos = paper_broker.get_portfolio().positions["000001"]
        assert "移动止损" in manager._stop_loss_reason(pos)
        records = manager.check_and_stop_loss()
        assert len(records) == 1
        assert records[0].status == TradeStatus.FILLED
        assert "000001" not in paper_broker.get_portfolio().positions

    def test_add_on_buy_updates_highest_price(self, paper_broker):
        """加仓价高于历史最高价时同步刷新最高价"""
        paper_broker.place_order("000001", "BUY", 1000, 15.0)
        paper_broker.place_order("000001", "BUY", 1000, 16.0)
        assert paper_broker.get_portfolio().positions["000001"].highest_price == 16.0


# ===================================================================
# 配置校验测试
# ===================================================================

class TestQuantConfig:
    """QuantConfig 启动校验与富途交易环境防误配测试"""

    def test_default_config_valid(self):
        from quant.config import QuantConfig
        cfg = QuantConfig()
        assert cfg.futu_trade_env == "SIMULATE"
        assert cfg.broker_type == "paper"

    def test_trade_env_and_broker_type_normalized(self):
        from quant.config import QuantConfig
        cfg = QuantConfig(futu_trade_env=" simulate ", broker_type="FUTU")
        assert cfg.futu_trade_env == "SIMULATE"
        assert cfg.broker_type == "futu"

    @pytest.mark.parametrize("env", ["SIMULATED", "sim", "", "live"])
    def test_invalid_trade_env_rejected(self, env):
        from quant.config import QuantConfig
        with pytest.raises(ValueError, match="futu_trade_env"):
            QuantConfig(futu_trade_env=env)

    @pytest.mark.parametrize("field_name,value", [
        ("risk_per_trade_pct", 2),       # 把 2% 误写成 2
        ("risk_per_trade_pct", 0),
        ("fixed_fraction", 1.5),
        ("kelly_win_rate", 1.0),
        ("kelly_win_loss_ratio", 0),
        ("max_positions", 0),
        ("initial_capital", -1),
        ("trailing_stop_pct", 1.0),
        ("sizing_method", "martingale"),
        ("broker_type", "ib"),
        ("max_signal_workers", 0),
    ])
    def test_invalid_field_rejected(self, field_name, value):
        from quant.config import QuantConfig
        with pytest.raises(ValueError, match=field_name):
            QuantConfig(**{field_name: value})

    def test_all_errors_reported_together(self):
        from quant.config import QuantConfig
        with pytest.raises(ValueError) as exc:
            QuantConfig(risk_per_trade_pct=2, max_positions=0)
        assert "risk_per_trade_pct" in str(exc.value)
        assert "max_positions" in str(exc.value)

    def test_from_env_rejects_lowercase_typo(self, monkeypatch):
        from quant.config import QuantConfig
        monkeypatch.setenv("QUANT_FUTU_TRADE_ENV", "simulate")
        assert QuantConfig.from_env().futu_trade_env == "SIMULATE"
        monkeypatch.setenv("QUANT_FUTU_TRADE_ENV", "SIMULATION")
        with pytest.raises(ValueError):
            QuantConfig.from_env()

    def test_futu_broker_rejects_invalid_trade_env(self):
        """拼错的交易环境在连接 OpenD 之前就被拒绝，不会落到真实盘"""
        from quant.broker.futu_broker import FutuBroker
        with patch("quant.broker.futu_broker.FutuBroker._connect") as connect:
            with pytest.raises(ValueError, match="futu_trade_env"):
                FutuBroker(trade_env="simulated")
            connect.assert_not_called()

    def test_futu_broker_lowercase_simulate_stays_simulate(self):
        """小写 simulate 规范化后仍连模拟盘（修复前会落到 REAL）"""
        import quant.broker.futu_broker as fb
        fake_ft = MagicMock()
        with patch.object(fb, "FUTU_AVAILABLE", True), \
                patch.object(fb, "ft", fake_ft, create=True):
            broker = fb.FutuBroker(trade_env="simulate")
        assert broker.trade_env == "SIMULATE"
        assert broker.trd_env is fake_ft.TrdEnv.SIMULATE


class TestFutuRiskState:
    """富途持仓的止损 / 止盈 / 最高价保存在本地状态文件并合并回持仓"""

    @staticmethod
    def _make_broker(tmp_path, rows):
        import pandas as pd
        import quant.broker.futu_broker as fb
        fake_ft = MagicMock()
        fake_ft.RET_OK = 0
        with patch.object(fb, "FUTU_AVAILABLE", True), \
                patch.object(fb, "ft", fake_ft, create=True):
            broker = fb.FutuBroker(risk_state_path=str(tmp_path / "risk.json"))
        broker._get_accounts = lambda: ["1"]
        broker._rows = rows
        broker._trade_ctx.position_list_query.side_effect = (
            lambda **kw: (0, pd.DataFrame(broker._rows))
        )
        return broker, fb, fake_ft

    @staticmethod
    def _row(code, price, cost=10.0, qty=100):
        return {"code": f"SH.{code}", "qty": qty, "cost_price": cost,
                "nominal_price": price, "pl_val": 0.0, "pl_ratio": 0.0,
                "stock_name": code}

    def test_stop_loss_merged_and_persisted(self, tmp_path):
        broker, fb, fake_ft = self._make_broker(tmp_path, [self._row("600519", 10.0)])
        with patch.object(fb, "ft", fake_ft, create=True):
            broker.update_stop_loss("600519", 9.5)
            broker.update_take_profit("600519", 12.0)
            pos = broker.get_positions()[0]
        assert pos.stop_loss_price == 9.5
        assert pos.take_profit_price == 12.0
        # 重启后仍能读到
        broker2, fb, fake_ft = self._make_broker(tmp_path, [self._row("600519", 9.4)])
        with patch.object(fb, "ft", fake_ft, create=True):
            positions = broker2.get_positions()
        assert positions[0].stop_loss_price == 9.5
        pf = Portfolio(total_capital=1e5, available_cash=1e5, total_market_value=0,
                       total_pnl=0, positions={p.stock_code: p for p in positions})
        assert RiskGuard().check_stop_loss(pf) == ["600519"]

    def test_highest_price_only_rises_for_trailing_stop(self, tmp_path):
        broker, fb, fake_ft = self._make_broker(tmp_path, [self._row("600519", 11.0)])
        with patch.object(fb, "ft", fake_ft, create=True):
            broker.get_positions()
            broker._rows = [self._row("600519", 10.2)]
            pos = broker.get_positions()[0]
        assert pos.highest_price == 11.0
        assert RiskGuard(trailing_stop_pct=0.05).effective_stop_price(pos) == pytest.approx(10.45)

    def test_closed_position_state_pruned(self, tmp_path):
        broker, fb, fake_ft = self._make_broker(tmp_path, [self._row("600519", 11.0)])
        with patch.object(fb, "ft", fake_ft, create=True):
            broker.update_stop_loss("600519", 9.5)
            broker.get_positions()
            # 当日写入的状态即使暂未持仓（挂单未成交）也保留
            broker._rows = []
            broker.get_positions()
            assert "600519" in broker._risk_state
            # 次日仍不在持仓中 -> 视为已平仓，清理掉旧止损和旧最高价
            broker._risk_state["600519"]["updated"] = "2000-01-01"
            broker.get_positions()
        assert "600519" not in broker._risk_state
        assert json.load(open(tmp_path / "risk.json")) == {}

    def test_query_failure_does_not_prune(self, tmp_path):
        broker, fb, fake_ft = self._make_broker(tmp_path, [])
        broker._risk_state = {"600519": {"stop_loss_price": 9.5, "updated": "2000-01-01"}}
        broker._trade_ctx.position_list_query.side_effect = lambda **kw: (-1, "err")
        with patch.object(fb, "ft", fake_ft, create=True):
            assert broker.get_positions() == []
        assert "600519" in broker._risk_state

    def test_can_sell_qty_limits_sellable(self, tmp_path):
        """止损卖单挂出未成交（can_sell_qty=0）时，下一轮不能对同一批股份再下卖单"""
        from quant.agents.portfolio_manager import PortfolioManager
        row = dict(self._row("600519", 9.0), can_sell_qty=0)
        broker, fb, fake_ft = self._make_broker(tmp_path, [row])
        with patch.object(fb, "ft", fake_ft, create=True):
            broker.update_stop_loss("600519", 9.5)
            pos = broker.get_positions()[0]
        assert pos.broker_sellable_qty == 0
        assert pos.sellable_quantity() == 0
        pf = Portfolio(total_capital=1e5, available_cash=1e5, total_market_value=900,
                       total_pnl=0, positions={pos.stock_code: pos})
        passed, reason = RiskGuard().check_sell(pf, "600519", 100, 9.0)
        assert not passed and "已有卖单" in reason

        fake_broker = MagicMock()
        fake_broker.get_portfolio.return_value = pf
        pm = PortfolioManager(broker=fake_broker)
        assert pm.check_and_stop_loss() == []
        fake_broker.place_order.assert_not_called()

    def test_partial_can_sell_qty_and_missing_field(self, tmp_path):
        rows = [dict(self._row("600519", 10.0), can_sell_qty=40),
                dict(self._row("000001", 10.0), can_sell_qty=float("nan")),
                self._row("600036", 10.0)]
        broker, fb, fake_ft = self._make_broker(tmp_path, rows)
        with patch.object(fb, "ft", fake_ft, create=True):
            by_code = {p.stock_code: p for p in broker.get_positions()}
        assert by_code["600519"].sellable_quantity() == 40
        # 字段缺失 / 无效时退回按持仓数量
        assert by_code["000001"].broker_sellable_qty is None
        assert by_code["000001"].sellable_quantity() == 100
        assert by_code["600036"].sellable_quantity() == 100


class TestFutuCodeConversion:
    """富途代码互转：港股 / 带点美股不能下到错误的市场或错误的股票上"""

    @staticmethod
    def _make_broker(tmp_path, rows, state=None):
        import pandas as pd
        import quant.broker.futu_broker as fb
        path = tmp_path / "risk.json"
        if state is not None:
            path.write_text(json.dumps(state), encoding="utf-8")
        fake_ft = MagicMock()
        fake_ft.RET_OK = 0
        with patch.object(fb, "FUTU_AVAILABLE", True), \
                patch.object(fb, "ft", fake_ft, create=True):
            broker = fb.FutuBroker(risk_state_path=str(path))
        broker._get_accounts = lambda: ["1"]
        broker._trade_ctx.position_list_query.side_effect = (
            lambda **kw: (0, pd.DataFrame(rows))
        )
        broker._trade_ctx.accinfo_query.side_effect = lambda **kw: (0, pd.DataFrame([{
            "total_assets": 100000.0, "cash": 50000.0, "market_val": 50000.0,
            "unrealized_pl": 0.0,
        }]))
        broker._trade_ctx.place_order.side_effect = (
            lambda **kw: (0, pd.DataFrame([{"order_id": "1"}]))
        )
        return broker, fb, fake_ft

    @staticmethod
    def _row(code, price=10.0):
        return {"code": code, "qty": 100, "cost_price": 10.0, "nominal_price": price,
                "pl_val": 0.0, "pl_ratio": 0.0, "stock_name": code}

    @pytest.mark.parametrize("code,expected", [
        ("00700", "HK.00700"),
        ("HK00700", "HK.00700"),
        ("hk00700", "HK.00700"),
        ("HK.00700", "HK.00700"),
        ("600519", "SH.600519"),
        ("510300", "SH.510300"),
        ("000858", "SZ.000858"),
        ("AAPL", "US.AAPL"),
        ("aapl", "US.AAPL"),
        ("BRK.B", "US.BRK.B"),
        ("HKD", "US.HKD"),
    ])
    def test_convert_code(self, tmp_path, code, expected):
        broker, _, _ = self._make_broker(tmp_path, [])
        assert broker._convert_code(code) == expected

    @pytest.mark.parametrize("futu_code,expected", [
        ("HK.00700", "HK00700"),
        ("SH.600519", "600519"),
        ("SZ.000858", "000858"),
        ("US.AAPL", "AAPL"),
        ("US.BRK.B", "BRK.B"),
    ])
    def test_position_code_round_trip(self, tmp_path, futu_code, expected):
        broker, fb, fake_ft = self._make_broker(tmp_path, [self._row(futu_code)])
        with patch.object(fb, "ft", fake_ft, create=True):
            pos = broker.get_positions()[0]
        assert pos.stock_code == expected
        assert broker._convert_code(pos.stock_code) == futu_code

    @pytest.mark.parametrize("signal_code", ["HK00700", "hk00700", "00700"])
    def test_hk_stop_loss_merged_whatever_the_signal_code(self, tmp_path, signal_code):
        broker, fb, fake_ft = self._make_broker(tmp_path, [self._row("HK.00700")])
        with patch.object(fb, "ft", fake_ft, create=True):
            broker.update_stop_loss(signal_code, 9.5)
            assert broker.get_positions()[0].stop_loss_price == 9.5

    def test_legacy_bare_hk_state_key_migrated(self, tmp_path):
        broker, fb, fake_ft = self._make_broker(
            tmp_path,
            [self._row("HK.00700")],
            state={"00700": {"stop_loss_price": 9.5, "updated": "2000-01-01"}},
        )
        with patch.object(fb, "ft", fake_ft, create=True):
            pos = broker.get_positions()[0]
        assert pos.stop_loss_price == 9.5
        assert "00700" not in broker._risk_state

    @pytest.mark.parametrize("futu_code", ["HK.00700", "US.BRK.B"])
    def test_stop_loss_sells_on_the_right_market(self, tmp_path, futu_code):
        from quant.agents.portfolio_manager import PortfolioManager
        broker, fb, fake_ft = self._make_broker(tmp_path, [self._row(futu_code, price=9.0)])
        with patch.object(fb, "ft", fake_ft, create=True):
            code = broker.get_positions()[0].stock_code
            broker.update_stop_loss(code, 9.5)
            records = PortfolioManager(broker).check_and_stop_loss()
        assert len(records) == 1
        order = broker._trade_ctx.place_order.call_args.kwargs
        assert order["code"] == futu_code
        assert order["qty"] == 100


class TestFutuPendingBuys:
    """富途买单挂出后持仓和现金都不变，风控要看到未成交买单"""

    @staticmethod
    def _order(code="SH.600519", side="BUY", status="SUBMITTED", qty=100, dealt=0, price=100.0):
        return {"code": code, "trd_side": side, "order_status": status,
                "qty": qty, "dealt_qty": dealt, "price": price}

    @staticmethod
    def _make_broker(tmp_path, rows=(), orders=(), max_positions=10):
        """cash=50000、无 market_val 的账户；orders 是可变列表，下单后自动追加"""
        import pandas as pd
        broker, fb, fake_ft = TestFutuCodeConversion._make_broker(tmp_path, list(rows))
        broker.max_positions = max_positions
        orders = list(orders)
        broker._trade_ctx.order_list_query.side_effect = (
            lambda **kw: (0, pd.DataFrame(orders))
        )

        def place_order(**kw):
            orders.append(TestFutuPendingBuys._order(
                code=kw["code"], qty=kw["qty"], price=kw["price"]))
            return 0, pd.DataFrame([{"order_id": str(len(orders))}])

        broker._trade_ctx.place_order.side_effect = place_order
        return broker, fb, fake_ft

    @staticmethod
    def _signal(code, price=100.0, stop=95.0):
        return OrderSignal(
            stock_code=code, stock_name=code, signal_type=SignalType.BUY,
            confidence=ConfidenceLevel.HIGH, sentiment_score=80,
            ideal_buy_price=price, stop_loss_price=stop, take_profit_price=0.0,
            buy_reason="测试",
        )

    def test_open_buy_reserves_cash(self, tmp_path):
        broker, fb, fake_ft = self._make_broker(tmp_path, orders=[self._order()])
        with patch.object(fb, "ft", fake_ft, create=True):
            portfolio = broker.get_portfolio()
        assert portfolio.pending_buys == {"600519": 10000.0}
        assert portfolio.available_cash == 40000.0
        # 现金转成挂单占用，总资产不变
        assert portfolio.total_assets == 50000.0 + 50000.0

    def test_partial_fill_reserves_remaining_only(self, tmp_path):
        broker, fb, fake_ft = self._make_broker(
            tmp_path, orders=[self._order(status="FILLED_PART", qty=300, dealt=100)])
        with patch.object(fb, "ft", fake_ft, create=True):
            assert broker.get_portfolio().pending_buys == {"600519": 20000.0}

    @pytest.mark.parametrize("order", [
        {"status": "FILLED_ALL"},
        {"status": "CANCELLED_ALL"},
        {"status": "FAILED"},
        {"side": "SELL"},
        {"status": "FILLED_PART", "qty": 100, "dealt": 100},
    ])
    def test_closed_or_sell_orders_reserve_nothing(self, tmp_path, order):
        broker, fb, fake_ft = self._make_broker(tmp_path, orders=[self._order(**order)])
        with patch.object(fb, "ft", fake_ft, create=True):
            portfolio = broker.get_portfolio()
        assert portfolio.pending_buys == {}
        assert portfolio.available_cash == 50000.0

    def test_hk_order_code_matches_signal_code(self, tmp_path):
        broker, fb, fake_ft = self._make_broker(
            tmp_path, orders=[self._order(code="HK.00700")])
        with patch.object(fb, "ft", fake_ft, create=True):
            assert list(broker.get_portfolio().pending_buys) == ["HK00700"]

    def test_second_run_does_not_double_buy(self, tmp_path):
        from quant.agents.portfolio_manager import PortfolioManager
        broker, fb, fake_ft = self._make_broker(tmp_path)
        manager = PortfolioManager(broker)
        with patch.object(fb, "ft", fake_ft, create=True):
            first = manager.process_signal(self._signal("600519"))
            second = manager.process_signal(self._signal("600519"))
        assert first.status == TradeStatus.PENDING
        assert second is None
        assert broker._trade_ctx.place_order.call_count == 1

    def test_buys_in_one_run_cannot_exceed_cash(self, tmp_path):
        from quant.agents.portfolio_manager import PortfolioManager
        broker, fb, fake_ft = self._make_broker(tmp_path)
        manager = PortfolioManager(broker, sizing_method="fixed_fraction")
        with patch.object(fb, "ft", fake_ft, create=True):
            for code in ("600001", "600002", "600003", "600004", "600005", "600006"):
                manager.process_signal(self._signal(code, price=100.0, stop=99.0))
        placed = [c.kwargs for c in broker._trade_ctx.place_order.call_args_list]
        assert sum(o["qty"] * o["price"] for o in placed) <= 50000.0

    def test_pending_buy_takes_a_position_slot(self, tmp_path):
        from quant.agents.portfolio_manager import PortfolioManager
        broker, fb, fake_ft = self._make_broker(
            tmp_path, orders=[self._order(price=10.0)], max_positions=1)
        with patch.object(fb, "ft", fake_ft, create=True):
            record = PortfolioManager(broker).process_signal(self._signal("000858"))
        assert record is None
        broker._trade_ctx.place_order.assert_not_called()

    def test_pending_buy_is_never_stopped_out(self, tmp_path):
        """未成交股份不进持仓，止损不能卖还没买到的股票"""
        from quant.agents.portfolio_manager import PortfolioManager
        broker, fb, fake_ft = self._make_broker(tmp_path, orders=[self._order()])
        with patch.object(fb, "ft", fake_ft, create=True):
            broker.update_stop_loss("600519", 200.0)
            assert PortfolioManager(broker).check_and_stop_loss() == []
        broker._trade_ctx.place_order.assert_not_called()

    def test_order_query_failure_blocks_buys_not_stops(self, tmp_path):
        from quant.agents.portfolio_manager import PortfolioManager
        rows = [TestFutuCodeConversion._row("SH.600519", price=9.0)]
        broker, fb, fake_ft = self._make_broker(tmp_path, rows=rows)
        broker._trade_ctx.order_list_query.side_effect = lambda **kw: (-1, "断线")
        manager = PortfolioManager(broker)
        with patch.object(fb, "ft", fake_ft, create=True):
            broker.update_stop_loss("600519", 9.5)
            assert manager.process_signal(self._signal("000858")) is None
            broker._trade_ctx.place_order.assert_not_called()
            records = manager.check_and_stop_loss()
        assert [r.stock_code for r in records] == ["600519"]


class TestOrderExecutorPendingStop:
    """富途限价单返回 PENDING 时也要写入止损；写入失败要报错而不是静默吞掉"""

    def _signal(self):
        return OrderSignal(
            stock_code="600519", stock_name="茅台", signal_type=SignalType.BUY,
            confidence=ConfidenceLevel.HIGH, ideal_buy_price=10.0, stop_loss_price=9.5, take_profit_price=12.0,
            sentiment_score=70, buy_reason="test",
        )

    def test_pending_order_writes_stop_loss(self):
        from quant.agents.order_executor import OrderExecutor
        from quant.models import TradeAction, TradeRecord, TradeStatus
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.place_order.return_value = TradeRecord.create(
            stock_code="600519", action=TradeAction.BUY, quantity=100, price=10.0,
            commission=0.0, reason="", status=TradeStatus.PENDING,
        )
        OrderExecutor(broker).execute_buy(self._signal(), 100)
        broker.update_stop_loss.assert_called_once_with("600519", 9.5)
        broker.update_take_profit.assert_called_once_with("600519", 12.0)

    def test_rejected_order_does_not_write_stop_loss(self):
        from quant.agents.order_executor import OrderExecutor
        from quant.models import TradeAction, TradeRecord, TradeStatus
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.place_order.return_value = TradeRecord.create(
            stock_code="600519", action=TradeAction.BUY, quantity=100, price=10.0,
            commission=0.0, reason="", status=TradeStatus.REJECTED,
        )
        OrderExecutor(broker).execute_buy(self._signal(), 100)
        broker.update_stop_loss.assert_not_called()

    def test_stop_loss_write_failure_logged(self, caplog):
        from quant.agents.order_executor import OrderExecutor
        from quant.models import TradeAction, TradeRecord, TradeStatus
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.update_stop_loss.side_effect = OSError("disk full")
        broker.place_order.return_value = TradeRecord.create(
            stock_code="600519", action=TradeAction.BUY, quantity=100, price=10.0,
            commission=0.0, reason="", status=TradeStatus.FILLED,
        )
        with caplog.at_level("ERROR"):
            record = OrderExecutor(broker).execute_buy(self._signal(), 100)
        assert record.status == TradeStatus.FILLED
        assert "写入止损/止盈失败" in caplog.text


# ===================================================================
# 信号提取测试
# ===================================================================

class TestSignalExtraction:
    """SignalAggregator._extract_signal 从 dashboard 解析价格"""

    def _dashboard(self, sniper_points=None, current_price=None):
        dash = {}
        if sniper_points is not None:
            dash["battle_plan"] = {"sniper_points": sniper_points}
        if current_price is not None:
            dash["data_perspective"] = {"price_position": {"current_price": current_price}}
        return {
            "sentiment_score": 75,
            "decision_type": "buy",
            "stock_name": "测试股",
            "dashboard": dash,
        }

    def _extract(self, dashboard):
        from quant.agents.signal_aggregator import SignalAggregator
        return SignalAggregator(config=MagicMock())._extract_signal("000001", dashboard)

    def test_parsed_stop_loss_kept(self):
        signal = self._extract(self._dashboard(
            {"ideal_buy": "10.00", "stop_loss": "9.20", "take_profit": "12.00"}
        ))
        assert signal.ideal_buy_price == 10.0
        assert signal.stop_loss_price == 9.2
        assert signal.take_profit_price == 12.0

    def test_missing_stop_loss_gets_default(self):
        """有买入价但没给止损价时补 95% 默认止损，否则持仓无止损、风控跳过单笔风险检查"""
        signal = self._extract(self._dashboard({"ideal_buy": "10.00", "stop_loss": "暂无"}))
        assert signal.ideal_buy_price == 10.0
        assert signal.stop_loss_price == pytest.approx(9.5)

    @pytest.mark.parametrize("bad_stop", ["nan", float("nan"), "inf", "-9.2"])
    def test_non_finite_stop_loss_gets_default(self, bad_stop):
        """止损价为 NaN / inf / 负数时按缺失处理补默认止损，不能让持仓无止损"""
        signal = self._extract(self._dashboard({"ideal_buy": "10.00", "stop_loss": bad_stop}))
        assert signal.stop_loss_price == pytest.approx(9.5)

    def test_nan_buy_price_falls_back_to_current_price(self):
        signal = self._extract(self._dashboard({"ideal_buy": "NaN"}, current_price="20.00"))
        assert signal.ideal_buy_price == 20.0
        assert signal.stop_loss_price == pytest.approx(19.0)

    def test_default_stop_loss_enforced_by_risk_guard(self):
        """补上默认止损后单笔风险检查生效：超出风险预算的数量被拒绝"""
        signal = self._extract(self._dashboard({"ideal_buy": "10.00"}))
        portfolio = Portfolio(
            total_capital=100000.0, available_cash=100000.0,
            total_market_value=0.0, total_pnl=0.0, risk_per_trade_pct=0.02,
        )
        guard = RiskGuard(max_single_stock_ratio=1.0)
        # 每股风险 0.5，2000 元预算最多 4000 股
        assert guard.check_buy(portfolio, signal, 4000, 10.0)[0]
        passed, reason = guard.check_buy(portfolio, signal, 5000, 10.0)
        assert not passed
        assert "单笔风险过高" in reason

    def test_fallback_to_current_price(self):
        signal = self._extract(self._dashboard({}, current_price="20.00"))
        assert signal.ideal_buy_price == 20.0
        assert signal.stop_loss_price == pytest.approx(19.0)
        assert signal.take_profit_price == pytest.approx(22.0)

    def test_no_price_keeps_zero_stop(self):
        signal = self._extract(self._dashboard({}))
        assert signal.ideal_buy_price == 0.0
        assert signal.stop_loss_price == 0.0

    @pytest.mark.parametrize("decision, advice, expected", [
        ("buy", None, SignalType.BUY),
        (" Sell ", None, SignalType.SELL),
        ("卖出", None, SignalType.SELL),
        ("加仓", None, SignalType.BUY),
        (None, "减仓", SignalType.SELL),
        ("", "买入", SignalType.BUY),
        ("buy/hold/sell", "卖出", SignalType.SELL),
        ("hold", "买入", SignalType.HOLD),
        (None, None, SignalType.HOLD),
        ("unknown", "随便", SignalType.HOLD),
    ])
    def test_signal_type_normalized(self, decision, advice, expected):
        """decision_type 带空白 / 中文 / 缺失时按 operation_advice 兜底，不再静默当 HOLD"""
        dashboard = self._dashboard({"ideal_buy": "10.00"})
        dashboard.pop("decision_type")
        if decision is not None:
            dashboard["decision_type"] = decision
        if advice is not None:
            dashboard["operation_advice"] = advice
        assert self._extract(dashboard).signal_type == expected

    def test_indicator_numbers_not_parsed_as_price(self):
        """文字描述里的 MA5 / 20日线 / -5% 等指标数字不能被当成价格"""
        signal = self._extract(self._dashboard({
            "ideal_buy": "回踩MA5附近1750元",
            "stop_loss": "跌破20日均线1650元止损（-5%）",
            "take_profit": "1,900.00",
        }))
        assert signal.ideal_buy_price == 1750.0
        assert signal.stop_loss_price == 1650.0
        assert signal.take_profit_price == 1900.0


class TestSentimentParsing:
    """情感评分 / 信心等级 / 嵌套字段异常时不丢信号、不绕过风控"""

    def _dashboard(self, **overrides):
        dash = {
            "sentiment_score": 75,
            "decision_type": "sell",
            "stock_name": "测试股",
            "dashboard": {"battle_plan": {"sniper_points": {"ideal_buy": "10.00"}}},
        }
        dash.update(overrides)
        return dash

    def _extract(self, dashboard):
        from quant.agents.signal_aggregator import SignalAggregator
        return SignalAggregator(config=MagicMock())._extract_signal("000001", dashboard)

    @pytest.mark.parametrize("raw, expected", [
        (75, 75.0), ("75", 75.0), ("75分", 75.0), ("82/100", 82.0), ("评分：68.5", 68.5),
        (150, 100.0), (-5, 0.0),
        (None, 0.0), ("暂无", 0.0), (float("nan"), 0.0), ("nan", 0.0), (float("inf"), 0.0),
    ])
    def test_score_parsed(self, raw, expected):
        """null / 带单位字符串不再抛异常丢掉整条信号，NaN 按 0 处理"""
        signal = self._extract(self._dashboard(sentiment_score=raw))
        assert signal is not None
        assert signal.signal_type == SignalType.SELL
        assert signal.sentiment_score == pytest.approx(expected)

    def test_missing_score_keeps_sell_signal(self):
        dash = self._dashboard()
        dash.pop("sentiment_score")
        signal = self._extract(dash)
        assert signal.signal_type == SignalType.SELL
        assert signal.sentiment_score == 0.0

    def test_invalid_score_buy_rejected_by_risk_guard(self):
        signal = self._extract(self._dashboard(decision_type="buy", sentiment_score=None))
        passed, reason = RiskGuard()._check_sentiment(signal)
        assert not passed
        assert "情感评分过低" in reason

    def test_risk_guard_rejects_nan_score(self):
        """直接构造的 NaN 评分信号也不能绕过最低买入评分检查"""
        signal = OrderSignal(
            stock_code="000001", stock_name="测试", signal_type=SignalType.BUY,
            confidence=ConfidenceLevel.HIGH, sentiment_score=float("nan"),
            ideal_buy_price=10.0, stop_loss_price=9.5, take_profit_price=11.0,
            buy_reason="test",
        )
        passed, reason = RiskGuard()._check_sentiment(signal)
        assert not passed
        assert "情感评分无效" in reason

    @pytest.mark.parametrize("inner", [None, "暂无", {"battle_plan": None},
                                       {"battle_plan": {"sniper_points": "无"}}])
    def test_non_dict_nested_fields_keep_signal(self, inner):
        signal = self._extract(self._dashboard(dashboard=inner))
        assert signal is not None
        assert signal.signal_type == SignalType.SELL
        assert signal.ideal_buy_price == 0.0

    @pytest.mark.parametrize("conf, score, expected", [
        ("中", 85, ConfidenceLevel.MEDIUM),
        ("medium", 30, ConfidenceLevel.MEDIUM),
        ("高", 30, ConfidenceLevel.HIGH),
        ("", 85, ConfidenceLevel.HIGH),
    ])
    def test_confidence_explicit_medium(self, conf, score, expected):
        """明确给出“中”时不再按高评分推断成高信心"""
        signal = self._extract(self._dashboard(confidence_level=conf, sentiment_score=score))
        assert signal.confidence == expected


class TestParsePriceStr:
    """_parse_price_str 价格文本解析"""

    @pytest.mark.parametrize("raw, expected", [
        (None, 0.0),
        (12.5, 12.5),
        ("1800.00", 1800.0),
        ("回踩到1750支撑", 1750.0),
        ("1750-1800", 1750.0),
        ("理想买入点：12.30元（在MA5附近）", 12.3),
        ("ma10 支撑 8.88", 8.88),
        ("5日线上方 10.2 元", 10.2),
        ("1,234.50元", 1234.5),
        ("MA5，1750", 1750.0),
        ("跌破 3%", 0.0),
        ("暂无", 0.0),
        ("nan", 0.0),
        ("NaN", 0.0),
        ("inf", 0.0),
        ("-1750", 0.0),
        (float("nan"), 0.0),
        (float("inf"), 0.0),
        (-3.0, 0.0),
        (True, 0.0),
    ])
    def test_parse(self, raw, expected):
        from quant.agents.signal_aggregator import _parse_price_str
        assert _parse_price_str(raw) == pytest.approx(expected)
