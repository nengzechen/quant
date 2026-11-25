import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1.endpoints import quant
from quant.broker.paper_broker import PaperBroker


@pytest.mark.parametrize("field,value,status", [
    ("price", "Infinity", 422),
    ("price", "NaN", 422),
    ("stop_loss_price", "Infinity", 422),
    ("stop_loss_price", "NaN", 422),
    ("stop_loss_price", -1, 422),
    ("stop_loss_price", 10.0, 400),
    ("stop_loss_price", 11.0, 400),
])
def test_invalid_order_prices_rejected_before_account_access(monkeypatch, field, value, status):
    def unexpected_broker_access():
        pytest.fail("invalid order accessed the account")

    monkeypatch.setattr(quant, "_get_broker", unexpected_broker_access)
    app = FastAPI()
    app.include_router(quant.router)
    payload = {"stock_code": "000001", "action": "BUY", "quantity": 100, "price": 10.0}
    payload[field] = value
    with TestClient(app) as client:
        response = client.post("/order", json=payload)
    assert response.status_code == status


def test_sell_ignores_buy_stop_relationship(monkeypatch, tmp_path):
    broker = PaperBroker(account_path=str(tmp_path / "account.json"), initial_capital=10_000, t_plus_one=False)
    broker.place_order("000001", "BUY", 100, 10.0)
    monkeypatch.setattr(quant, "_get_broker", lambda: broker)
    app = FastAPI()
    app.include_router(quant.router)
    with TestClient(app) as client:
        response = client.post("/order", json={
            "stock_code": "000001", "action": "SELL", "quantity": 100,
            "price": 10.0, "stop_loss_price": 11.0,
        })
    assert response.status_code == 200
    assert response.json()["status"] == "FILLED"


@pytest.mark.parametrize("quantity,new_stop,expected_status,expected_stop", [
    (100_000, 9.5, "REJECTED", 9.0),
    (100, 9.5, "FILLED", 9.5),
    (100, 8.0, "FILLED", 9.0),
    (100, 0.0, "FILLED", 9.0),
])
def test_manual_buy_updates_stop_only_after_fill(
    monkeypatch, tmp_path, quantity, new_stop, expected_status, expected_stop,
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
            "stop_loss_price": new_stop,
        })

    assert response.status_code == 200
    assert response.json()["status"] == expected_status
    reloaded = PaperBroker(account_path=str(tmp_path / "account.json"))
    assert reloaded.get_positions()[0].stop_loss_price == expected_stop
