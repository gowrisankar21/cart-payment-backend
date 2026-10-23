"""Cart use cases. Each public function owns exactly one short transaction.

Every mutation takes ``SELECT ... FOR UPDATE`` on the cart row first, so cart mutations
serialise with each other and with checkout (which locks the same row). A mutation that
waited behind a checkout wakes up, sees CHECKED_OUT and returns CART_NOT_OPEN.
Carts never reserve inventory; stock is only consumed by checkout.
"""

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.db import Database
from app.core.errors import (
    cart_not_found,
    cart_not_open,
    conflict,
    insufficient_stock,
    item_not_in_cart,
    unprocessable,
    validation_error,
)
from app.models import Cart, CartItem, Product
from app.schemas.cart import CartItemOut, CartOut


def load_cart(session: Session, cart_id: uuid.UUID, *, lock: bool = False) -> Cart:
    stmt = select(Cart).where(Cart.id == cart_id)
    cart = session.execute(stmt.with_for_update() if lock else stmt).scalar_one_or_none()
    if cart is None:
        raise cart_not_found(cart_id)
    return cart


def lock_and_require_open(session: Session, cart_id: uuid.UUID) -> Cart:
    cart = load_cart(session, cart_id, lock=True)
    if cart.status != "OPEN":
        raise cart_not_open(cart_id)
    return cart


def _validate_quantity(quantity: int, settings: Settings) -> None:
    if not 1 <= quantity <= settings.max_line_quantity:
        raise validation_error(
            "quantity", f"quantity must be between 1 and {settings.max_line_quantity}."
        )


def _require_stock(product: Product, quantity: int) -> None:
    if quantity > product.inventory:
        raise insufficient_stock([(product.id, quantity, product.inventory)])


def build_cart_view(session: Session, cart: Cart) -> CartOut:
    rows = session.execute(
        select(CartItem.quantity, Product)
        .join(Product, Product.id == CartItem.product_id)
        .where(CartItem.cart_id == cart.id)
        .order_by(Product.id)
    ).all()
    items = [
        CartItemOut(
            product_id=p.id,
            name=p.name,
            unit_price_cents=p.price_cents,
            quantity=qty,
            line_total_cents=p.price_cents * qty,
            available_inventory=p.inventory,
            in_stock=p.inventory >= qty,
        )
        for qty, p in rows
    ]
    return CartOut(
        id=cart.id,
        status=cart.status,
        items=items,
        subtotal_cents=sum(i.line_total_cents for i in items),
    )


def create_cart(db: Database) -> CartOut:
    with db.session_factory() as s, s.begin():
        cart = Cart(id=uuid.uuid4(), status="OPEN")
        s.add(cart)
        s.flush()
        return build_cart_view(s, cart)


def get_cart(db: Database, cart_id: uuid.UUID) -> CartOut:
    with db.session_factory() as s, s.begin():
        return build_cart_view(s, load_cart(s, cart_id))


def add_item(
    db: Database, settings: Settings, cart_id: uuid.UUID, product_id: int, quantity: int
) -> CartOut:
    _validate_quantity(quantity, settings)
    with db.session_factory() as s, s.begin():
        cart = lock_and_require_open(s, cart_id)
        product = s.get(Product, product_id)
        if product is None:
            raise unprocessable(
                "PRODUCT_NOT_FOUND", "Product does not exist.", {"product_id": product_id}
            )
        if s.get(CartItem, (cart_id, product_id)) is not None:
            raise conflict(
                "ITEM_ALREADY_IN_CART",
                "Product is already in the cart. Use PUT to change its quantity.",
                {"product_id": product_id},
            )
        count = s.execute(
            select(func.count()).select_from(CartItem).where(CartItem.cart_id == cart_id)
        ).scalar_one()
        if count >= settings.max_cart_items:
            raise unprocessable(
                "CART_FULL",
                f"A cart can hold at most {settings.max_cart_items} distinct products.",
            )
        _require_stock(product, quantity)
        s.add(CartItem(cart_id=cart_id, product_id=product_id, quantity=quantity))
        s.flush()
        return build_cart_view(s, cart)


def update_item(
    db: Database, settings: Settings, cart_id: uuid.UUID, product_id: int, quantity: int
) -> CartOut:
    _validate_quantity(quantity, settings)
    with db.session_factory() as s, s.begin():
        cart = lock_and_require_open(s, cart_id)
        item = s.get(CartItem, (cart_id, product_id))
        if item is None:
            raise item_not_in_cart(product_id)
        _require_stock(s.get(Product, product_id), quantity)
        item.quantity = quantity
        s.flush()
        return build_cart_view(s, cart)


def remove_item(db: Database, cart_id: uuid.UUID, product_id: int) -> CartOut:
    with db.session_factory() as s, s.begin():
        cart = lock_and_require_open(s, cart_id)
        deleted = s.execute(
            delete(CartItem).where(CartItem.cart_id == cart_id, CartItem.product_id == product_id)
        ).rowcount
        if deleted == 0:
            raise item_not_in_cart(product_id)
        return build_cart_view(s, cart)
