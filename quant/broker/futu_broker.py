# -*- coding: utf-8 -*-
"""
===================================
量化交易系统 - 富途 OpenD 真实下单 Broker（可选）
===================================

依赖 futu-api（pip install futu-api）。
需要本地运行富途 OpenD 客户端（默认 localhost:11111）。

如果 futu-api 未安装，导入此模块会给出友好提示而不会报错。

支持：
- 港股、A股（沪深港通）、美股
- 市价单和限价单
- 模拟盘和真实盘（通过 trade_env 控制）
- 止损价 / 止盈价 / 持仓最高价保存在本地 JSON（富途持仓不带这些字段），
  查询持仓时合并回 Position，止损、止盈、移动止损才能对富途持仓生效
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from quant.broker.base import BaseBroker
from quant.config import normalize_trade_env
from quant.models import (
    Portfolio,
    Position,
    TradeAction,
    TradeRecord,
    TradeStatus,
)

logger = logging.getLogger(__name__)

_HK_CODE_RE = re.compile(r'^HK\d{5}$')                # 规范港股代码
_HK_INPUT_RE = re.compile(r'^HK\.?(\d{1,5})$|^(\d{5})$')  # 可接受的港股写法

# 尝试导入富途 API
try:
    import futu as ft
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False
    logger.warning(
        "富途 API 未安装，FutuBroker 不可用。"
        "请运行: pip install futu-api"
    )


def _check_futu_available():
    """检查富途 API 是否可用，不可用则抛出有意义的错误"""
    if not FUTU_AVAILABLE:
        raise RuntimeError(
            "futu-api 未安装。请运行: pip install futu-api\n"
            "同时需要运行富途 OpenD 客户端。"
        )


class FutuBroker(BaseBroker):
    """
    富途 OpenD 真实下单 Broker。

    通过富途 OpenD 客户端连接富途证券账户，支持港股/A股/美股下单。

    使用前提：
    1. 安装 futu-api: pip install futu-api
    2. 运行富途 OpenD 客户端（参考富途官方文档）
    3. 配置正确的 host/port 和 trade_env
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        trade_env: str = "SIMULATE",
        market: str = "HK",
        max_positions: int = 10,
        risk_per_trade_pct: float = 0.02,
        risk_state_path: Optional[str] = None,
    ):
        """
        初始化富途 Broker。

        Args:
            host: OpenD 地址
            port: OpenD 端口
            trade_env: 交易环境 "SIMULATE"（模拟）或 "REAL"（真实）
            market: 默认市场 "HK"/"US"/"CN"
            max_positions: 最大持仓数量
            risk_per_trade_pct: 单笔最大风险比例
            risk_state_path: 本地止损 / 止盈状态文件
                （默认 ~/.stock_quant/futu_risk_state_<env>.json，模拟盘与真实盘分开）

        Raises:
            ValueError: trade_env 不是 SIMULATE / REAL（大小写不敏感）
        """
        # 先校验交易环境再检查依赖：拼错的环境绝不能落到真实盘
        trade_env = normalize_trade_env(trade_env)
        _check_futu_available()

        self.host = host
        self.port = port
        self.trade_env = trade_env
        self.market = market
        self.max_positions = max_positions
        self.risk_per_trade_pct = risk_per_trade_pct

        self._quote_ctx: Optional[object] = None
        self._trade_ctx: Optional[object] = None
        self._account_id: Optional[str] = None
        self.risk_state_path = Path(os.path.expanduser(
            risk_state_path or f"~/.stock_quant/futu_risk_state_{trade_env.lower()}.json"
        ))
        self._risk_state = self._load_risk_state()

        self._connect()

    def _connect(self) -> None:
        """建立与 OpenD 的连接"""
        try:
            self._quote_ctx = ft.OpenQuoteContext(host=self.host, port=self.port)

            # 只有明确的 REAL 才连真实盘，其余一律模拟盘
            env = ft.TrdEnv.REAL if self.trade_env == "REAL" else ft.TrdEnv.SIMULATE

            # 根据市场选择交易连接
            if self.market in ("HK", "US"):
                self._trade_ctx = ft.OpenHKTradeContext(host=self.host, port=self.port)
            else:
                # A 股通过港股通交易
                self._trade_ctx = ft.OpenHKTradeContext(host=self.host, port=self.port)

            self.trd_env = env
            logger.info(
                f"富途 OpenD 已连接: {self.host}:{self.port}, "
                f"环境: {self.trade_env}"
            )
        except Exception as e:
            logger.error(f"连接富途 OpenD 失败: {e}")
            raise

    def _convert_code(self, stock_code: str) -> str:
        """
        将内部股票代码转换为富途格式。

        例如：
        - "600519" -> "SH.600519"
        - "000858" -> "SZ.000858"
        - "AAPL" -> "US.AAPL"
        - "00700" / "HK00700" -> "HK.00700"
        """
        code = _normalize_code(stock_code)
        if _HK_CODE_RE.match(code):
            return f"HK.{code[2:]}"
        if code.isdigit():
            if code.startswith('6') or code.startswith('5'):
                return f"SH.{code}"
            return f"SZ.{code}"
        return f"US.{code}"

    def _get_accounts(self) -> List[str]:
        """获取账户列表"""
        try:
            ret, data = self._trade_ctx.get_acc_list()
            if ret == ft.RET_OK:
                return [str(row['acc_id']) for _, row in data.iterrows()]
            return []
        except Exception as e:
            logger.error(f"获取账户列表失败: {e}")
            return []

    def get_account_info(self) -> dict:
        """获取富途账户基本信息"""
        try:
            accounts = self._get_accounts()
            if not accounts:
                raise RuntimeError("未找到账户")
            acc_id = int(accounts[0])

            ret, data = self._trade_ctx.accinfo_query(
                trd_env=self.trd_env,
                acc_id=acc_id,
            )
            if ret != ft.RET_OK:
                raise RuntimeError(f"账户查询失败: {data}")

            row = data.iloc[0]
            return {
                "total_assets": float(row.get('total_assets', 0)),
                "available_cash": float(row.get('cash', 0)),
                "market_value": float(row.get('market_val', 0)),
                "total_pnl": float(row.get('unrealized_pl', 0)),
                "pnl_pct": 0.0,
                "total_capital": float(row.get('total_assets', 0)),
                "position_count": 0,
                "max_positions": self.max_positions,
            }
        except Exception as e:
            logger.error(f"获取富途账户信息失败: {e}")
            return {
                "total_assets": 0, "available_cash": 0,
                "market_value": 0, "total_pnl": 0,
                "pnl_pct": 0, "total_capital": 0,
                "position_count": 0, "max_positions": self.max_positions,
            }

    def place_order(
        self,
        stock_code: str,
        action: str,
        quantity: int,
        price: float,
        order_type: str = "LIMIT",
    ) -> TradeRecord:
        """通过富途 OpenD 下单"""
        try:
            futu_code = self._convert_code(stock_code)
            trade_side = ft.TrdSide.BUY if action.upper() == "BUY" else ft.TrdSide.SELL

            if order_type == "MARKET":
                order_type_ft = ft.OrderType.MARKET
            else:
                order_type_ft = ft.OrderType.NORMAL  # 普通限价单

            accounts = self._get_accounts()
            acc_id = int(accounts[0]) if accounts else 0

            ret, data = self._trade_ctx.place_order(
                price=price,
                qty=quantity,
                code=futu_code,
                trd_side=trade_side,
                order_type=order_type_ft,
                trd_env=self.trd_env,
                acc_id=acc_id,
            )

            if ret != ft.RET_OK:
                raise RuntimeError(f"下单失败: {data}")

            order_id = str(data.iloc[0]['order_id'])
            trade_action = TradeAction.BUY if action.upper() == "BUY" else TradeAction.SELL

            record = TradeRecord.create(
                stock_code=stock_code,
                action=trade_action,
                quantity=quantity,
                price=price,
                commission=0.0,  # 富途会自动扣除手续费
                reason=f"富途{self.trade_env}下单",
                status=TradeStatus.PENDING,
            )
            record.order_id = order_id
            logger.info(
                f"富途下单成功: {stock_code} {action} x{quantity} @ {price:.2f}, "
                f"订单ID: {order_id}"
            )
            return record

        except Exception as e:
            logger.error(f"富途下单失败: {e}")
            trade_action = TradeAction.BUY if action.upper() == "BUY" else TradeAction.SELL
            return TradeRecord.create(
                stock_code=stock_code,
                action=trade_action,
                quantity=quantity,
                price=price,
                commission=0.0,
                reason=f"富途下单失败: {e}",
                status=TradeStatus.REJECTED,
            )

    def cancel_order(self, order_id: str) -> bool:
        """撤销富途订单"""
        try:
            accounts = self._get_accounts()
            acc_id = int(accounts[0]) if accounts else 0

            ret, data = self._trade_ctx.modify_order(
                modify_order_op=ft.ModifyOrderOp.CANCEL,
                order_id=order_id,
                qty=0,
                price=0,
                trd_env=self.trd_env,
                acc_id=acc_id,
            )
            if ret == ft.RET_OK:
                logger.info(f"富途撤单成功: {order_id}")
                return True
            else:
                logger.warning(f"富途撤单失败: {data}")
                return False
        except Exception as e:
            logger.error(f"富途撤单异常: {e}")
            return False

    def get_positions(self) -> List[Position]:
        """获取富途账户持仓"""
        try:
            accounts = self._get_accounts()
            acc_id = int(accounts[0]) if accounts else 0

            ret, data = self._trade_ctx.position_list_query(
                trd_env=self.trd_env,
                acc_id=acc_id,
            )
            if ret != ft.RET_OK:
                logger.error(f"查询持仓失败: {data}")
                return []

            positions = []
            for _, row in data.iterrows():
                code = _from_futu_code(row.get('code', ''))
                qty = int(row.get('qty', 0))
                cost = float(row.get('cost_price', 0))
                current = float(row.get('nominal_price', 0))
                pnl = float(row.get('pl_val', 0))
                positions.append(Position(
                    stock_code=code,
                    stock_name=str(row.get('stock_name', code)),
                    quantity=qty,
                    avg_cost=cost,
                    current_price=current,
                    market_value=qty * current,
                    pnl=pnl,
                    pnl_pct=float(row.get('pl_ratio', 0)) * 100,
                    open_time=datetime.now().isoformat(),
                ))
            self._apply_risk_state(positions)
            return positions
        except Exception as e:
            logger.error(f"获取富途持仓失败: {e}")
            return []

    # ===== 本地止损 / 止盈状态 =====

    def update_stop_loss(self, stock_code: str, stop_loss_price: float) -> None:
        """记录持仓止损价（富途持仓不带止损价，保存在本地状态文件）"""
        self._set_risk_field(stock_code, 'stop_loss_price', stop_loss_price)

    def update_take_profit(self, stock_code: str, take_profit_price: float) -> None:
        """记录持仓止盈价（保存在本地状态文件）"""
        self._set_risk_field(stock_code, 'take_profit_price', take_profit_price)

    def _set_risk_field(self, stock_code: str, field: str, value: float) -> None:
        code = _normalize_code(stock_code)
        if not code:
            return
        entry = self._risk_state.setdefault(code, {})
        entry[field] = float(value)
        # 记下写入日期：当日挂出的买单可能还未成交，持仓里暂时没有，不能被清理
        entry['updated'] = datetime.now().strftime('%Y-%m-%d')
        self._save_risk_state()

    def _apply_risk_state(self, positions: List[Position]) -> None:
        """
        把本地止损 / 止盈 / 最高价合并进富途持仓，并维护状态文件。

        - 持仓最高价按每次查询到的现价只升不降，供移动止损使用
        - 已不在持仓中、且不是当日写入的状态视为已平仓，删除，避免旧止损和旧最高价
          套到日后重新买入的同一只股票上
        """
        today = datetime.now().strftime('%Y-%m-%d')
        changed = False
        held = set()
        for pos in positions:
            code = _normalize_code(pos.stock_code)
            held.add(code)
            entry = self._risk_state.get(code)
            if entry is None:
                entry = self._risk_state[code] = {}
                changed = True
            if pos.current_price > 0 and pos.current_price > (entry.get('highest_price') or 0):
                entry['highest_price'] = pos.current_price
                changed = True
            pos.stop_loss_price = entry.get('stop_loss_price')
            pos.take_profit_price = entry.get('take_profit_price')
            pos.highest_price = entry.get('highest_price')

        for code in list(self._risk_state):
            if code not in held and self._risk_state[code].get('updated') != today:
                del self._risk_state[code]
                changed = True

        if changed:
            self._save_risk_state()

    def _load_risk_state(self) -> dict:
        if not self.risk_state_path.exists():
            return {}
        try:
            with open(self.risk_state_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            # 旧状态文件的港股键是 '00700'，合并到规范键，否则止损对不上持仓、次日被清理
            state = {}
            for code, entry in data.items():
                state.setdefault(_normalize_code(code), {}).update(entry)
            return state
        except Exception as e:
            logger.error(f"读取富途止损状态失败，持仓将没有止损保护: {e}")
            return {}

    def _save_risk_state(self) -> None:
        try:
            self.risk_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.risk_state_path.with_suffix('.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._risk_state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.risk_state_path)
        except Exception as e:
            logger.error(f"保存富途止损状态失败: {e}")

    def get_order_status(self, order_id: str) -> str:
        """查询富途订单状态"""
        try:
            accounts = self._get_accounts()
            acc_id = int(accounts[0]) if accounts else 0

            ret, data = self._trade_ctx.order_list_query(
                order_id=order_id,
                trd_env=self.trd_env,
                acc_id=acc_id,
            )
            if ret != ft.RET_OK or data.empty:
                return TradeStatus.PENDING.value

            row = data.iloc[0]
            order_status = str(row.get('order_status', ''))

            # 富途状态映射
            status_map = {
                'SUBMITTED': TradeStatus.PENDING.value,
                'FILLED_ALL': TradeStatus.FILLED.value,
                'CANCELLED_ALL': TradeStatus.CANCELLED.value,
                'FAILED': TradeStatus.REJECTED.value,
            }
            return status_map.get(order_status, TradeStatus.PENDING.value)
        except Exception as e:
            logger.error(f"查询富途订单状态失败: {e}")
            return TradeStatus.PENDING.value

    def get_trade_records(self, limit: int = 50) -> List[TradeRecord]:
        """获取富途历史成交记录"""
        try:
            accounts = self._get_accounts()
            acc_id = int(accounts[0]) if accounts else 0

            ret, data = self._trade_ctx.deal_list_query(
                trd_env=self.trd_env,
                acc_id=acc_id,
            )
            if ret != ft.RET_OK:
                logger.error(f"查询成交记录失败: {data}")
                return []

            records = []
            for _, row in list(data.iterrows())[:limit]:
                code = _from_futu_code(row.get('code', ''))
                trd_side = str(row.get('trd_side', 'BUY'))
                action = TradeAction.BUY if 'BUY' in trd_side.upper() else TradeAction.SELL
                records.append(TradeRecord.create(
                    stock_code=code,
                    action=action,
                    quantity=int(row.get('qty', 0)),
                    price=float(row.get('price', 0)),
                    commission=0.0,
                    reason="富途成交",
                    status=TradeStatus.FILLED,
                ))
            return records
        except Exception as e:
            logger.error(f"获取富途成交记录失败: {e}")
            return []

    def get_portfolio(self) -> Portfolio:
        """获取富途账户投资组合"""
        try:
            account_info = self.get_account_info()
            positions = self.get_positions()
            positions_dict = {p.stock_code: p for p in positions}

            return Portfolio(
                total_capital=account_info['total_assets'],
                available_cash=account_info['available_cash'],
                total_market_value=account_info['market_value'],
                total_pnl=account_info['total_pnl'],
                positions=positions_dict,
                max_positions=self.max_positions,
                risk_per_trade_pct=self.risk_per_trade_pct,
            )
        except Exception as e:
            logger.error(f"获取富途投资组合失败: {e}")
            return Portfolio(
                total_capital=0,
                available_cash=0,
                total_market_value=0,
                total_pnl=0,
            )

    def __del__(self):
        """析构时关闭连接"""
        try:
            if self._quote_ctx:
                self._quote_ctx.close()
            if self._trade_ctx:
                self._trade_ctx.close()
        except Exception:
            pass


def _normalize_code(stock_code) -> str:
    """
    富途持仓和状态文件的内部代码：去空白转大写，港股统一为项目规范的 HK00700。

    "00700" / "hk00700" / "HK.00700" -> "HK00700"，其余代码原样（A 股 6 位、美股 ticker）。
    """
    code = str(stock_code or '').strip().upper()
    match = _HK_INPUT_RE.match(code)
    if match:
        return f"HK{(match.group(1) or match.group(2)).zfill(5)}"
    return code


def _from_futu_code(futu_code) -> str:
    """
    富途代码转内部代码："HK.00700" -> "HK00700"，"SH.600519" -> "600519"，
    "US.BRK.B" -> "BRK.B"。

    只去掉第一段市场前缀：ticker 自身可能带点，取最后一段会把 BRK.B 变成 B，
    卖出时下到另一只股票上；港股保留 HK 前缀，才能和信号代码、止损状态对上。
    """
    market, _, code = str(futu_code or '').strip().partition('.')
    if market.upper() == 'HK':
        return _normalize_code(f"HK{code}")
    return _normalize_code(code or market)
