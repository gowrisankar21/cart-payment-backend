"""Checkout: ONE PostgreSQL transaction (READ COMMITTED) owned by this module.

Lock / statement order (fixed, to avoid deadlocks):
  idempotency claim -> cart row -> products (ascending id) -> coupon -> inserts

Everything inside ``_checkout_once`` commits or rolls back together: the idempotency claim,
every inventory decrement, the coupon redemption, the order + items, the cart transition
and the stored idempotency response. There are no network calls inside the transaction
(payment is treated as succeeding at commit; see DECISIONS.md).
"""

import hashlib
import json
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core import hooks
from app.core.config import Settings
from app.core.db import Database
from app.core.errors import (
    AppError,
    conflict,
    constraint_name,
    coupon_already_redeemed,
    insufficient_stock,
    unprocessable,
    validation_error,
)
from app.models import Order, OrderItem
from app.services import money
from app.services.cart import lock_and_require_open
from app.services.orders import build_order_view
from app.services.rewards import claim_coupon

log = logging.getLogger("app.checkout")

SCOPE = "checkout"
_KEY_RE = re.compile(r"^[\x21-\x7E]{1,255}$")  # printable ASCII, no spaces
# deadlock, lock timeout, serialization failure, statement timeout
_TRANSIENT_SQLSTATES = {"40P01", "55P03", "40001", "57014"}


@dataclass(frozen=True)
class CheckoutResult:
    status_code: int
    body: dict
    replayed: bool


def validate_idempotency_key(key: str | None) -> str:
    if key is None or key == "":
        raise AppError(400, "IDEMPOTENCY_KEY_REQUIRED", "The Idempotency-Key header is required.")
    if not _KEY_RE.match(key):
        raise AppError(
            400,
            "IDEMPOTENCY_KEY_INVALID",
            "Idempotency-Key must be 1-255 printable ASCII characters without spaces.",
        )
    return key


def normalize_coupon_code(code: str | None) -> str | None:
    if code is None:
        return None
    normalized = code.strip().upper()
    if not normalized or len(normalized) > 64:
        raise validation_error(
            "coupon_code", "coupon_code must be a non-empty string of at most 64 characters."
        )
    return normalized


def request_hash(cart_id: uuid.UUID, coupon_code: str | None) -> str:
    """Hash of the *normalised* payload, so cosmetic differences (case, whitespace) match."""
    canonical = json.dumps(
        {"cart_id": str(cart_id), "coupon_code": coupon_code}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def checkout(
    db: Database,
    settings: Settings,
    cart_id: uuid.UUID,
    idempotency_key: str | None,
    coupon_code: str | None,
) -> CheckoutResult:
    key = validate_idempotency_key(idempotency_key)
    coupon = normalize_coupon_code(coupon_code)
    req_hash = request_hash(cart_id, coupon)

    attempts = settings.checkout_max_attempts
    for attempt in range(1, attempts + 1):
        try:
            return _checkout_once(db, cart_id, key, coupon, req_hash)
        except IntegrityError as exc:
            mapped = _map_integrity_error(exc)
            if mapped is None:
                raise
            raise mapped from None
        except DBAPIError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None)
            if sqlstate not in _TRANSIENT_SQLSTATES:
                raise
            # The whole transaction (including the idempotency claim) has rolled back and
            # nothing was committed, so re-running the entire use case is safe.
            log.warning(
                "checkout_transient_failure",
                extra={"extra_fields": {"sqlstate": sqlstate, "attempt": attempt}},
            )
            if attempt == attempts:
                raise AppError(
                    503,
                    "TRANSIENT_CONFLICT",
                    "The request conflicted with another operation. Retry with the same Idempotency-Key.",
                    headers={"Retry-After": "1"},
                ) from None
            time.sleep(random.uniform(0.02, 0.1) * attempt)
    raise AssertionError("unreachable")


def _map_integrity_error(exc: IntegrityError) -> AppError | None:
    """Unique-constraint backstops that the locks should make unreachable; never a 500."""
    name = constraint_name(exc)
    if name in ("uq_orders_cart_id", "uq_idempotency_order_id"):
        return conflict("CART_NOT_OPEN", "Cart has already been checked out.")
    if name == "uq_orders_coupon_id":
        return coupon_already_redeemed()
    return None


def _checkout_once(
    db: Database, cart_id: uuid.UUID, key: str, coupon_code: str | None, req_hash: str
) -> CheckoutResult:
    with db.session_factory() as s, s.begin():
        # Claim the key. A concurrent in-flight claimant makes this INSERT wait on the unique
        # index until it commits (-> DO NOTHING, replay path) or rolls back (-> we own it).
        claimed = s.execute(
            text(
                "INSERT INTO idempotency_keys (id, scope, key, request_hash) "
                "VALUES (:id, :scope, :key, :hash) "
                "ON CONFLICT (scope, key) DO NOTHING RETURNING id"
            ),
            {"id": uuid.uuid4(), "scope": SCOPE, "key": key, "hash": req_hash},
        ).first()
        if claimed is None:
            return _replay(s, key, req_hash)
        claim_id = claimed.id
        hooks.checkpoint("checkout.after_claim", cart_id=cart_id)

        # Row lock serialises checkouts of the same cart and cart mutations.
        lock_and_require_open(s, cart_id)

        # Ascending product id = global lock order.
        items = s.execute(
            text("SELECT product_id, quantity FROM cart_items WHERE cart_id = :c ORDER BY product_id"),
            {"c": cart_id},
        ).all()
        if not items:
            raise unprocessable("CART_EMPTY", "Cart has no items.", {"cart_id": str(cart_id)})

        # Price + stock in one atomic conditional UPDATE per product. RETURNING gives
        # the name/price of the very row we locked and decremented, so the snapshot
        # cannot disagree with the inventory we consumed.
        snapshots: list[tuple[int, str, int, int]] = []
        shortfalls: list[tuple[int, int]] = []
        for product_id, qty in items:
            row = s.execute(
                text(
                    "UPDATE products SET inventory = inventory - :q, updated_at = now() "
                    "WHERE id = :id AND inventory >= :q RETURNING name, price_cents"
                ),
                {"q": qty, "id": product_id},
            ).first()
            if row is None:
                shortfalls.append((product_id, qty))
            else:
                snapshots.append((product_id, row.name, row.price_cents, qty))
        if shortfalls:
            available = dict(
                s.execute(
                    text("SELECT id, inventory FROM products WHERE id = ANY(:ids)"),
                    {"ids": [p for p, _ in shortfalls]},
                ).all()
            )
            raise insufficient_stock([(p, q, available.get(p, 0)) for p, q in shortfalls])
        hooks.checkpoint("checkout.after_inventory", cart_id=cart_id)

        # Optional coupon: conditional AVAILABLE -> REDEEMED inside this transaction.
        claimed_coupon = claim_coupon(s, coupon_code) if coupon_code else None
        hooks.checkpoint("checkout.after_coupon", cart_id=cart_id)

        gross = sum(money.line_total(price, qty) for _, _, price, qty in snapshots)
        percent = claimed_coupon.discount_percent if claimed_coupon else None
        discount, net = money.compute_totals(gross, percent)

        order = Order(
            id=uuid.uuid4(),
            cart_id=cart_id,
            coupon_id=claimed_coupon.id if claimed_coupon else None,
            discount_percent=percent,
            gross_cents=gross,
            discount_cents=discount,
            net_cents=net,
            currency=money.CURRENCY,
        )
        s.add(order)
        s.flush()
        for product_id, name, price, qty in snapshots:
            s.add(
                OrderItem(
                    id=uuid.uuid4(),
                    order_id=order.id,
                    product_id=product_id,
                    product_name=name,
                    unit_price_cents=price,
                    quantity=qty,
                    line_total_cents=money.line_total(price, qty),
                )
            )
        s.flush()

        s.execute(
            text(
                "UPDATE carts SET status = 'CHECKED_OUT', checked_out_at = now(), updated_at = now() "
                "WHERE id = :c AND status = 'OPEN'"
            ),
            {"c": cart_id},
        )

        # Persist the idempotency result in the same transaction as the order.
        body = json.loads(build_order_view(s, order).model_dump_json())
        s.execute(
            text(
                "UPDATE idempotency_keys SET order_id = :o, response_status = 201, "
                "response_body = CAST(:b AS jsonb) WHERE id = :id"
            ),
            {"o": order.id, "b": json.dumps(body), "id": claim_id},
        )
        hooks.checkpoint("checkout.before_commit", cart_id=cart_id)
    return CheckoutResult(201, body, replayed=False)


def _replay(s: Session, key: str, req_hash: str) -> CheckoutResult:
    row = s.execute(
        text(
            "SELECT request_hash, response_status, response_body FROM idempotency_keys "
            "WHERE scope = :scope AND key = :key"
        ),
        {"scope": SCOPE, "key": key},
    ).first()
    if row is None:  # conflict implies a committed row, so this is only a safety net
        raise AppError(503, "TRANSIENT_CONFLICT", "Please retry.", headers={"Retry-After": "1"})
    if row.request_hash.strip() != req_hash:
        raise conflict(
            "IDEMPOTENCY_KEY_REUSED",
            "This Idempotency-Key was already used with a different request payload.",
        )
    return CheckoutResult(row.response_status, row.response_body, replayed=True)
