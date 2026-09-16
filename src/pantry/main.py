import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id   serial PRIMARY KEY,
    name text   NOT NULL
)
"""

app = FastAPI(title="pantry")


def conninfo() -> str:
    return psycopg.conninfo.make_conninfo(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
    )


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    # The table is ensured on every connection, not at startup, so the app
    # starts (and stays live) even when Postgres is down, and recovers if
    # the table later disappears (e.g. a PVC gets recreated). CREATE TABLE
    # IF NOT EXISTS is cheap enough to run unconditionally.
    with psycopg.connect(conninfo(), connect_timeout=3) as conn:
        conn.execute(SCHEMA)
        conn.commit()
        yield conn


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class Item(ItemIn):
    id: int


@app.get("/livez")
def livez() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok"}


@app.get("/items", response_model=list[Item])
def list_items() -> list[Item]:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name FROM items ORDER BY id").fetchall()
    return [Item(id=r[0], name=r[1]) for r in rows]


@app.post("/items", response_model=Item, status_code=201)
def create_item(item: ItemIn) -> Item:
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO items (name) VALUES (%s) RETURNING id, name",
            (item.name,),
        ).fetchone()
    return Item(id=row[0], name=row[1])
