# -*- coding: utf-8 -*-
"""
Phase1 全市场扫描的并发/超时逻辑测试（不访问网络）

覆盖：
- 同一代码被多个模型共用时只提交一次任务
- 时间预算耗尽后取消剩余任务、保留已有结果
- baostock 取数：等锁时间不计入查询超时，避免"假超时"触发重连
- worker 全部卡死时时间预算照样生效；baostock login/logout 卡死不会拖住锁
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from src.screening import indicators
from src.screening.pipeline import phase1


def test_build_code_models_dedups_shared_pool():
    m1, m2, m3 = object(), object(), object()
    code_models = phase1._build_code_models([
        (m1, ["000001", "600000"]),
        (m2, ["600000", "300750"]),
        (m3, ["600000", "300750"]),
    ])
    assert list(code_models) == ["000001", "600000", "300750"]
    assert code_models["600000"] == [m1, m2, m3]
    assert code_models["300750"] == [m2, m3]


def test_run_scoring_collects_all_results():
    code_models = {f"{i:06d}": ["m"] for i in range(20)}
    results = phase1._run_scoring(code_models, lambda code, models: [code], max_workers=4)
    assert sorted(results) == sorted(code_models)


def test_run_scoring_survives_task_exception():
    def score(code, models):
        if code == "bad":
            raise RuntimeError("boom")
        return [code]

    results = phase1._run_scoring({"ok": [], "bad": []}, score, max_workers=2)
    assert results == ["ok"]


def test_run_scoring_stops_when_budget_exceeded():
    calls = []

    def score(code, models):
        calls.append(code)
        time.sleep(0.05)
        return [code]

    code_models = {f"{i:06d}": [] for i in range(200)}
    # 全跑完需要约 5 秒，预算 0.3 秒：中途停止，保留已完成的结果
    results = phase1._run_scoring(
        code_models, score, max_workers=2, budget_sec=0.3, poll_sec=0.05,
    )
    assert 1 <= len(results) < len(code_models)
    assert len(calls) < len(code_models)


def test_run_scoring_budget_fires_when_all_workers_hang():
    """worker 全部卡死（没有任何任务完成）时，到预算也必须返回已有结果"""
    release = threading.Event()

    def score(code, models):
        if code != "000000":
            release.wait(5)   # 模拟 socket 无限阻塞
        return [code]

    code_models = {f"{i:06d}": [] for i in range(10)}
    t0 = time.monotonic()
    try:
        results = phase1._run_scoring(
            code_models, score, max_workers=3, budget_sec=0.3, poll_sec=0.05,
        )
    finally:
        release.set()
    assert time.monotonic() - t0 < 2
    assert results == ["000000"]


def test_time_budget_env(monkeypatch):
    monkeypatch.setenv("PHASE1_TIME_BUDGET_MIN", "150")
    assert phase1._time_budget_seconds() == 9000
    monkeypatch.setenv("PHASE1_TIME_BUDGET_MIN", "abc")
    assert phase1._time_budget_seconds() == 0
    monkeypatch.delenv("PHASE1_TIME_BUDGET_MIN")
    assert phase1._time_budget_seconds() == 0


class _FakeRS:
    def __init__(self, rows):
        self.error_code = "0"
        self._rows = list(rows)

    def next(self):
        return bool(self._rows)

    def get_row_data(self):
        return self._rows.pop(0)


def _fake_bs(query_delay):
    row = ["2026-10-09", "10", "11", "9", "10.5", "1000", "10500", "1.2", "0.5"]
    state = {"logins": 0}

    def query(*args, **kwargs):
        time.sleep(query_delay)
        return _FakeRS([row])

    def login():
        state["logins"] += 1
        return SimpleNamespace(error_code="0")

    fake = SimpleNamespace(
        query_history_k_data_plus=query,
        login=login,
        logout=lambda: None,
    )
    return fake, state


def test_baostock_lock_wait_not_counted_as_timeout(monkeypatch):
    """等锁时间超过查询超时、查询本身很快时，不应判超时、也不应重连"""
    fake, state = _fake_bs(query_delay=0.01)
    monkeypatch.setitem(__import__("sys").modules, "baostock", fake)
    monkeypatch.setattr(indicators, "_BS_LOGGED_IN", True)

    holder_started = threading.Event()

    def hold_lock():
        with indicators._BS_LOCK:
            holder_started.set()
            time.sleep(0.3)

    # 把超时压到 0.1 秒加速测试：等锁 0.3 秒 > 超时，但查询本身 0.01 秒
    real_timeout = indicators._run_with_timeout
    with patch.object(indicators, "_run_with_timeout",
                      lambda fn, timeout_sec=10: real_timeout(fn, timeout_sec=0.1)):
        t = threading.Thread(target=hold_lock)
        t.start()
        holder_started.wait()
        df = indicators._get_daily_df_baostock("600000", days=5)
        t.join()

    assert df is not None and len(df) == 1
    assert state["logins"] == 0


def test_baostock_real_timeout_reconnects_once(monkeypatch):
    """查询本身超时：重连一次后重试，两次都超时返回 None"""
    fake, state = _fake_bs(query_delay=0.3)
    monkeypatch.setitem(__import__("sys").modules, "baostock", fake)
    monkeypatch.setattr(indicators, "_BS_LOGGED_IN", True)

    real_timeout = indicators._run_with_timeout
    with patch.object(indicators, "_run_with_timeout",
                      lambda fn, timeout_sec=10: real_timeout(fn, timeout_sec=0.05)):
        df = indicators._get_daily_df_baostock("600000", days=5)

    assert df is None
    assert state["logins"] == 1


def test_baostock_login_hang_does_not_hold_lock(monkeypatch):
    """login 卡死时按超时放弃并释放锁，而不是让所有 worker 永远等锁"""
    fake, _ = _fake_bs(query_delay=0)
    fake.login = lambda: time.sleep(5)
    monkeypatch.setitem(__import__("sys").modules, "baostock", fake)
    monkeypatch.setattr(indicators, "_BS_LOGGED_IN", False)
    monkeypatch.setattr(indicators, "_BS_SESSION_TIMEOUT", 0.1)

    t0 = time.monotonic()
    assert indicators._bs_ensure_login() is False
    assert time.monotonic() - t0 < 2
    assert not indicators._BS_LOCK.locked()


def test_bs_logout_skips_when_lock_stuck(monkeypatch):
    """收尾登出时锁被卡死的 worker 占着，应超时跳过而不是挂住整个进程"""
    fake, _ = _fake_bs(query_delay=0)
    monkeypatch.setitem(__import__("sys").modules, "baostock", fake)
    monkeypatch.setattr(indicators, "_BS_LOGGED_IN", True)
    monkeypatch.setattr(indicators, "_BS_SESSION_TIMEOUT", 0.1)

    indicators._BS_LOCK.acquire()
    try:
        t0 = time.monotonic()
        indicators.bs_logout()
        assert time.monotonic() - t0 < 2
    finally:
        indicators._BS_LOCK.release()
    assert indicators._BS_LOGGED_IN is True
