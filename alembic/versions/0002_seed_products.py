"""Seed products (deterministic IDs, repeatable, never resets inventory).

ON CONFLICT (id) DO NOTHING means re-running this migration, or re-seeding after
restarts, cannot overwrite inventory/prices that have changed since.

Revision ID: 0002_seed_products
Revises: 0001_initial_schema
"""

from alembic import op

revision = "0002_seed_products"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None

SEED = [
    (1, "Wireless Mouse", 2499, 50),
    (2, "Mechanical Keyboard", 8999, 25),
    (3, "USB-C Hub", 3999, 40),
    (4, "27-inch Monitor", 18999, 10),
    (5, "Noise-Cancelling Headphones", 12999, 15),
    (6, "Limited Edition Desk Mat", 4999, 3),  # limited inventory
]


def upgrade() -> None:
    for pid, name, price, inv in SEED:
        op.execute(
            "INSERT INTO products (id, name, price_cents, inventory) "
            f"VALUES ({pid}, '{name}', {price}, {inv}) ON CONFLICT (id) DO NOTHING"
        )


def downgrade() -> None:
    ids = ",".join(str(s[0]) for s in SEED)
    op.execute(f"DELETE FROM products WHERE id IN ({ids})")
