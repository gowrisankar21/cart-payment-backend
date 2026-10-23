import datetime as dt
import uuid

from pydantic import BaseModel


class OrderItemOut(BaseModel):
    product_id: int
    product_name: str
    unit_price_cents: int
    quantity: int
    line_total_cents: int


class OrderOut(BaseModel):
    id: uuid.UUID
    cart_id: uuid.UUID
    items: list[OrderItemOut]
    gross_cents: int
    discount_percent: int | None
    discount_cents: int
    net_cents: int
    coupon_code: str | None
    currency: str
    placed_at: dt.datetime


class CheckoutRequest(BaseModel):
    coupon_code: str | None = None
