"""试点模式：登录、数据域、审计、多轮、配额。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(config, "AUTH_REQUIRED", True)
    monkeypatch.setattr(config, "SEED_DEMO_USERS", True)
    from app.api import app

    store.init_db()
    with TestClient(app) as c:
        yield c


def login(c: TestClient, username: str, password: str):
    res = c.post("/api/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return res.json()["user"]


def test_ask_requires_login(client):
    assert client.post("/api/ask", json={"question": "上月总保费"}).status_code == 401
    assert client.get("/api/semantics").status_code == 401


def test_login_rejects_bad_password(client):
    res = client.post("/api/login", json={"username": "zhang", "password": "nope"})
    assert res.status_code == 401
    assert "不正确" in res.json()["detail"]


def test_scope_limits_rows(client):
    login(client, "zhang", "demo123")
    scoped = client.post("/api/ask", json={"question": "上月各渠道保费收入是多少"}).json()
    assert scoped["status"] == "ok"
    assert scoped["scope"] == "渠道 C01"
    assert len(scoped["rows"]) == 1
    assert scoped["rows"][0][0] == "个险代理人"
    assert "WHERE channel_code IN ('C01')" in scoped["sql"]

    client.post("/api/logout")
    login(client, "admin", "admin123")
    full = client.post("/api/ask", json={"question": "上月各渠道保费收入是多少"}).json()
    assert len(full["rows"]) == 4
    assert "WHERE channel_code" not in full["sql"]


def test_audit_records_user_and_sql(client):
    login(client, "zhang", "demo123")
    client.post("/api/ask", json={"question": "上月各渠道保费收入是多少"})
    rows = client.get("/api/audit").json()["rows"]
    assert rows and rows[0]["username"] == "zhang"
    assert rows[0]["status"] == "ok"
    assert "dwd_premium_month" in rows[0]["sql"]


def test_analyst_cannot_read_all_audit(client):
    login(client, "zhang", "demo123")
    rows = client.get("/api/audit").json()["rows"]
    assert all(r["username"] == "zhang" for r in rows)

    client.post("/api/logout")
    login(client, "li", "demo123")
    client.post("/api/ask", json={"question": "上月各渠道保费收入是多少"})
    rows = client.get("/api/audit").json()["rows"]
    assert all(r["username"] == "li" for r in rows), "渠道账号不应看到别人的审计"


def test_followup_inherits_context(client):
    login(client, "admin", "admin123")
    first = client.post("/api/ask", json={"question": "上月各渠道保费收入是多少"}).json()
    cid = first["conversation_id"]
    follow = client.post("/api/ask", json={"question": "那今年呢", "conversation_id": cid}).json()
    assert follow["status"] == "ok"
    assert follow["conversation_id"] == cid
    assert follow["rows"] and len(follow["rows"]) == 4
    gen = [s for s in follow["trace"] if s["key"] == "generate"][0]
    assert "追问改写" in gen["detail"]


def test_quota_blocks_after_limit(client):
    login(client, "zhang", "demo123")
    with store.connect() as con:
        con.execute("UPDATE users SET daily_quota = 1 WHERE username = 'zhang'")
    assert client.post("/api/ask", json={"question": "上月总保费"}).status_code == 200
    res = client.post("/api/ask", json={"question": "上月总保费"})
    assert res.status_code == 429
    assert "额度" in res.json()["detail"]


def test_password_hash_roundtrip():
    stored = store.hash_password("s3cret")
    assert stored.startswith("scrypt$")
    assert store.verify_password("s3cret", stored)
    assert not store.verify_password("s3cret2", stored)
