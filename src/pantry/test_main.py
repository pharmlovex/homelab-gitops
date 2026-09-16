from fastapi.testclient import TestClient

import main


def test_livez_does_not_need_db(db_down):
    r = TestClient(main.app).get("/livez")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_healthz_ok_when_db_up(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_healthz_503_when_db_down(db_down):
    r = TestClient(main.app).get("/healthz")
    assert r.status_code == 503


def test_items_empty(client):
    r = client.get("/items")
    assert r.status_code == 200
    assert r.json() == []


def test_create_then_list(client):
    r = client.post("/items", json={"name": "rice"})
    assert r.status_code == 201
    assert r.json() == {"id": 1, "name": "rice"}

    client.post("/items", json={"name": "beans"})
    r = client.get("/items")
    assert r.json() == [{"id": 1, "name": "rice"}, {"id": 2, "name": "beans"}]


def test_create_rejects_empty_name(client):
    r = client.post("/items", json={"name": ""})
    assert r.status_code == 422


def test_items_recover_after_table_dropped(client):
    with main.get_conn() as conn:
        conn.execute("DROP TABLE items")
    r = client.get("/items")
    assert r.status_code == 200
    assert r.json() == []
