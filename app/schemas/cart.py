import uuid

from pydantic import BaseModel, StrictInt


class AddItemRequest(BaseModel):
    # StrictInt: reject 1.5, "2", true. Range (1..MAX_LINE_QUANTITY) is checked in the service.
    product_id: StrictInt
    quantity: StrictInt


class UpdateItemRequest(BaseModel):
    quantity: StrictInt


class CartItemOut(BaseModel):
    product_id: int
    name: str
    unit_price_cents: int
    quantity: int
    line_total_cents: int
    available_inventory: int
    in_stock: bool


class CartOut(BaseModel):
    id: uuid.UUID
    status: str
    items: list[CartItemOut]
    subtotal_cents: int
    currency: str = "USD"
