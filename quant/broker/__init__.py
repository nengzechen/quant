# -*- coding: utf-8 -*-
"""
量化交易系统 - 券商接口模块

提供统一的券商接口抽象，以及模拟盘和富途实盘实现。
"""

from quant.broker.base import BaseBroker
from quant.broker.paper_broker import PaperBroker

__all__ = ["BaseBroker", "PaperBroker"]
