"""Reporting: strictly read-only.

One ``REPEATABLE READ READ ONLY`` transaction => every aggregate is computed from the same
snapshot (a READ COMMITTED report could mix pre- and post-commit state across statements),
and PostgreSQL itself rejects any write attempted inside it.
"""

from sqlalchemy import text

from app.core.db import Database
from app.schemas.reward import CouponCountsOut, ProductQuantityOut, ReportOut
from app.services import money


def summary_report(db: Database) -> ReportOut:
    with db.read_only_session_factory() as s, s.begin():
        totals = s.execute(
            text(
                "SELECT count(*) AS orders, "
                "COALESCE(sum(gross_cents), 0) AS gross, "
                "COALESCE(sum(discount_cents), 0) AS discounts, "
                "COALESCE(sum(net_cents), 0) AS net "
                "FROM orders"
            )
        ).one()
        by_product = s.execute(
            text(
                "SELECT oi.product_id, "
                "(SELECT name FROM products p WHERE p.id = oi.product_id) AS name, "
                "sum(oi.quantity) AS qty "
                "FROM order_items oi GROUP BY oi.product_id ORDER BY oi.product_id"
            )
        ).all()
        coupons = s.execute(
            text(
                "SELECT count(*) AS generated, "
                "count(*) FILTER (WHERE status = 'AVAILABLE') AS available, "
                "count(*) FILTER (WHERE status = 'REDEEMED') AS redeemed "
                "FROM coupons"
            )
        ).one()
        now = s.execute(text("SELECT now()")).scalar_one()
    return ReportOut(
        generated_at=now,
        currency=money.CURRENCY,
        total_orders=totals.orders,
        gross_revenue_cents=int(totals.gross),
        total_discounts_cents=int(totals.discounts),
        net_revenue_cents=int(totals.net),
        quantity_by_product=[
            ProductQuantityOut(product_id=r.product_id, name=r.name, quantity_sold=int(r.qty))
            for r in by_product
        ],
        coupons=CouponCountsOut(
            generated=coupons.generated, available=coupons.available, redeemed=coupons.redeemed
        ),
    )
