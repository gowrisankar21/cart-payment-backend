from sqlalchemy import select

from app.core.db import Database
from app.models import Product
from app.schemas.product import ProductOut


def list_products(db: Database) -> list[ProductOut]:
    with db.session_factory() as s, s.begin():
        rows = s.execute(select(Product).order_by(Product.id)).scalars().all()
        return [
            ProductOut(id=p.id, name=p.name, price_cents=p.price_cents, inventory=p.inventory)
            for p in rows
        ]
