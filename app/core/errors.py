import uuid

from sqlalchemy.exc import IntegrityError


class AppError(Exception):
    """A deliberate, client-facing error with a stable machine-readable code."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers or {}


def not_found(code: str, message: str, details: dict | None = None) -> AppError:
    return AppError(404, code, message, details)


def conflict(code: str, message: str, details: dict | None = None) -> AppError:
    return AppError(409, code, message, details)


def unprocessable(code: str, message: str, details: dict | None = None) -> AppError:
    return AppError(422, code, message, details)


def validation_error(field: str, message: str) -> AppError:
    issue = {"field": field, "message": message, "type": "value"}
    return unprocessable("VALIDATION_ERROR", message, {"issues": [issue]})


def cart_not_found(cart_id: uuid.UUID) -> AppError:
    return not_found("CART_NOT_FOUND", "Cart not found.", {"cart_id": str(cart_id)})


def cart_not_open(cart_id: uuid.UUID) -> AppError:
    return conflict(
        "CART_NOT_OPEN", "Cart has already been checked out.", {"cart_id": str(cart_id)}
    )


def item_not_in_cart(product_id: int) -> AppError:
    return not_found("ITEM_NOT_IN_CART", "Product is not in the cart.", {"product_id": product_id})


def coupon_already_redeemed() -> AppError:
    return conflict("COUPON_ALREADY_REDEEMED", "This coupon has already been redeemed.")


def insufficient_stock(shortfalls: list[tuple[int, int, int]]) -> AppError:
    """`shortfalls` is a list of (product_id, requested, available)."""
    return conflict(
        "INSUFFICIENT_STOCK",
        "Not enough stock available for one or more items.",
        {
            "items": [
                {"product_id": pid, "requested": requested, "available": available}
                for pid, requested, available in shortfalls
            ]
        },
    )


def constraint_name(exc: IntegrityError) -> str | None:
    return getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
