import uuid

from fastapi import APIRouter, Depends

from app.api.deps import get_db, get_settings
from app.core.config import Settings
from app.core.db import Database
from app.schemas.cart import AddItemRequest, CartOut, UpdateItemRequest
from app.services import cart as cart_service

router = APIRouter(prefix="/carts", tags=["carts"])


@router.post("", response_model=CartOut, status_code=201)
def create_cart(db: Database = Depends(get_db)):
    return cart_service.create_cart(db)


@router.get("/{cart_id}", response_model=CartOut)
def get_cart(cart_id: uuid.UUID, db: Database = Depends(get_db)):
    return cart_service.get_cart(db, cart_id)


@router.post("/{cart_id}/items", response_model=CartOut, status_code=201)
def add_item(
    cart_id: uuid.UUID,
    body: AddItemRequest,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    return cart_service.add_item(db, settings, cart_id, body.product_id, body.quantity)


@router.put("/{cart_id}/items/{product_id}", response_model=CartOut)
def update_item(
    cart_id: uuid.UUID,
    product_id: int,
    body: UpdateItemRequest,
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    return cart_service.update_item(db, settings, cart_id, product_id, body.quantity)


@router.delete("/{cart_id}/items/{product_id}", response_model=CartOut)
def remove_item(cart_id: uuid.UUID, product_id: int, db: Database = Depends(get_db)):
    return cart_service.remove_item(db, cart_id, product_id)
