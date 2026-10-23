"""ADMINISTRATIVE endpoints (privileged).

Authentication/authorisation is intentionally NOT implemented in this assignment. In
production every route here must sit behind admin authentication + RBAC (and audit
logging). They are grouped under ``/admin`` so a gateway/policy can protect the prefix.
"""

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_db, get_settings
from app.core.config import Settings
from app.core.db import Database
from app.schemas.order import OrderOut
from app.schemas.reward import CouponOut, ReportOut
from app.services import orders as order_service
from app.services import reporting, rewards

router = APIRouter(
    prefix="/admin",
    tags=["ADMIN (privileged - unauthenticated in this assignment)"],
)


@router.post("/coupons", response_model=CouponOut, status_code=201, summary="ADMIN: generate a coupon")
def generate_coupon(db: Database = Depends(get_db), settings: Settings = Depends(get_settings)):
    return rewards.generate_coupon(db, settings)


@router.get("/coupons", response_model=list[CouponOut], summary="ADMIN: list coupons")
def list_coupons(db: Database = Depends(get_db)):
    return rewards.list_coupons(db)


@router.get("/orders", response_model=list[OrderOut], summary="ADMIN: list orders")
def list_orders(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
):
    return order_service.list_orders(db, limit, offset)


@router.get("/reports/summary", response_model=ReportOut, summary="ADMIN: reward & revenue summary")
def summary(db: Database = Depends(get_db)):
    return reporting.summary_report(db)
