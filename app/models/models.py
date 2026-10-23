"""SQLAlchemy 2.x models. The Alembic migration is the authoritative schema; constraints are
mirrored here. Money is always integer cents (USD)."""

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now():
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="ck_products_name_not_blank"),
        CheckConstraint("price_cents > 0", name="ck_products_price_positive"),
        CheckConstraint("inventory >= 0", name="ck_products_inventory_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inventory: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = _now()
    updated_at: Mapped[dt.datetime] = _now()


class Cart(Base):
    __tablename__ = "carts"
    __table_args__ = (
        CheckConstraint("status IN ('OPEN','CHECKED_OUT')", name="ck_carts_status"),
        CheckConstraint(
            "(status = 'CHECKED_OUT') = (checked_out_at IS NOT NULL)",
            name="ck_carts_checked_out_at_matches_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    created_at: Mapped[dt.datetime] = _now()
    updated_at: Mapped[dt.datetime] = _now()
    checked_out_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (
        CheckConstraint("quantity BETWEEN 1 AND 100", name="ck_cart_items_quantity_range"),
    )

    cart_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("carts.id"), primary_key=True
    )
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("products.id"), primary_key=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = _now()
    updated_at: Mapped[dt.datetime] = _now()


class Coupon(Base):
    __tablename__ = "coupons"
    __table_args__ = (
        UniqueConstraint("code", name="uq_coupons_code"),
        UniqueConstraint("milestone_order_count", name="uq_coupons_milestone_order_count"),
        CheckConstraint("milestone_order_count > 0", name="ck_coupons_milestone_positive"),
        CheckConstraint("discount_percent BETWEEN 1 AND 100", name="ck_coupons_percent_range"),
        CheckConstraint("status IN ('AVAILABLE','REDEEMED')", name="ck_coupons_status"),
        CheckConstraint(
            "(status = 'REDEEMED') = (redeemed_at IS NOT NULL)",
            name="ck_coupons_redeemed_at_matches_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    # Persisted absolute threshold (n, 2n, 3n ... at generation time), not a mutable counter.
    milestone_order_count: Mapped[int] = mapped_column(Integer, nullable=False)
    discount_percent: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="AVAILABLE")
    generated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    redeemed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("cart_id", name="uq_orders_cart_id"),
        UniqueConstraint("coupon_id", name="uq_orders_coupon_id"),
        CheckConstraint("gross_cents > 0", name="ck_orders_gross_positive"),
        CheckConstraint(
            "discount_cents >= 0 AND discount_cents <= gross_cents", name="ck_orders_discount_range"
        ),
        CheckConstraint("net_cents = gross_cents - discount_cents", name="ck_orders_net_formula"),
        CheckConstraint("currency = 'USD'", name="ck_orders_currency"),
        CheckConstraint(
            "(coupon_id IS NULL) = (discount_percent IS NULL)", name="ck_orders_coupon_percent_pair"
        ),
        CheckConstraint(
            "coupon_id IS NOT NULL OR discount_cents = 0", name="ck_orders_discount_requires_coupon"
        ),
        CheckConstraint(
            "discount_percent IS NULL OR discount_percent BETWEEN 1 AND 100",
            name="ck_orders_percent_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cart_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("carts.id"), nullable=False
    )
    coupon_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coupons.id")
    )
    discount_percent: Mapped[int | None] = mapped_column(SmallInteger)
    gross_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    placed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        UniqueConstraint("order_id", "product_id", name="uq_order_items_order_product"),
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint("unit_price_cents > 0", name="ck_order_items_price_positive"),
        CheckConstraint(
            "line_total_cents = unit_price_cents * quantity", name="ck_order_items_line_total"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("products.id"), nullable=False
    )
    product_name: Mapped[str] = mapped_column(Text, nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    line_total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
        UniqueConstraint("order_id", name="uq_idempotency_order_id"),
        CheckConstraint(
            "(response_status IS NULL) = (response_body IS NULL) "
            "AND (response_status IS NULL) = (order_id IS NULL)",
            name="ck_idempotency_result_complete",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id")
    )
    response_status: Mapped[int | None] = mapped_column(SmallInteger)
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = _now()
