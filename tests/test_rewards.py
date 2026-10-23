import pytest

from app.core import hooks
from app.core.errors import AppError
from app.services import rewards as rewards_mod
from tests.conftest import (
    REWARD_N,
    REWARD_PERCENT,
    checkout,
    err,
    exec_sql,
    inventory,
    make_cart,
    rows,
    run_concurrently,
    run_with_pause,
    scalar,
    set_inventory,
)


def place_order(client, key, items=None):
    cart_id = make_cart(client, items or {5: 1})
    r = client.post(f"/carts/{cart_id}/checkout", json={}, headers={"Idempotency-Key": key})
    assert r.status_code == 201, r.text
    return r.json()


def place_orders(client, count, prefix="o"):
    return [place_order(client, f"{prefix}-{i}") for i in range(count)]


def generate(client):
    return client.post("/admin/coupons")


def insert_coupon(db, code, milestone, percent):
    exec_sql(
        db,
        "INSERT INTO coupons (id, code, milestone_order_count, discount_percent) "
        "VALUES (gen_random_uuid(), :c, :m, :p)",
        c=code,
        m=milestone,
        p=percent,
    )


def new_coupon(client):
    place_orders(client, REWARD_N, prefix="seed")
    r = generate(client)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------ generation rules
def test_no_coupon_before_milestone(client, db):
    place_orders(client, REWARD_N - 1)
    r = generate(client)
    assert r.status_code == 409 and err(r)["code"] == "NO_ELIGIBLE_MILESTONE"
    assert scalar(db, "SELECT count(*) FROM coupons") == 0


def test_no_coupon_with_zero_orders(client):
    r = generate(client)
    assert r.status_code == 409 and err(r)["code"] == "NO_ELIGIBLE_MILESTONE"


def test_coupon_generated_at_nth_order_exactly_once_per_milestone(client, db):
    place_orders(client, REWARD_N)
    first = generate(client)
    assert first.status_code == 201
    c = first.json()
    assert c["milestone_order_count"] == REWARD_N
    assert c["discount_percent"] == REWARD_PERCENT
    assert c["status"] == "AVAILABLE" and c["redeemed_at"] is None and c["code"].startswith("RWD-")

    again = generate(client)  # same milestone already rewarded
    assert again.status_code == 409 and err(again)["code"] == "NO_ELIGIBLE_MILESTONE"
    assert scalar(db, "SELECT count(*) FROM coupons") == 1


def test_backlog_one_coupon_per_request_lowest_first(client, db):
    place_orders(client, REWARD_N * 2)  # two milestones reached before any generation
    a, b = generate(client), generate(client)
    assert a.status_code == b.status_code == 201
    assert (a.json()["milestone_order_count"], b.json()["milestone_order_count"]) == (
        REWARD_N,
        REWARD_N * 2,
    )
    assert generate(client).status_code == 409


def test_failed_checkouts_do_not_advance_milestone(client, db):
    """Failed / rolled-back checkouts must not count toward the milestone."""
    place_orders(client, REWARD_N - 1)
    # failure 1: insufficient stock
    cart = make_cart(client, {6: 1})
    set_inventory(db, 6, 0)
    assert checkout(client, cart, "f1").status_code == 409
    # failure 2: injected crash
    cart2 = make_cart(client, {1: 1})

    def boom(**ctx):
        raise RuntimeError("x")

    hooks.hooks["checkout.before_commit"] = boom
    assert checkout(client, cart2, "f2").status_code == 500
    hooks.hooks.clear()
    # failure 3: unknown coupon
    assert checkout(client, cart2, "f3", coupon="NOPE").status_code == 422

    assert scalar(db, "SELECT count(*) FROM orders") == REWARD_N - 1
    assert generate(client).status_code == 409  # still short of the milestone

    place_order(client, "the-nth")  # the Nth *successful* order
    assert generate(client).status_code == 201


def test_config_change_does_not_void_or_duplicate_existing_milestones(app, client, db):
    """Thresholds are persisted absolute values; a changed n only affects new milestones."""
    place_orders(client, REWARD_N * 2)
    assert generate(client).status_code == 201  # milestone N

    original = app.state.settings.reward_every_n_orders
    try:
        app.state.settings.reward_every_n_orders = 2  # n changes 3 -> 2 (orders = 6)
        # lowest multiple of 2 within 6 without a coupon: 2 (3 already used), then 4, then 6
        for expected in (2, 4, 6):
            assert generate(client).json()["milestone_order_count"] == expected
        assert generate(client).status_code == 409
    finally:
        app.state.settings.reward_every_n_orders = original
    thresholds = [r[0] for r in rows(db, "SELECT milestone_order_count FROM coupons ORDER BY 1")]
    assert thresholds == sorted(set(thresholds))  # never duplicated


# ----------------------------------------------------------------- redemption
def test_coupon_redemption_applies_discount_and_is_single_use(client, db):
    coupon = new_coupon(client)  # 3 prior orders of product 5 @150
    cart = make_cart(client, {1: 3, 2: 1})  # 3000 + 2550 = 5550
    r = checkout(client, cart, "redeem-1", coupon=coupon["code"].lower())  # case-insensitive
    assert r.status_code == 201, r.text
    o = r.json()
    assert o["gross_cents"] == 5550
    assert o["discount_percent"] == REWARD_PERCENT
    assert o["discount_cents"] == 555 and o["net_cents"] == 4995
    assert o["coupon_code"] == coupon["code"]
    assert scalar(db, "SELECT status FROM coupons WHERE code = :c", c=coupon["code"]) == "REDEEMED"

    cart2 = make_cart(client, {1: 1})
    r = checkout(client, cart2, "redeem-2", coupon=coupon["code"])
    assert r.status_code == 409 and err(r)["code"] == "COUPON_ALREADY_REDEEMED"
    assert scalar(db, "SELECT count(*) FROM orders WHERE cart_id = :c", c=cart2) == 0


def test_unknown_coupon_creates_no_order_and_consumes_no_stock(client, db):
    """T11"""
    cart = make_cart(client, {1: 2})
    r = checkout(client, cart, "bad-coupon", coupon="DOES-NOT-EXIST")
    assert r.status_code == 422 and err(r)["code"] == "COUPON_NOT_FOUND"
    assert inventory(db, 1) == 100
    assert scalar(db, "SELECT count(*) FROM orders") == 0
    assert scalar(db, "SELECT status FROM carts WHERE id = :c", c=cart) == "OPEN"
    assert scalar(db, "SELECT count(*) FROM idempotency_keys") == 0


def test_blank_coupon_code_is_validation_error(client):
    cart = make_cart(client, {1: 1})
    r = checkout(client, cart, "blank", coupon="   ")
    assert r.status_code == 422 and err(r)["code"] == "VALIDATION_ERROR"


def test_coupon_not_consumed_by_checkout_that_rolls_back(client, db):
    """Invariant 7: a failure after the coupon was claimed leaves it AVAILABLE."""
    coupon = new_coupon(client)
    cart = make_cart(client, {1: 1})

    def boom(**ctx):
        raise RuntimeError("fail after coupon claim")

    hooks.hooks["checkout.after_coupon"] = boom
    assert checkout(client, cart, "rb", coupon=coupon["code"]).status_code == 500
    hooks.hooks.clear()

    assert scalar(db, "SELECT status FROM coupons WHERE code = :c", c=coupon["code"]) == "AVAILABLE"
    assert scalar(db, "SELECT redeemed_at FROM coupons WHERE code = :c", c=coupon["code"]) is None
    assert inventory(db, 1) == 100
    assert checkout(client, cart, "rb", coupon=coupon["code"]).status_code == 201  # still usable


def test_coupon_not_consumed_when_stock_insufficient(client, db):
    coupon = new_coupon(client)
    cart = make_cart(client, {6: 1})
    set_inventory(db, 6, 0)
    assert checkout(client, cart, "nostock", coupon=coupon["code"]).status_code == 409
    assert scalar(db, "SELECT status FROM coupons WHERE code = :c", c=coupon["code"]) == "AVAILABLE"


def test_full_discount_never_negative(client, db):
    """100% coupon: discount == gross, net == 0, never negative (DB CHECKs agree)."""
    place_orders(client, REWARD_N)
    insert_coupon(db, "FULL100", 99, 100)
    cart = make_cart(client, {5: 1})
    o = checkout(client, cart, "free", coupon="FULL100").json()
    assert o["gross_cents"] == 150 and o["discount_cents"] == 150 and o["net_cents"] == 0


def test_replay_of_coupon_checkout_does_not_redeem_twice(client, db):
    coupon = new_coupon(client)
    cart = make_cart(client, {1: 1})
    a = checkout(client, cart, "idem-c", coupon=coupon["code"])
    b = checkout(client, cart, "idem-c", coupon=coupon["code"])
    assert a.status_code == b.status_code == 201 and a.json() == b.json()
    assert scalar(db, "SELECT count(*) FROM orders WHERE coupon_id IS NOT NULL") == 1


# ----------------------------------------------------------------- concurrency
def test_two_checkouts_cannot_redeem_one_coupon(app, client, db):
    """T9: both carts claim the same coupon at the same moment; exactly one wins."""
    coupon = new_coupon(client)
    cart_a = make_cart(client, {1: 1})
    cart_b = make_cart(client, {2: 1})
    results = run_with_pause(
        app,
        db,
        "checkout.after_coupon",
        [
            lambda c: checkout(c, cart_a, "cp-a", coupon=coupon["code"]),
            lambda c: checkout(c, cart_b, "cp-b", coupon=coupon["code"]),
        ],
    )
    assert sorted(r.status_code for r in results) == [201, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert err(loser)["code"] == "COUPON_ALREADY_REDEEMED"
    assert scalar(db, "SELECT count(*) FROM orders WHERE coupon_id IS NOT NULL") == 1
    assert scalar(db, "SELECT status FROM coupons WHERE code = :c", c=coupon["code"]) == "REDEEMED"
    # the loser consumed nothing
    winner_items = {1: 99, 2: 100} if results[0].status_code == 201 else {1: 100, 2: 99}
    for pid, expected in winner_items.items():
        assert inventory(db, pid) == expected


def test_concurrent_generation_yields_exactly_one_coupon_per_milestone(app, client, db):
    """T12: 10 simultaneous admin requests, one milestone reached -> exactly one coupon."""
    place_orders(client, REWARD_N)
    results = run_concurrently(app, [generate] * 10)
    codes = sorted(r.status_code for r in results)
    assert codes == [201] + [409] * 9
    assert all(err(r)["code"] == "NO_ELIGIBLE_MILESTONE" for r in results if r.status_code == 409)
    assert scalar(db, "SELECT count(*) FROM coupons") == 1


def test_concurrent_generation_with_backlog_never_duplicates(app, client, db):
    place_orders(client, REWARD_N * 3)  # three milestones available
    results = run_concurrently(app, [generate] * 8)
    assert sorted(r.status_code for r in results) == [201] * 3 + [409] * 5
    thresholds = [r[0] for r in rows(db, "SELECT milestone_order_count FROM coupons ORDER BY 1")]
    assert thresholds == [REWARD_N, REWARD_N * 2, REWARD_N * 3]
    assert len({r.json()["code"] for r in results if r.status_code == 201}) == 3


def test_generation_serialises_on_advisory_lock(app, client, db):
    """Deterministic overlap: request 1 holds the advisory lock; request 2 is observed
    blocked inside PostgreSQL before request 1 is allowed to finish."""
    place_orders(client, REWARD_N)
    results = run_with_pause(
        app, db, "rewards.after_generation_lock", [generate, generate], expected_waiters=1
    )
    assert sorted(r.status_code for r in results) == [201, 409]
    assert scalar(db, "SELECT count(*) FROM coupons") == 1


def test_claim_coupon_conditional_update_alone_blocks_second_claim(db):
    """Isolates the *application-level* guard (AVAILABLE -> REDEEMED conditional UPDATE),
    independent of the UNIQUE(orders.coupon_id) backstop: a second claim must be rejected
    and must not even rewrite redeemed_at."""
    insert_coupon(db, "SOLO", 1, 10)
    with db.session_factory() as s, s.begin():
        assert rewards_mod.claim_coupon(s, "SOLO").discount_percent == 10
    first = scalar(db, "SELECT redeemed_at FROM coupons WHERE code = 'SOLO'")
    with db.session_factory() as s, s.begin():
        with pytest.raises(AppError) as e:
            rewards_mod.claim_coupon(s, "SOLO")
    assert e.value.code == "COUPON_ALREADY_REDEEMED"
    assert scalar(db, "SELECT redeemed_at FROM coupons WHERE code = 'SOLO'") == first
