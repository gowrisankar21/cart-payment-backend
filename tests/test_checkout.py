import psycopg.errors
import pytest
from sqlalchemy.exc import OperationalError

from app.core import hooks
from app.services import money
from tests.conftest import (
    PausePoint,
    checkout,
    err,
    exec_sql,
    inventory,
    join_threads,
    make_cart,
    run_concurrently,
    run_with_pause,
    scalar,
    set_inventory,
    set_price,
    start_threads,
    wait_for_lock_waiters,
)


# ---------------------------------------------------------------- money unit tests
@pytest.mark.parametrize(
    "gross,pct,expected",
    [
        (1000, 10, 100),
        (1005, 10, 101),  # 100.5 -> half-up
        (1004, 10, 100),  # 100.4
        (1, 10, 0),  # 0.1 -> 0
        (5, 10, 1),  # 0.5 -> 1 (half-up)
        (999, 100, 999),  # 100% never exceeds gross
        (0, 50, 0),
    ],
)
def test_discount_rounding_half_up(gross, pct, expected):
    d = money.compute_discount(gross, pct)
    assert d == expected
    assert 0 <= d <= gross


def test_totals_never_negative():
    for gross in (1, 2, 3, 99, 100, 12345):
        for pct in (1, 10, 33, 50, 99, 100):
            discount, net = money.compute_totals(gross, pct)
            assert net >= 0 and discount + net == gross


# ------------------------------------------------------------------- happy path
def test_successful_checkout_totals_snapshot_and_state(client, db):
    cart_id = make_cart(client, {1: 2, 2: 1, 3: 3})  # 2000 + 2550 + 999
    r = checkout(client, cart_id, "key-1")
    assert r.status_code == 201, r.text
    o = r.json()
    assert o["gross_cents"] == 5549 and o["discount_cents"] == 0 and o["net_cents"] == 5549
    assert o["coupon_code"] is None and o["discount_percent"] is None
    assert [(i["product_id"], i["quantity"], i["line_total_cents"]) for i in o["items"]] == [
        (1, 2, 2000),
        (2, 1, 2550),
        (3, 3, 999),
    ]
    assert "Idempotent-Replayed" not in r.headers
    assert (inventory(db, 1), inventory(db, 2), inventory(db, 3)) == (98, 99, 97)
    assert scalar(db, "SELECT status FROM carts WHERE id = :c", c=cart_id) == "CHECKED_OUT"
    assert scalar(db, "SELECT count(*) FROM orders") == 1

    got = client.get(f"/orders/{o['id']}")
    assert got.status_code == 200 and got.json() == o
    assert client.get("/orders/00000000-0000-0000-0000-000000000000").status_code == 404


def test_order_snapshot_survives_product_changes(client, db):
    cart_id = make_cart(client, {1: 2})
    o = checkout(client, cart_id, "snap").json()
    exec_sql(db, "UPDATE products SET price_cents = 9999, name = 'Renamed' WHERE id = 1")
    again = client.get(f"/orders/{o['id']}").json()
    assert again["items"][0]["unit_price_cents"] == 1000
    assert again["items"][0]["product_name"] == "Widget"
    assert again["gross_cents"] == 2000


def test_checkout_uses_current_price_not_price_at_add_time(client, db):
    cart_id = make_cart(client, {1: 2})
    set_price(db, 1, 1500)
    o = checkout(client, cart_id, "price-change").json()
    assert o["gross_cents"] == 3000 and o["items"][0]["unit_price_cents"] == 1500


# ------------------------------------------------------------------ validation
def test_idempotency_key_required_and_validated(client, db):
    cart_id = make_cart(client, {1: 1})
    r = client.post(f"/carts/{cart_id}/checkout", json={})
    assert r.status_code == 400 and err(r)["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    r = client.post(f"/carts/{cart_id}/checkout", json={}, headers={"Idempotency-Key": "has space"})
    assert r.status_code == 400 and err(r)["code"] == "IDEMPOTENCY_KEY_INVALID"
    assert scalar(db, "SELECT count(*) FROM orders") == 0
    assert scalar(db, "SELECT count(*) FROM idempotency_keys") == 0


def test_empty_cart_unknown_cart_and_closed_cart(client, db):
    empty = make_cart(client)
    r = checkout(client, empty, "k-empty")
    assert r.status_code == 422 and err(r)["code"] == "CART_EMPTY"
    # the failed attempt must not permanently claim the key
    assert scalar(db, "SELECT count(*) FROM idempotency_keys") == 0

    r = checkout(client, "00000000-0000-0000-0000-000000000000", "k-missing")
    assert r.status_code == 404 and err(r)["code"] == "CART_NOT_FOUND"

    cart_id = make_cart(client, {1: 1})
    assert checkout(client, cart_id, "k-a").status_code == 201
    r = checkout(client, cart_id, "k-b")  # different key, same (closed) cart
    assert r.status_code == 409 and err(r)["code"] == "CART_NOT_OPEN"
    assert scalar(db, "SELECT count(*) FROM orders") == 1
    # a closed cart rejects mutations too
    r = client.post(f"/carts/{cart_id}/items", json={"product_id": 2, "quantity": 1})
    assert r.status_code == 409 and err(r)["code"] == "CART_NOT_OPEN"


def test_insufficient_inventory_rolls_back_everything(client, db):
    """T3: item 1 is fine, item 6 is short because stock dropped after it was added."""
    cart_id = make_cart(client, {1: 2, 6: 1})
    set_inventory(db, 6, 0)
    r = checkout(client, cart_id, "short")
    assert r.status_code == 409 and err(r)["code"] == "INSUFFICIENT_STOCK"
    assert err(r)["details"]["items"] == [{"product_id": 6, "requested": 1, "available": 0}]
    assert inventory(db, 1) == 100  # earlier decrement was rolled back
    assert scalar(db, "SELECT count(*) FROM orders") == 0
    assert scalar(db, "SELECT status FROM carts WHERE id = :c", c=cart_id) == "OPEN"
    assert scalar(db, "SELECT count(*) FROM idempotency_keys") == 0


# ------------------------------------------------------------------ idempotency
def test_same_key_same_payload_returns_original_without_side_effects(client, db):
    cart_id = make_cart(client, {1: 2})
    first = checkout(client, cart_id, "same")
    second = checkout(client, cart_id, "same")
    third = checkout(client, cart_id, "same")
    assert first.status_code == 201
    assert second.status_code == 201 and third.status_code == 201
    assert second.json() == first.json() == third.json()
    assert second.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert scalar(db, "SELECT count(*) FROM orders") == 1
    assert inventory(db, 1) == 98  # consumed once


def test_same_key_different_payload_is_rejected(client, db):
    cart_a = make_cart(client, {1: 1})
    cart_b = make_cart(client, {2: 1})
    assert checkout(client, cart_a, "shared").status_code == 201
    r = checkout(client, cart_b, "shared")
    assert r.status_code == 409 and err(r)["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert scalar(db, "SELECT count(*) FROM orders") == 1
    assert scalar(db, "SELECT status FROM carts WHERE id = :c", c=cart_b) == "OPEN"
    assert inventory(db, 2) == 100
    # same cart but different coupon field is also a different payload
    r = checkout(client, cart_a, "shared", coupon="SOMECODE")
    assert r.status_code == 409 and err(r)["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_failed_checkout_leaves_key_reusable(client, db):
    """A rolled-back checkout must not leave a permanently claimed incomplete key."""
    cart_id = make_cart(client, {6: 1})
    set_inventory(db, 6, 0)
    assert checkout(client, cart_id, "retry-me").status_code == 409
    set_inventory(db, 6, 1)
    r = checkout(client, cart_id, "retry-me")  # same key, now fulfillable
    assert r.status_code == 201
    assert scalar(db, "SELECT count(*) FROM orders") == 1


# ------------------------------------------------------- rollback / failure injection
def test_failure_after_inventory_decrement_rolls_back_everything(client, db):
    """T10: crash after inventory is decremented but before commit."""
    cart_id = make_cart(client, {1: 2, 6: 1})

    def boom(**ctx):
        raise RuntimeError("injected failure after inventory update")

    hooks.hooks["checkout.after_inventory"] = boom
    r = checkout(client, cart_id, "crash")
    assert r.status_code == 500 and err(r)["code"] == "INTERNAL_ERROR"
    assert "injected" not in r.text and "Traceback" not in r.text  # nothing leaked

    assert inventory(db, 1) == 100 and inventory(db, 6) == 1
    assert scalar(db, "SELECT count(*) FROM orders") == 0
    assert scalar(db, "SELECT count(*) FROM idempotency_keys") == 0
    assert scalar(db, "SELECT status FROM carts WHERE id = :c", c=cart_id) == "OPEN"

    hooks.hooks.clear()
    assert checkout(client, cart_id, "crash").status_code == 201  # same key succeeds now
    assert inventory(db, 1) == 98 and inventory(db, 6) == 0


def test_failure_just_before_commit_rolls_back_everything(client, db):
    cart_id = make_cart(client, {1: 1})

    def boom(**ctx):
        raise RuntimeError("late failure")

    hooks.hooks["checkout.before_commit"] = boom
    assert checkout(client, cart_id, "late").status_code == 500
    assert scalar(db, "SELECT count(*) FROM orders") == 0
    assert scalar(db, "SELECT count(*) FROM order_items") == 0
    assert scalar(db, "SELECT count(*) FROM idempotency_keys") == 0
    assert inventory(db, 1) == 100
    assert scalar(db, "SELECT status FROM carts WHERE id = :c", c=cart_id) == "OPEN"


# ---------------------------------------------------------------- concurrency
def test_two_buyers_compete_for_the_final_unit(app, client, db):
    """T4: both requests overlap in PostgreSQL; exactly one wins; stock ends at zero."""
    cart_a = make_cart(client, {6: 1})
    cart_b = make_cart(client, {6: 1})
    results = run_with_pause(
        app,
        db,
        "checkout.after_inventory",
        [lambda c: checkout(c, cart_a, "buyer-a"), lambda c: checkout(c, cart_b, "buyer-b")],
    )
    assert sorted(r.status_code for r in results) == [201, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert err(loser)["code"] == "INSUFFICIENT_STOCK"
    assert inventory(db, 6) == 0
    assert scalar(db, "SELECT count(*) FROM orders") == 1
    assert scalar(db, "SELECT sum(quantity) FROM order_items WHERE product_id = 6") == 1
    assert scalar(db, "SELECT count(*) FROM carts WHERE status = 'CHECKED_OUT'") == 1


def test_many_buyers_never_oversell(app, client, db):
    carts = [make_cart(client, {6: 1}) for _ in range(8)]
    results = run_concurrently(
        app, [lambda c, cid=cid, i=i: checkout(c, cid, f"bulk-{i}") for i, cid in enumerate(carts)]
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [201] + [409] * 7
    assert inventory(db, 6) == 0
    assert scalar(db, "SELECT count(*) FROM orders") == 1


def test_two_simultaneous_checkouts_of_the_same_cart(app, client, db):
    """T5: different idempotency keys, same cart. One order, inventory consumed once."""
    cart_id = make_cart(client, {1: 3})
    results = run_with_pause(
        app,
        db,
        "checkout.after_inventory",
        [lambda c: checkout(c, cart_id, "dup-a"), lambda c: checkout(c, cart_id, "dup-b")],
    )
    assert sorted(r.status_code for r in results) == [201, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert err(loser)["code"] == "CART_NOT_OPEN"
    assert scalar(db, "SELECT count(*) FROM orders WHERE cart_id = :c", c=cart_id) == 1
    assert inventory(db, 1) == 97
    # the loser's idempotency claim rolled back with it
    assert scalar(db, "SELECT count(*) FROM idempotency_keys") == 1


def test_cart_mutation_racing_checkout_sees_closed_cart(app, client, db):
    """T5b: a cart mutation blocked behind an in-flight checkout wakes up to CART_NOT_OPEN.

    Ordering is explicit: checkout is paused *holding the cart lock* before the mutation
    starts, and the mutation is observed blocked inside PostgreSQL before checkout resumes.
    """
    cart_id = make_cart(client, {1: 1})
    pause = PausePoint("checkout.after_inventory")
    checkout_run = start_threads(app, [lambda c: checkout(c, cart_id, "race-ck")])
    assert pause.entered.wait(timeout=30)  # checkout now holds the cart row lock

    def add_item(c):
        return c.post(f"/carts/{cart_id}/items", json={"product_id": 2, "quantity": 1})

    mutate_run = start_threads(app, [add_item])
    try:
        wait_for_lock_waiters(db, 1)  # mutation is blocked on the cart lock in PostgreSQL
    finally:
        pause.release()
    (ck,) = join_threads(*checkout_run)
    (mut,) = join_threads(*mutate_run)

    assert ck.status_code == 201
    assert mut.status_code == 409
    assert err(mut)["code"] == "CART_NOT_OPEN"
    assert scalar(db, "SELECT count(*) FROM cart_items WHERE cart_id = :c", c=cart_id) == 1


def test_simultaneous_duplicate_requests_with_one_key(app, client, db):
    """T8: identical key + payload racing. One commit; both callers get the same order."""
    cart_id = make_cart(client, {1: 2})
    results = run_with_pause(
        app,
        db,
        "checkout.after_claim",
        [lambda c: checkout(c, cart_id, "twin"), lambda c: checkout(c, cart_id, "twin")],
    )
    assert [r.status_code for r in results] == [201, 201]
    assert results[0].json() == results[1].json()
    assert sorted(r.headers.get("Idempotent-Replayed", "false") for r in results) == [
        "false",
        "true",
    ]
    assert scalar(db, "SELECT count(*) FROM orders") == 1
    assert scalar(db, "SELECT count(*) FROM idempotency_keys") == 1
    assert inventory(db, 1) == 98


def test_simultaneous_same_key_different_payload(app, client, db):
    cart_a = make_cart(client, {1: 1})
    cart_b = make_cart(client, {2: 1})
    results = run_with_pause(
        app,
        db,
        "checkout.after_claim",
        [lambda c: checkout(c, cart_a, "clash"), lambda c: checkout(c, cart_b, "clash")],
    )
    assert sorted(r.status_code for r in results) == [201, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert err(loser)["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert scalar(db, "SELECT count(*) FROM orders") == 1


def test_opposite_product_order_carts_do_not_deadlock(app, client, db):
    """Carts listing products in different insertion orders still lock in ascending id."""
    cart_a = make_cart(client, {1: 1, 2: 1, 3: 1})
    cart_b = make_cart(client, {3: 1, 2: 1, 1: 1})
    results = run_concurrently(
        app, [lambda c: checkout(c, cart_a, "ord-a"), lambda c: checkout(c, cart_b, "ord-b")]
    )
    assert [r.status_code for r in results] == [201, 201]
    assert (inventory(db, 1), inventory(db, 2), inventory(db, 3)) == (98, 98, 98)


# ------------------------------------------------- transient-failure retry policy
def _deadlock(**ctx):
    raise OperationalError("injected", {}, psycopg.errors.DeadlockDetected("deadlock detected"))


def test_transient_deadlock_is_retried_and_succeeds(client, db):
    """First attempt dies with SQLSTATE 40P01 *after* inventory was decremented. The whole
    transaction (incl. idempotency claim) rolls back; the second attempt succeeds once."""
    cart_id = make_cart(client, {1: 2})
    calls = {"n": 0}

    def flaky(**ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            _deadlock()

    hooks.hooks["checkout.after_inventory"] = flaky
    r = checkout(client, cart_id, "flaky")
    assert r.status_code == 201, r.text
    assert calls["n"] == 2
    assert scalar(db, "SELECT count(*) FROM orders") == 1
    assert inventory(db, 1) == 98  # decremented exactly once, not twice
    assert scalar(db, "SELECT count(*) FROM idempotency_keys") == 1


def test_persistent_deadlock_returns_503_and_leaves_no_state(client, db):
    cart_id = make_cart(client, {1: 2})
    hooks.hooks["checkout.after_inventory"] = _deadlock
    r = checkout(client, cart_id, "doomed")
    assert r.status_code == 503 and err(r)["code"] == "TRANSIENT_CONFLICT"
    assert r.headers["Retry-After"] == "1"
    assert "deadlock" not in r.text.lower()  # no driver/SQL detail leaked
    assert scalar(db, "SELECT count(*) FROM orders") == 0
    assert inventory(db, 1) == 100
    assert scalar(db, "SELECT count(*) FROM idempotency_keys") == 0
    hooks.hooks.clear()
    assert checkout(client, cart_id, "doomed").status_code == 201  # same key works afterwards
