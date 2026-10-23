"""Database constraints are the final safeguard: prove they hold even when the application
layer is bypassed with raw SQL."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from tests.conftest import exec_sql as exec_raw
from tests.conftest import make_cart


def constraint_of(exc: IntegrityError) -> str:
    return exc.value.orig.diag.constraint_name


def test_inventory_can_never_go_negative(db):
    with pytest.raises(IntegrityError) as e:
        exec_raw(db, "UPDATE products SET inventory = -1 WHERE id = 1")
    assert constraint_of(e) == "ck_products_inventory_non_negative"


def test_non_positive_price_rejected(db):
    with pytest.raises(IntegrityError) as e:
        exec_raw(db, "UPDATE products SET price_cents = 0 WHERE id = 1")
    assert constraint_of(e) == "ck_products_price_positive"


def test_cart_item_quantity_bounds(client, db):
    cart = make_cart(client)
    for bad in (0, -1, 101):
        with pytest.raises(IntegrityError) as e:
            exec_raw(
                db,
                "INSERT INTO cart_items (cart_id, product_id, quantity) VALUES (:c, 1, :q)",
                c=cart,
                q=bad,
            )
        assert constraint_of(e) == "ck_cart_items_quantity_range"


def _insert_order(db, cart_id, *, coupon_id=None, pct=None, gross=1000, disc=0, net=1000):
    exec_raw(
        db,
        "INSERT INTO orders (id, cart_id, coupon_id, discount_percent, gross_cents, "
        "discount_cents, net_cents) VALUES (:id, :cart, :cp, :pct, :g, :d, :n)",
        id=uuid.uuid4(), cart=cart_id, cp=coupon_id, pct=pct, g=gross, d=disc, n=net,
    )


def test_one_order_per_cart(client, db):
    cart = make_cart(client)
    _insert_order(db, cart)
    with pytest.raises(IntegrityError) as e:
        _insert_order(db, cart)
    assert constraint_of(e) == "uq_orders_cart_id"


def test_order_totals_must_be_consistent_and_non_negative(client, db):
    cart = make_cart(client)
    with pytest.raises(IntegrityError) as e:
        _insert_order(db, cart, gross=1000, disc=0, net=999)
    assert constraint_of(e) == "ck_orders_net_formula"
    exec_raw(db, "INSERT INTO coupons (id, code, milestone_order_count, discount_percent) "
                 "VALUES (:i, 'C1', 5, 10)", i=uuid.uuid4())
    cid = uuid.uuid4()
    exec_raw(db, "UPDATE coupons SET id = :i WHERE code = 'C1'", i=cid)
    with pytest.raises(IntegrityError) as e:  # discount larger than gross => negative net
        _insert_order(db, cart, coupon_id=cid, pct=10, gross=100, disc=150, net=-50)
    assert constraint_of(e) in ("ck_orders_discount_range", "ck_orders_net_formula")
    with pytest.raises(IntegrityError) as e:  # discount without a coupon
        _insert_order(db, cart, gross=1000, disc=100, net=900)
    assert constraint_of(e) == "ck_orders_discount_requires_coupon"


def test_a_coupon_can_back_at_most_one_order(client, db):
    cid = uuid.uuid4()
    exec_raw(db, "INSERT INTO coupons (id, code, milestone_order_count, discount_percent, "
                 "status, redeemed_at) VALUES (:i, 'ONCE', 5, 10, 'REDEEMED', now())", i=cid)
    _insert_order(db, make_cart(client), coupon_id=cid, pct=10, gross=1000, disc=100, net=900)
    with pytest.raises(IntegrityError) as e:
        _insert_order(db, make_cart(client), coupon_id=cid, pct=10, gross=1000, disc=100, net=900)
    assert constraint_of(e) == "uq_orders_coupon_id"


def test_one_coupon_per_milestone_and_unique_codes(db):
    exec_raw(db, "INSERT INTO coupons (id, code, milestone_order_count, discount_percent) "
                 "VALUES (:i, 'A', 5, 10)", i=uuid.uuid4())
    with pytest.raises(IntegrityError) as e:
        exec_raw(db, "INSERT INTO coupons (id, code, milestone_order_count, discount_percent) "
                     "VALUES (:i, 'B', 5, 10)", i=uuid.uuid4())
    assert constraint_of(e) == "uq_coupons_milestone_order_count"
    with pytest.raises(IntegrityError) as e:
        exec_raw(db, "INSERT INTO coupons (id, code, milestone_order_count, discount_percent) "
                     "VALUES (:i, 'A', 10, 10)", i=uuid.uuid4())
    assert constraint_of(e) == "uq_coupons_code"


def test_coupon_status_and_redeemed_at_must_agree(db):
    with pytest.raises(IntegrityError) as e:
        exec_raw(db, "INSERT INTO coupons (id, code, milestone_order_count, discount_percent, status) "
                     "VALUES (:i, 'X', 5, 10, 'REDEEMED')", i=uuid.uuid4())
    assert constraint_of(e) == "ck_coupons_redeemed_at_matches_status"


def test_order_item_line_total_must_equal_price_times_quantity(client, db):
    cart = make_cart(client)
    oid = uuid.uuid4()
    exec_raw(db, "INSERT INTO orders (id, cart_id, gross_cents, discount_cents, net_cents) "
                 "VALUES (:o, :c, 1000, 0, 1000)", o=oid, c=cart)
    with pytest.raises(IntegrityError) as e:
        exec_raw(db, "INSERT INTO order_items (id, order_id, product_id, product_name, "
                     "unit_price_cents, quantity, line_total_cents) "
                     "VALUES (:i, :o, 1, 'W', 1000, 2, 1999)", i=uuid.uuid4(), o=oid)
    assert constraint_of(e) == "ck_order_items_line_total"


def test_idempotency_scope_key_unique(db):
    exec_raw(db, "INSERT INTO idempotency_keys (id, scope, key, request_hash) "
                 "VALUES (:i, 'checkout', 'k', :h)", i=uuid.uuid4(), h="a" * 64)
    with pytest.raises(IntegrityError) as e:
        exec_raw(db, "INSERT INTO idempotency_keys (id, scope, key, request_hash) "
                     "VALUES (:i, 'checkout', 'k', :h)", i=uuid.uuid4(), h="b" * 64)
    assert constraint_of(e) == "uq_idempotency_scope_key"


def test_checked_out_cart_requires_timestamp(client, db):
    cart = make_cart(client)
    with pytest.raises(IntegrityError) as e:
        exec_raw(db, "UPDATE carts SET status = 'CHECKED_OUT' WHERE id = :c", c=cart)
    assert constraint_of(e) == "ck_carts_checked_out_at_matches_status"
