from unittest.mock import Mock

from api.v1.endpoints import quant


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
