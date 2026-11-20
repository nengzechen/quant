import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1.endpoints import quant
from quant.broker.paper_broker import PaperBroker


@pytest.mark.parametrize("quantity,expected_status,expected_stop", [
    (100_000, "REJECTED", 9.0),
    (100, "FILLED", 9.5),
])
def test_manual_buy_updates_stop_only_after_fill(
    monkeypatch, tmp_path, quantity, expected_status, expected_stop,
):
    broker = PaperBroker(account_path=str(tmp_path / "account.json"), initial_capital=10_000)
    broker.place_order("000001", "BUY", 100, 10.0)
    broker.update_stop_loss("000001", 9.0)
    monkeypatch.setattr(quant, "_get_broker", lambda: broker)
    app = FastAPI()
    app.include_router(quant.router)

    with TestClient(app) as client:
        response = client.post("/order", json={
            "stock_code": "000001",
            "action": "BUY",
            "quantity": quantity,
            "price": 10.0,
            "stop_loss_price": 9.5,
        })

    assert response.status_code == 200
    assert response.json()["status"] == expected_status
    reloaded = PaperBroker(account_path=str(tmp_path / "account.json"))
    assert reloaded.get_positions()[0].stop_loss_price == expected_stop
