import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import (
    REWARD_N,
    checkout,
    make_cart,
    rows,
    run_concurrently,
    scalar,
    set_inventory,
)


def snapshot(db) -> dict:
    """Content fingerprint of every mutable table (row counts + checksums)."""
    out = {}
    for t in ("products", "carts", "cart_items", "orders", "order_items", "coupons", "idempotency_keys"):
        out[t] = rows(db, f"SELECT md5(string_agg(x::text, ',' ORDER BY x::text)), count(*) FROM {t} x")
    return out


def test_empty_report(client):
    r = client.get("/admin/reports/summary")
    assert r.status_code == 200
    b = r.json()
    assert b["total_orders"] == 0 and b["gross_revenue_cents"] == 0
    assert b["total_discounts_cents"] == 0 and b["net_revenue_cents"] == 0
    assert b["quantity_by_product"] == []
    assert b["coupons"] == {"generated": 0, "available": 0, "redeemed": 0}
    assert b["currency"] == "USD"


def test_report_reconciles_with_orders_and_coupons(client, db):
    """T14: every figure is recomputed independently from the API's own order/coupon lists."""
    for i in range(REWARD_N):
        c = make_cart(client, {1: 1 + i, 5: 2})
        assert checkout(client, c, f"r-{i}").status_code == 201
    coupon = client.post("/admin/coupons").json()
    c = make_cart(client, {2: 1, 3: 3, 5: 1})
    assert checkout(client, c, "with-coupon", coupon=coupon["code"]).status_code == 201
    for i in range(2):  # 4 + 2 = 6 orders -> second milestone reached
        assert checkout(client, make_cart(client, {4: 1}), f"x-{i}").status_code == 201
    second = client.post("/admin/coupons").json()  # available, never redeemed

    report = client.get("/admin/reports/summary").json()
    orders = client.get("/admin/orders?limit=200").json()
    coupons = client.get("/admin/coupons").json()

    assert report["total_orders"] == len(orders) == 6
    assert report["gross_revenue_cents"] == sum(o["gross_cents"] for o in orders)
    assert report["total_discounts_cents"] == sum(o["discount_cents"] for o in orders)
    assert report["net_revenue_cents"] == sum(o["net_cents"] for o in orders)
    assert report["net_revenue_cents"] == report["gross_revenue_cents"] - report["total_discounts_cents"]
    assert report["total_discounts_cents"] > 0

    expected_qty: dict[int, int] = {}
    for o in orders:
        for it in o["items"]:
            expected_qty[it["product_id"]] = expected_qty.get(it["product_id"], 0) + it["quantity"]
    assert {p["product_id"]: p["quantity_sold"] for p in report["quantity_by_product"]} == expected_qty
    assert [p["product_id"] for p in report["quantity_by_product"]] == sorted(expected_qty)

    cc = report["coupons"]
    assert cc["generated"] == len(coupons) == 2
    assert cc["available"] == sum(1 for c in coupons if c["status"] == "AVAILABLE") == 1
    assert cc["redeemed"] == sum(1 for c in coupons if c["status"] == "REDEEMED") == 1
    assert cc["generated"] == cc["available"] + cc["redeemed"]
    # every redeemed coupon is referenced by exactly one order, and vice-versa
    assert cc["redeemed"] == sum(1 for o in orders if o["coupon_code"])
    assert second["status"] == "AVAILABLE"

    # the discount applied equals the half-up percentage of that order's gross
    coupon_order = next(o for o in orders if o["coupon_code"])
    assert coupon_order["discount_cents"] == (coupon_order["gross_cents"] * 10 + 50) // 100
    for o in orders:
        assert o["gross_cents"] == sum(i["line_total_cents"] for i in o["items"])
        assert o["net_cents"] == o["gross_cents"] - o["discount_cents"]


def test_failed_checkouts_do_not_appear_in_report(client, db):
    ok = make_cart(client, {1: 1})
    assert checkout(client, ok, "good").status_code == 201
    bad = make_cart(client, {6: 1})
    set_inventory(db, 6, 0)
    assert checkout(client, bad, "bad").status_code == 409
    assert checkout(client, make_cart(client, {1: 1}), "bad2", coupon="NOPE").status_code == 422
    r = client.get("/admin/reports/summary").json()
    assert r["total_orders"] == 1 and r["gross_revenue_cents"] == 1000
    assert [p["product_id"] for p in r["quantity_by_product"]] == [1]


def test_repeated_reports_do_not_mutate_state(client, db):
    """T15"""
    for i in range(REWARD_N):
        assert checkout(client, make_cart(client, {1: 1}), f"s-{i}").status_code == 201
    assert client.post("/admin/coupons").status_code == 201
    before = snapshot(db)
    reports = [client.get("/admin/reports/summary").json() for _ in range(5)]
    client.get("/admin/orders")
    client.get("/admin/coupons")
    after = snapshot(db)
    assert before == after
    for r in reports[1:]:
        r2 = {k: v for k, v in r.items() if k != "generated_at"}
        r1 = {k: v for k, v in reports[0].items() if k != "generated_at"}
        assert r1 == r2


def test_report_transaction_is_read_only_in_postgres(db):
    """The report's session cannot write even if code tried: PostgreSQL rejects it."""
    with pytest.raises(DBAPIError) as exc:
        with db.read_only_session_factory() as s, s.begin():
            s.execute(text("UPDATE products SET inventory = inventory + 1 WHERE id = 1"))
    assert exc.value.orig.sqlstate == "25006"  # read_only_sql_transaction


def test_report_is_a_consistent_snapshot_under_concurrent_checkouts(app, client, db):
    """Orders committing while reports are generated never yield internally inconsistent
    reports (gross - discounts == net, qty matches orders count)."""
    carts = [make_cart(client, {1: 1}) for _ in range(12)]
    fns = [lambda c, cid=cid, i=i: checkout(c, cid, f"snap-{i}") for i, cid in enumerate(carts)]
    fns += [lambda c: c.get("/admin/reports/summary") for _ in range(12)]
    results = run_concurrently(app, fns)
    for r in results[12:]:
        b = r.json()
        assert b["gross_revenue_cents"] - b["total_discounts_cents"] == b["net_revenue_cents"]
        qty = sum(p["quantity_sold"] for p in b["quantity_by_product"])
        assert qty == b["total_orders"]  # one unit per order in this scenario
        assert b["gross_revenue_cents"] == 1000 * b["total_orders"]
    final = client.get("/admin/reports/summary").json()
    assert final["total_orders"] == 12 == scalar(db, "SELECT count(*) FROM orders")


def test_admin_orders_pagination_validation(client):
    assert client.get("/admin/orders?limit=0").status_code == 422
    assert client.get("/admin/orders?limit=1000").status_code == 422
    assert client.get("/admin/orders?offset=-1").status_code == 422
