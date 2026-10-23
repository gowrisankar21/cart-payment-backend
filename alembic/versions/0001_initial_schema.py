"""Initial schema: products, carts, cart_items, coupons, orders, order_items, idempotency_keys.

Revision ID: 0001_initial_schema
Revises:
"""

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = [
    # ---------------------------------------------------------------- products
    """
    CREATE TABLE products (
        id          BIGINT PRIMARY KEY,
        name        TEXT NOT NULL,
        price_cents BIGINT NOT NULL,
        inventory   INTEGER NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_products_name_not_blank CHECK (length(btrim(name)) > 0),
        CONSTRAINT ck_products_price_positive CHECK (price_cents > 0),
        CONSTRAINT ck_products_inventory_non_negative CHECK (inventory >= 0)
    )
    """,
    # ------------------------------------------------------------------- carts
    """
    CREATE TABLE carts (
        id             UUID PRIMARY KEY,
        status         VARCHAR(16) NOT NULL DEFAULT 'OPEN',
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        checked_out_at TIMESTAMPTZ,
        CONSTRAINT ck_carts_status CHECK (status IN ('OPEN','CHECKED_OUT')),
        CONSTRAINT ck_carts_checked_out_at_matches_status
            CHECK ((status = 'CHECKED_OUT') = (checked_out_at IS NOT NULL))
    )
    """,
    # -------------------------------------------------------------- cart_items
    """
    CREATE TABLE cart_items (
        cart_id    UUID   NOT NULL REFERENCES carts(id),
        product_id BIGINT NOT NULL REFERENCES products(id),
        quantity   INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (cart_id, product_id),
        CONSTRAINT ck_cart_items_quantity_range CHECK (quantity BETWEEN 1 AND 100)
    )
    """,
    # ----------------------------------------------------------------- coupons
    # milestone_order_count stores the persisted absolute threshold (n, 2n, 3n ...)
    # that triggered the coupon; UNIQUE guarantees one coupon per milestone.
    """
    CREATE TABLE coupons (
        id                    UUID PRIMARY KEY,
        code                  TEXT NOT NULL,
        milestone_order_count INTEGER NOT NULL,
        discount_percent      SMALLINT NOT NULL,
        status                VARCHAR(16) NOT NULL DEFAULT 'AVAILABLE',
        generated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        redeemed_at           TIMESTAMPTZ,
        CONSTRAINT uq_coupons_code UNIQUE (code),
        CONSTRAINT uq_coupons_milestone_order_count UNIQUE (milestone_order_count),
        CONSTRAINT ck_coupons_milestone_positive CHECK (milestone_order_count > 0),
        CONSTRAINT ck_coupons_percent_range CHECK (discount_percent BETWEEN 1 AND 100),
        CONSTRAINT ck_coupons_status CHECK (status IN ('AVAILABLE','REDEEMED')),
        CONSTRAINT ck_coupons_redeemed_at_matches_status
            CHECK ((status = 'REDEEMED') = (redeemed_at IS NOT NULL))
    )
    """,
    # ------------------------------------------------------------------ orders
    # Only successful orders exist, so COUNT(*) is the milestone counter.
    # UNIQUE(cart_id): a cart yields at most one order.
    # UNIQUE(coupon_id): a coupon is redeemed by at most one order (NULLs allowed repeatedly).
    """
    CREATE TABLE orders (
        id               UUID PRIMARY KEY,
        cart_id          UUID   NOT NULL REFERENCES carts(id),
        coupon_id        UUID   REFERENCES coupons(id),
        discount_percent SMALLINT,
        gross_cents      BIGINT NOT NULL,
        discount_cents   BIGINT NOT NULL,
        net_cents        BIGINT NOT NULL,
        currency         VARCHAR(3) NOT NULL DEFAULT 'USD',
        placed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_orders_cart_id UNIQUE (cart_id),
        CONSTRAINT uq_orders_coupon_id UNIQUE (coupon_id),
        CONSTRAINT ck_orders_gross_positive CHECK (gross_cents > 0),
        CONSTRAINT ck_orders_discount_range
            CHECK (discount_cents >= 0 AND discount_cents <= gross_cents),
        CONSTRAINT ck_orders_net_formula CHECK (net_cents = gross_cents - discount_cents),
        CONSTRAINT ck_orders_currency CHECK (currency = 'USD'),
        CONSTRAINT ck_orders_coupon_percent_pair
            CHECK ((coupon_id IS NULL) = (discount_percent IS NULL)),
        CONSTRAINT ck_orders_discount_requires_coupon
            CHECK (coupon_id IS NOT NULL OR discount_cents = 0),
        CONSTRAINT ck_orders_percent_range
            CHECK (discount_percent IS NULL OR discount_percent BETWEEN 1 AND 100)
    )
    """,
    "CREATE INDEX ix_orders_placed_at ON orders (placed_at, id)",
    # ------------------------------------------------------------- order_items
    """
    CREATE TABLE order_items (
        id               UUID PRIMARY KEY,
        order_id         UUID   NOT NULL REFERENCES orders(id),
        product_id       BIGINT NOT NULL REFERENCES products(id),
        product_name     TEXT   NOT NULL,
        unit_price_cents BIGINT NOT NULL,
        quantity         INTEGER NOT NULL,
        line_total_cents BIGINT NOT NULL,
        CONSTRAINT uq_order_items_order_product UNIQUE (order_id, product_id),
        CONSTRAINT ck_order_items_quantity_positive CHECK (quantity > 0),
        CONSTRAINT ck_order_items_price_positive CHECK (unit_price_cents > 0),
        CONSTRAINT ck_order_items_line_total
            CHECK (line_total_cents = unit_price_cents * quantity)
    )
    """,
    "CREATE INDEX ix_order_items_product_id ON order_items (product_id)",
    # ---------------------------------------------------------- idempotency_keys
    # Row is inserted (claimed) at the start of the checkout transaction and completed
    # at its end; claim and result commit atomically, so a committed row is always complete.
    """
    CREATE TABLE idempotency_keys (
        id              UUID PRIMARY KEY,
        scope           VARCHAR(32)  NOT NULL,
        key             VARCHAR(255) NOT NULL,
        request_hash    CHAR(64)     NOT NULL,
        order_id        UUID REFERENCES orders(id),
        response_status SMALLINT,
        response_body   JSONB,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_idempotency_scope_key UNIQUE (scope, key),
        CONSTRAINT uq_idempotency_order_id UNIQUE (order_id),
        CONSTRAINT ck_idempotency_result_complete CHECK (
            (response_status IS NULL) = (response_body IS NULL)
            AND (response_status IS NULL) = (order_id IS NULL)
        )
    )
    """,
]


def upgrade() -> None:
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for table in (
        "idempotency_keys",
        "order_items",
        "orders",
        "coupons",
        "cart_items",
        "carts",
        "products",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
