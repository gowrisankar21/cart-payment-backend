"""Test fixtures: real PostgreSQL (never SQLite), migrated with Alembic, reset per test."""

import os
import threading
import time
from collections.abc import Callable

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from alembic import command
from app.core import hooks
from app.core.config import Settings
from app.main import create_app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://checkout:checkout_local_pw@localhost:5432/checkout_test",
)

# Deterministic test catalogue: (id, name, price_cents, inventory)
TEST_PRODUCTS = [
    (1, "Widget", 1000, 100),
    (2, "Gadget", 2550, 100),
    (3, "Gizmo", 333, 100),
    (4, "Thingamajig", 9999, 100),
    (5, "Doohickey", 150, 100),
    (6, "Last-One Special", 5000, 1),  # exactly one unit
]

REWARD_N = 3
REWARD_PERCENT = 10


def _ensure_database() -> None:
    url = make_url(TEST_DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        exists = c.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}
        ).scalar()
        if not exists:
            c.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def _migrated_database():
    _ensure_database()
    os.environ["ALEMBIC_DATABASE_URL"] = TEST_DATABASE_URL
    # Start from a pristine schema so leftover rows/tables never mask a migration problem.
    reset = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with reset.connect() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    reset.dispose()
    command.upgrade(Config("alembic.ini"), "head")
    yield


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        database_url=TEST_DATABASE_URL,
        db_pool_size=30,
        db_max_overflow=20,
        db_lock_timeout_ms=10000,
        reward_every_n_orders=REWARD_N,
        reward_discount_percent=REWARD_PERCENT,
        log_level="WARNING",
    )


@pytest.fixture(scope="session")
def app(settings):
    application = create_app(settings)
    yield application
    application.state.db.dispose()


@pytest.fixture(scope="session")
def db(app):
    return app.state.db


@pytest.fixture(autouse=True)
def _clean_state(app):
    """Reset all data and re-seed the deterministic catalogue before every test."""
    hooks.hooks.clear()
    with app.state.db.engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE idempotency_keys, order_items, orders, coupons, cart_items, carts, "
                "products RESTART IDENTITY CASCADE"
            )
        )
        for pid, name, price, inv in TEST_PRODUCTS:
            conn.execute(
                text(
                    "INSERT INTO products (id, name, price_cents, inventory) "
                    "VALUES (:i, :n, :p, :v)"
                ),
                {"i": pid, "n": name, "p": price, "v": inv},
            )
    yield
    hooks.hooks.clear()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ------------------------------------------------------------------ helpers
def start_threads(app, funcs: list[Callable[[TestClient], object]], barrier=None):
    """Start each callable in its own thread with its own client. Returns (threads, results);
    results are filled in input order by the time the threads are joined."""
    results: list = [None] * len(funcs)

    def worker(i: int, fn):
        c = TestClient(app, raise_server_exceptions=False)
        if barrier:
            barrier.wait(timeout=30)
        try:
            results[i] = fn(c)
        except BaseException as exc:  # surface in the main thread
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i, f)) for i, f in enumerate(funcs)]
    for t in threads:
        t.start()
    return threads, results


def join_threads(threads, results) -> list:
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), "worker thread hung"
    for r in results:
        if isinstance(r, BaseException):
            raise r
    return results


def run_concurrently(app, funcs: list[Callable[[TestClient], object]]) -> list:
    """Run the callables released together by a barrier so the requests genuinely overlap."""
    return join_threads(*start_threads(app, funcs, threading.Barrier(len(funcs))))


def err(r) -> dict:
    return r.json()["error"]


def checkout(client, cart_id, key, coupon=None):
    body = {"coupon_code": coupon} if coupon is not None else {}
    return client.post(f"/carts/{cart_id}/checkout", json=body, headers={"Idempotency-Key": key})


def make_cart(client: TestClient, items: dict[int, int] | None = None) -> str:
    r = client.post("/carts")
    assert r.status_code == 201, r.text
    cart_id = r.json()["id"]
    for pid, qty in (items or {}).items():
        r = client.post(f"/carts/{cart_id}/items", json={"product_id": pid, "quantity": qty})
        assert r.status_code == 201, r.text
    return cart_id


def scalar(db, sql: str, **params):
    with db.engine.connect() as c:
        return c.execute(text(sql), params).scalar()


def rows(db, sql: str, **params):
    with db.engine.connect() as c:
        return c.execute(text(sql), params).all()


def exec_sql(db, sql: str, **params) -> None:
    """Run a write statement in its own committed transaction."""
    with db.engine.begin() as c:
        c.execute(text(sql), params)


def inventory(db, product_id: int) -> int:
    return scalar(db, "SELECT inventory FROM products WHERE id = :i", i=product_id)


def set_inventory(db, product_id: int, quantity: int) -> None:
    exec_sql(db, "UPDATE products SET inventory = :q WHERE id = :i", q=quantity, i=product_id)


def set_price(db, product_id: int, price_cents: int) -> None:
    exec_sql(db, "UPDATE products SET price_cents = :p WHERE id = :i", p=price_cents, i=product_id)


# ------------------------------------------------- deterministic coordination
class PausePoint:
    """Pauses the FIRST transaction to reach a named checkpoint until released.

    Later arrivals pass straight through. Combined with ``wait_for_lock_waiters`` this lets
    a test prove a second transaction is genuinely blocked on the first one's locks
    (observed in PostgreSQL itself) before the first one is allowed to continue.
    """

    def __init__(self, name: str):
        self.entered = threading.Event()
        self._release = threading.Event()
        self._lock = threading.Lock()
        self._taken = False
        hooks.hooks[name] = self._hook

    def _hook(self, **ctx):
        with self._lock:
            first = not self._taken
            self._taken = True
        if first:
            self.entered.set()
            assert self._release.wait(timeout=30), "pause point never released"

    def release(self):
        self._release.set()


def wait_for_lock_waiters(db, expected: int = 1, timeout: float = 15.0) -> None:
    """Block until `expected` backends are waiting on a heavyweight lock (bounded poll
    on pg_stat_activity - state observed in the database, not the clock)."""
    deadline = time.monotonic() + timeout
    sql = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
        "AND pid <> pg_backend_pid()"
    )
    while time.monotonic() < deadline:
        with db.engine.connect() as c:
            if c.execute(sql).scalar() >= expected:
                return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {expected} lock waiter(s)")


def run_with_pause(app, db, hook_name: str, funcs, expected_waiters: int = 1):
    """Run `funcs` concurrently; the first to hit `hook_name` pauses until the others are
    observed blocked on locks in PostgreSQL, then everything is released."""
    pause = PausePoint(hook_name)
    threads, results = start_threads(app, funcs)
    try:
        assert pause.entered.wait(timeout=30), "no request reached the pause point"
        wait_for_lock_waiters(db, expected_waiters)
    finally:
        pause.release()
    return join_threads(threads, results)
