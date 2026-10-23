"""Exact integer-cent arithmetic (USD). No floats anywhere.

Rules (see DECISIONS.md):
* The discount is a whole-order percentage applied to the gross amount.
* Rounding happens exactly once, on the discount, half-up to the nearest cent.
* net = gross - discount, and since percent <= 100, 0 <= discount <= gross always holds.
"""

CURRENCY = "USD"


def line_total(unit_price_cents: int, quantity: int) -> int:
    return unit_price_cents * quantity


def compute_discount(gross_cents: int, percent: int) -> int:
    """Half-up rounded discount in cents. Pure integer maths: (gross*pct + 50) // 100."""
    if gross_cents < 0:
        raise ValueError("gross_cents must be non-negative")
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return (gross_cents * percent + 50) // 100


def compute_totals(gross_cents: int, percent: int | None) -> tuple[int, int]:
    """Return (discount_cents, net_cents)."""
    discount = compute_discount(gross_cents, percent) if percent else 0
    return discount, gross_cents - discount
