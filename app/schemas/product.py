from pydantic import BaseModel


class ProductOut(BaseModel):
    id: int
    name: str
    price_cents: int
    inventory: int
