import uuid

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from app.api.deps import get_db, get_settings
from app.core.config import Settings
from app.core.db import Database
from app.schemas.order import CheckoutRequest, OrderOut
from app.services import checkout as checkout_service
from app.services import orders as order_service

router = APIRouter(tags=["orders"])


@router.post(
    "/carts/{cart_id}/checkout",
    response_model=OrderOut,
    status_code=201,
    summary="Check out a cart (Idempotency-Key header required)",
)
def checkout(
    cart_id: uuid.UUID,
    body: CheckoutRequest | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    result = checkout_service.checkout(
        db, settings, cart_id, idempotency_key, body.coupon_code if body else None
    )
    headers = {"Idempotent-Replayed": "true"} if result.replayed else {}
    return JSONResponse(status_code=result.status_code, content=result.body, headers=headers)


@router.get("/orders/{order_id}", response_model=OrderOut)
def get_order(order_id: uuid.UUID, db: Database = Depends(get_db)):
    return order_service.get_order(db, order_id)
