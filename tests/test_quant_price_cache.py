import sys
from types import SimpleNamespace
from unittest.mock import Mock

import requests
import pytest

from api.v1.endpoints import quant


@pytest.mark.parametrize("codes,running,expected_starts", [
    (["600519", "000001"], False, 1),
    (["600519", "000001"], True, 0),
    (["600519"], False, 0),
    ([], False, 0),
])
def test_fresh_cache_refreshes_missing_holdings(monkeypatch, codes, running, expected_starts):
    monkeypatch.setattr(quant, "_price_cache", {"600519": 100.0})
    monkeypatch.setattr(quant, "_price_cache_ts", 1000.0)
    monkeypatch.setattr(quant.time, "time", lambda: 1001.0)
    monkeypatch.setattr(quant, "_price_fetch_running", running)
    thread_factory = Mock()
    monkeypatch.setattr(quant.threading, "Thread", thread_factory)

    cached = quant._get_prices_nonblocking(codes)

    assert cached == ({"600519": 100.0} if "600519" in codes else {})
    assert thread_factory.call_count == expected_starts
    if expected_starts:
        thread_factory.assert_called_once_with(target=quant._bg_refresh, args=(codes,), daemon=True)
        thread_factory.return_value.start.assert_called_once_with()
        assert quant._price_fetch_running


def test_partial_realtime_quotes_fall_back_only_for_missing_codes(monkeypatch):
    response = Mock(text=(
        'v_sh600519="1~name~600519~123.5~0";\n'
        'v_sz000001="1~name~000001~bad~0";\n'
        'v_sz000002="1~name~000002~inf~0";'
    ))
    monkeypatch.setattr(requests, "get", Mock(return_value=response))
    daily = Mock(side_effect=lambda **kwargs: SimpleNamespace(
        empty=False,
        iloc=[{"收盘": 15.0 if kwargs["symbol"] == "000001" else float("inf")}],
    ))
    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(stock_zh_a_hist=daily))

    assert quant._do_fetch_prices(["600519", "000001", "000002"]) == {"600519": 123.5, "000001": 15.0}
    assert [call.kwargs["symbol"] for call in daily.call_args_list] == ["000001", "000002"]


def test_refresh_start_failure_returns_cached_prices_and_allows_retry(monkeypatch):
    monkeypatch.setattr(quant, "_price_cache", {"600519": 100.0})
    monkeypatch.setattr(quant, "_price_cache_ts", 0.0)
    monkeypatch.setattr(quant, "_price_fetch_running", False)
    worker = Mock()
    worker.start.side_effect = [RuntimeError("can't start new thread"), None]
    thread_factory = Mock(return_value=worker)
    monkeypatch.setattr(quant.threading, "Thread", thread_factory)

    assert quant._get_prices_nonblocking(["600519"]) == {"600519": 100.0}
    assert not quant._price_fetch_running

    assert quant._get_prices_nonblocking(["600519"]) == {"600519": 100.0}
    assert quant._price_fetch_running
    assert thread_factory.call_count == 2
