import pytest

from app import domain
from app.db import init_db
from app.main import app

app.config.TOUCHUP = False


@pytest.fixture(autouse=True)
def _reset_inflight():
    domain._inflight_flows.clear()
    yield
    domain._inflight_flows.clear()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setattr(domain.Config, "LANDER_DELAY", 0)
    monkeypatch.setattr(domain.Config, "ROLLBACK_DELAY", 0)
    init_db(str(db))
    return app.test_client


@pytest.fixture()
def auth(client):
    _req, resp = client.post("/api/login", json={"username": "admin", "password": "123456"})
    assert resp.status == 200
    cookie = resp.cookies.get("flow_session")
    return {"Cookie": f"flow_session={cookie}"}


def wait_status(client, auth, batch_id, expected, timeout=5.0):
    """确定性等待后台入库线程收尾，再确认批次进入 expected 之一。"""
    domain.wait_worker(batch_id, timeout)
    _req, resp = client.get(f"/api/batches/{batch_id}", headers=auth)
    assert resp.status == 200
    assert resp.json["status"] in expected, (
        f"批次 {batch_id} 期望 {expected}，实际 {resp.json['status']}（{resp.json['note']}）"
    )
    return resp.json


def wait_rollback(client, auth, rollback_id, timeout=5.0):
    """确定性等待后台回滚线程收尾。"""
    domain.wait_worker(rollback_id, timeout)
    _req, resp = client.get(f"/api/rollbacks/{rollback_id}", headers=auth)
    assert resp.status == 200
    assert resp.json["status"] == "已完成", resp.json["status"]
    return resp.json
