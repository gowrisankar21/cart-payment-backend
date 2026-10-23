import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import Database
from app.core.errors import not_found
from app.models import Coupon, Order, OrderItem
from app.schemas.order import OrderItemOut, OrderOut


def build_order_view(session: Session, order: Order) -> OrderOut:
    items = (
        session.execute(
            select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.product_id)
        )
        .scalars()
        .all()
    )
    coupon_code = None
    if order.coupon_id is not None:
        coupon_code = session.execute(
            select(Coupon.code).where(Coupon.id == order.coupon_id)
        ).scalar_one()
    return OrderOut(
        id=order.id,
        cart_id=order.cart_id,
        items=[
            OrderItemOut(
                product_id=i.product_id,
                product_name=i.product_name,
                unit_price_cents=i.unit_price_cents,
                quantity=i.quantity,
                line_total_cents=i.line_total_cents,
            )
            for i in items
        ],
        gross_cents=order.gross_cents,
        discount_percent=order.discount_percent,
        discount_cents=order.discount_cents,
        net_cents=order.net_cents,
        coupon_code=coupon_code,
        currency=order.currency,
        placed_at=order.placed_at,
    )


def get_order(db: Database, order_id: uuid.UUID) -> OrderOut:
    with db.session_factory() as s, s.begin():
        order = s.get(Order, order_id)
        if order is None:
            raise not_found("ORDER_NOT_FOUND", "Order not found.", {"order_id": str(order_id)})
        return build_order_view(s, order)


def list_orders(db: Database, limit: int, offset: int) -> list[OrderOut]:
    """Read-only snapshot so the page of orders is internally consistent."""
    with db.read_only_session_factory() as s, s.begin():
        orders = (
            s.execute(select(Order).order_by(Order.placed_at, Order.id).limit(limit).offset(offset))
            .scalars()
            .all()
        )
        return [build_order_view(s, o) for o in orders]
