import os

import pytest
from fastapi.testclient import TestClient

# Local test Postgres (see plan Task 3 Step 2). Real env vars win.
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "56432")
os.environ.setdefault("POSTGRES_USER", "pantry")
os.environ.setdefault("POSTGRES_PASSWORD", "pantry")
os.environ.setdefault("POSTGRES_DB", "pantry")

if os.environ["POSTGRES_HOST"] not in ("localhost", "127.0.0.1"):
    raise RuntimeError("tests truncate tables; refusing to run against non-local POSTGRES_HOST")

import main  # noqa: E402


@pytest.fixture
def client():
    with main.get_conn() as conn:
        conn.execute("TRUNCATE items RESTART IDENTITY")
    return TestClient(main.app)


@pytest.fixture
def db_down(monkeypatch):
    # Nothing listens on port 1, so connecting fails immediately
    monkeypatch.setenv("POSTGRES_PORT", "1")
