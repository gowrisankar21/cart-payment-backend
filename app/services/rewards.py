"""Rewards: coupon redemption (participates in the checkout transaction) and
administrative coupon generation (owns its own transaction).

Milestones are *persisted absolute order counts* (``coupons.milestone_order_count``),
not a mutable counter. The eligible set is
    { n, 2n, 3n, ... } ∩ [1..N]  minus  { milestones that already have a coupon }
where N = COUNT(orders) (only successful orders exist). Changing ``n`` later never
voids or duplicates an existing coupon: the UNIQUE threshold is the source of truth.
"""

import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import hooks
from app.core.config import Settings
from app.core.db import Database
from app.core.errors import (
    conflict,
    constraint_name,
    coupon_already_redeemed,
    unprocessable,
)
from app.models import Coupon
from app.schemas.reward import CouponOut

# Advisory-lock key for coupon generation. The lock lives in PostgreSQL, so it is shared by
# every app instance (unlike a process-local lock).
COUPON_GENERATION_LOCK_KEY = 7_265_001


@dataclass(frozen=True)
class ClaimedCoupon:
    id: object
    code: str
    discount_percent: int


def claim_coupon(session: Session, code: str) -> ClaimedCoupon:
    """Atomically move a coupon AVAILABLE -> REDEEMED inside the caller's transaction.

    The conditional UPDATE succeeds for exactly one transaction. A competing transaction
    blocks on the row lock until the first one commits (then matches 0 rows -> conflict)
    or rolls back (then it wins). Never commits: if the surrounding checkout rolls back,
    the coupon is automatically AVAILABLE again.
    """
    row = session.execute(
        text(
            "UPDATE coupons SET status = 'REDEEMED', redeemed_at = now() "
            "WHERE code = :code AND status = 'AVAILABLE' "
            "RETURNING id, code, discount_percent"
        ),
        {"code": code},
    ).first()
    if row is not None:
        return ClaimedCoupon(id=row.id, code=row.code, discount_percent=row.discount_percent)

    exists = session.execute(
        text("SELECT 1 FROM coupons WHERE code = :code"), {"code": code}
    ).first()
    if exists is None:
        raise unprocessable("COUPON_NOT_FOUND", "Coupon code is not valid.")
    raise coupon_already_redeemed()


def to_coupon_out(c: Coupon) -> CouponOut:
    return CouponOut(
        id=c.id,
        code=c.code,
        milestone_order_count=c.milestone_order_count,
        discount_percent=c.discount_percent,
        status=c.status,
        generated_at=c.generated_at,
        redeemed_at=c.redeemed_at,
    )


def generate_coupon(db: Database, settings: Settings) -> CouponOut:
    """ADMIN. Generate one coupon for the lowest eligible, un-rewarded milestone.

    Serialised across all instances by a transaction-scoped advisory lock; the UNIQUE
    constraint on ``milestone_order_count`` is the final backstop. A loser of the race
    waits for the lock, then re-evaluates eligibility with a fresh READ COMMITTED
    snapshot and either takes the next milestone or gets NO_ELIGIBLE_MILESTONE.
    """
    n = settings.reward_every_n_orders
    try:
        with db.session_factory() as s, s.begin():
            s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": COUPON_GENERATION_LOCK_KEY})
            hooks.checkpoint("rewards.after_generation_lock")
            total_orders = s.execute(text("SELECT count(*) FROM orders")).scalar_one()
            milestone = s.execute(
                text(
                    "SELECT m FROM generate_series("
                    "CAST(:n AS integer), CAST(:total AS integer), CAST(:n AS integer)) AS m "
                    "WHERE NOT EXISTS (SELECT 1 FROM coupons WHERE milestone_order_count = m) "
                    "ORDER BY m LIMIT 1"
                ),
                {"n": n, "total": total_orders},
            ).scalar_one_or_none() if total_orders >= n else None
            if milestone is None:
                raise conflict(
                    "NO_ELIGIBLE_MILESTONE",
                    "No un-rewarded order milestone has been reached.",
                    {"total_orders": total_orders, "reward_every_n_orders": n},
                )
            coupon = Coupon(
                id=uuid.uuid4(),
                code="RWD-" + secrets.token_hex(6).upper(),
                milestone_order_count=milestone,
                discount_percent=settings.reward_discount_percent,
                status="AVAILABLE",
            )
            s.add(coupon)
            s.flush()
            s.refresh(coupon)
            return to_coupon_out(coupon)
    except IntegrityError as exc:
        if constraint_name(exc) == "uq_coupons_milestone_order_count":
            # Unreachable while the advisory lock is held; mapped defensively, never a 500.
            raise conflict("NO_ELIGIBLE_MILESTONE", "That milestone already has a coupon.") from None
        raise


def list_coupons(db: Database) -> list[CouponOut]:
    with db.session_factory() as s, s.begin():
        rows = s.execute(select(Coupon).order_by(Coupon.milestone_order_count)).scalars().all()
        return [to_coupon_out(c) for c in rows]
