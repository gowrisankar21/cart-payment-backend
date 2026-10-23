import datetime as dt
import uuid

from pydantic import BaseModel


class CouponOut(BaseModel):
    id: uuid.UUID
    code: str
    milestone_order_count: int
    discount_percent: int
    status: str
    generated_at: dt.datetime
    redeemed_at: dt.datetime | None


class ProductQuantityOut(BaseModel):
    product_id: int
    name: str
    quantity_sold: int


class CouponCountsOut(BaseModel):
    generated: int
    available: int
    redeemed: int


class ReportOut(BaseModel):
    generated_at: dt.datetime
    currency: str
    total_orders: int
    gross_revenue_cents: int
    total_discounts_cents: int
    net_revenue_cents: int
    quantity_by_product: list[ProductQuantityOut]
    coupons: CouponCountsOut
