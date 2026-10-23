# DECISIONS

Scope: backend only (no frontend). Stack: Python 3.12, FastAPI, PostgreSQL 16, SQLAlchemy 2, Alembic, Pytest.
Money is integer cents (USD). Setup, API reference and commands are in [`SETUP.md`](SETUP.md).

## 1. System invariants and where they are enforced

| # | Invariant | Application enforcement | Database safeguard | Proving test(s) |
|---|---|---|---|---|
| I1 | Inventory never negative / never oversold | Conditional `UPDATE products SET inventory = inventory - :q WHERE id = :id AND inventory >= :q`, row count checked (`checkout._checkout_once`) | `CHECK (inventory >= 0)` | `test_two_buyers_compete_for_the_final_unit`, `test_many_buyers_never_oversell`, `test_inventory_can_never_go_negative` |
| I2 | A cart produces at most one order | `SELECT … FOR UPDATE` on the cart row + status check | `UNIQUE (orders.cart_id)` | `test_two_simultaneous_checkouts_of_the_same_cart`, `test_one_order_per_cart` |
| I3 | Order keeps immutable product/price/quantity/financial snapshot | Name and price are read from the same row that was locked and decremented (`UPDATE … RETURNING`) and copied into `order_items`; no update endpoint exists | CHECKs: `line_total = unit_price * qty`, `net = gross - discount`, `0 <= discount <= gross` | `test_order_snapshot_survives_product_changes`, `test_order_item_line_total_must_equal_price_times_quantity` |
| I4 | Retry of a successful checkout (same key) does not create another order or consume stock | Idempotency row claimed in the checkout transaction; response stored and replayed | `UNIQUE (scope, key)` | `test_same_key_same_payload_returns_original_without_side_effects`, `test_simultaneous_duplicate_requests_with_one_key` |
| I5 | Same key + different normalised payload is rejected (409) | SHA-256 of `{cart_id, normalised coupon_code}` compared on replay | stored `request_hash` | `test_same_key_different_payload_is_rejected`, `test_simultaneous_same_key_different_payload` |
| I6 | A coupon is redeemed at most once | Conditional `UPDATE … SET status='REDEEMED' WHERE code=:c AND status='AVAILABLE'` | `UNIQUE (orders.coupon_id)`, `CHECK (status/redeemed_at agree)` | `test_two_checkouts_cannot_redeem_one_coupon`, `test_claim_coupon_conditional_update_alone_blocks_second_claim`, `test_a_coupon_can_back_at_most_one_order` |
| I7 | A coupon is not consumed by a checkout that rolls back | The redemption runs inside the checkout transaction and never commits on its own | transaction atomicity | `test_coupon_not_consumed_by_checkout_that_rolls_back`, `test_coupon_not_consumed_when_stock_insufficient` |
| I8 | Order, inventory, coupon, cart transition and idempotency response commit atomically | A single `session.begin()` block; no intermediate commits | PostgreSQL transaction | `test_failure_after_inventory_decrement_rolls_back_everything`, `test_failure_just_before_commit_rolls_back_everything` |
| I9 | Failed checkouts do not count toward milestones | Eligibility uses `COUNT(*)` of `orders`; failures insert no row | n/a (rows only exist for success) | `test_failed_checkouts_do_not_advance_milestone` |
| I10 | A milestone never gets two coupons, even under concurrent admin requests | `pg_advisory_xact_lock` around "count → pick lowest un-rewarded milestone → insert" | `UNIQUE (coupons.milestone_order_count)` | `test_concurrent_generation_yields_exactly_one_coupon_per_milestone`, `test_concurrent_generation_with_backlog_never_duplicates`, `test_generation_serialises_on_advisory_lock`, `test_one_coupon_per_milestone_and_unique_codes` |
| I11 | Money is exact and deterministic | integer arithmetic only (`services/money.py`) | `BIGINT` columns + CHECKs | `test_discount_rounding_half_up`, `test_totals_never_negative`, `test_full_discount_never_negative` |
| I12 | Reporting is read-only and reconciles | One `REPEATABLE READ READ ONLY` transaction | PostgreSQL rejects writes (SQLSTATE 25006) | `test_report_reconciles_with_orders_and_coupons`, `test_repeated_reports_do_not_mutate_state`, `test_report_transaction_is_read_only_in_postgres`, `test_report_is_a_consistent_snapshot_under_concurrent_checkouts` |
| I13 | Cart mutations cannot race with checkout | Cart row `FOR UPDATE` in every mutation and in checkout | row lock | `test_cart_mutation_racing_checkout_sees_closed_cart`, `test_concurrent_adds_of_same_product_yield_one_row` |

Known gap: `orders.gross_cents = SUM(order_items.line_total_cents)` spans two tables, so it cannot be a CHECK. It is enforced by the service, and the report test asserts it against the API's own orders.

## 2. Ambiguities and selected semantics

| Question | Selected behaviour |
|---|---|
| Who owns a coupon? | **Bearer coupon**: whoever presents the code can use it (the brief has no customer concept). |
| Can several coupons be used in one checkout? | **No**, at most one `coupon_code` per checkout. |
| Coupon expiry / older unused coupons | **No expiry**; an unused coupon stays valid indefinitely and is independent of later milestones. |
| Discount scope and rounding | Whole-order percentage on the gross amount; one rounding, **half-up** to the cent: `(gross*pct + 50) // 100`. |
| Several milestones reached before an admin acts | **One coupon per admin request**, lowest un-rewarded milestone first. |
| Changing `n` or `x` later | Milestones are **persisted absolute order counts** (`coupons.milestone_order_count`, UNIQUE), not a mutable counter. `x` is snapshotted on each coupon. A new `n` applies to future eligibility only and never voids or duplicates existing coupons (`test_config_change_does_not_void_or_duplicate_existing_milestones`). Approved with clarification. |
| Can the order that reaches a milestone use the coupon it triggers? | No: the coupon only exists after an admin generates it, which requires the Nth order to be committed. |
| 100% coupon | Allowed. Net is 0, never negative. |
| Adding a product already in the cart | `POST` returns 409 `ITEM_ALREADY_IN_CART`; `PUT` sets the quantity. |
| Stock checked when adding to a cart? | Yes, as validation only (`INSUFFICIENT_STOCK` if quantity > current stock). Nothing is reserved. Checkout re-checks atomically. |
| Price or availability changes after adding | Carts display **current** prices. Checkout charges current prices and snapshots them. Availability is re-validated at checkout; a shortfall returns 409 `INSUFFICIENT_STOCK` with per-item details. |
| Payment | **No payment abstraction.** Reaching commit is treated as payment success. A fake gateway would add code without protecting any invariant, and a real one must not be called while locks are held. |

## 3. Decisions

### Decision: PostgreSQL as the authoritative store
**Context:** Overlapping checkouts need row locks, atomic conditional updates, unique constraints and real transactions.
**Options considered:** In-memory with locks; SQLite; PostgreSQL.
**Choice:** PostgreSQL 16 (pinned image `postgres:16.4-alpine`), Alembic migrations written as explicit SQL.
**Why:** In-memory and SQLite cannot show multi-connection behaviour. Constraints act as the final safeguard if application code is wrong.
**Consequences:** Docker is required to run the tests. Tests never use SQLite.

### Decision: One READ COMMITTED transaction per use case, owned by the service
**Context:** Checkout touches idempotency, cart, products, coupon, order and items, which must succeed or fail together.
**Options considered:** Per-step commits with compensation; `SERIALIZABLE` with retry; one `READ COMMITTED` transaction with atomic updates and explicit locks.
**Choice:** The last. Routes never open or commit transactions; helpers never commit. Reporting uses `REPEATABLE READ READ ONLY`.
**Why:** Every conflict point is known and protected directly. The idempotency re-read after `ON CONFLICT DO NOTHING` depends on READ COMMITTED visibility of the winner's commit.
**Consequences:** The lock order must be preserved: idempotency row → cart → products ascending → coupon. Deadlock (`40P01`), lock-timeout (`55P03`), serialization (`40001`) and statement-timeout (`57014`) errors roll the whole transaction back and the *entire* use case is retried at most 3 times with jittered back-off, then 503 `TRANSIENT_CONFLICT` with `Retry-After`. Retrying is safe because nothing was committed and there are no external side effects.

### Decision: Idempotency
**Context:** Clients retry after timeouts.
**Options considered:** In-memory map; SELECT-then-INSERT; unique claim row inside the checkout transaction; also caching failures.
**Choice:** `idempotency_keys` with `UNIQUE (scope, key)`, claimed by `INSERT … ON CONFLICT DO NOTHING RETURNING id` inside the checkout transaction and completed at the end. Scope is `checkout`. Only successful outcomes are persisted.
**Why:** A concurrent duplicate blocks on the unique index until the first transaction finishes: if it commits, the duplicate replays the stored response; if it rolls back, the duplicate owns the key. A crash after commit but before the response is delivered is covered because the stored response is part of the same commit. A rolled-back checkout never leaves a permanently claimed key (`test_failed_checkout_leaves_key_reusable`).
**Consequences:** Failed checkouts are re-executed on retry (safe, they changed nothing). **Unresolved: retention.** Keys are kept forever in this submission. Production needs a TTL (at least 24h) plus cleanup. After expiry, double orders are still prevented by `UNIQUE (orders.cart_id)` and the cart status, but the client would see 409 `CART_NOT_OPEN` instead of the original body.

### Decision: Inventory concurrency
**Context:** Two buyers may want the last unit.
**Options considered:** Read-then-write; `SELECT … FOR UPDATE` on products; conditional `UPDATE` with row-count check.
**Choice:** Conditional atomic `UPDATE … RETURNING name, price_cents`, in ascending `product_id` order, plus `CHECK (inventory >= 0)`. All shortfalls are collected so the 409 lists every unavailable item.
**Why:** No lost update, no separate lock step, and the price snapshot comes from the row that was actually decremented.
**Consequences:** No reservation at add-to-cart time; a cart can look valid and still fail at checkout. Hot products serialise on their row lock for the duration of a checkout transaction.

### Decision: Coupon concurrency and milestone progression
**Context:** Races exist both on redemption and on generation.
**Options considered:** Lock the coupon then update; conditional update; a counter table incremented per order.
**Choice:** Redemption is a conditional `AVAILABLE → REDEEMED` update with `UNIQUE (orders.coupon_id)` as backstop. Generation takes a transaction-scoped advisory lock (held in PostgreSQL, so it is shared by every app instance), derives eligibility from `COUNT(orders)`, and relies on `UNIQUE (milestone_order_count)` as backstop.
**Why:** There is no counter to drift or become a hot lock. A failed checkout creates no order, so it cannot advance a milestone.
**Consequences:** One coupon per admin call. The loser of a generation race re-evaluates after the lock is released and gets either the next milestone or `NO_ELIGIBLE_MILESTONE`.

### Decision: Monetary precision and rounding
**Context:** No floating point; discounts must be deterministic and non-negative.
**Options considered:** `float`; `NUMERIC(12,2)` with `Decimal`; integer cents.
**Choice:** `BIGINT` cents, USD only (`CHECK currency = 'USD'`). Discount = `(gross*pct + 50) // 100`, rounded once on the whole order. `net = gross - discount`.
**Why:** Integer arithmetic is exact and identical in Python and in the CHECK constraints. Since `pct <= 100`, `0 <= discount <= gross`.
**Consequences:** No per-line discount allocation. Multi-currency and tax are out of scope.

### Decision: Price changes after cart creation
**Choice:** Carts always show current prices; checkout uses current prices and snapshots them in the order.
**Why:** Simple and deterministic; matches the recommended default.
**Consequences:** The customer may pay a different total than the last cart view. An optional `expected_total` precondition is deferred.

### Decision: No Redis
**Why:** PostgreSQL already holds the authoritative state and offers locks, unique constraints and transactions. A second store would add dual-write risk and no requirement needs a cache or queue.

### Decision: API error contract
**Choice:** Every non-2xx response is `{"error":{"code","message","details","request_id"}}`. Codes are stable and machine-readable. A resource named in the path that is missing gives 404; one named in the body gives 422; state conflicts give 409; malformed idempotency header gives 400. Unexpected errors give a generic 500 `INTERNAL_ERROR`; SQL text and stack traces are logged server-side only (`test_failure_after_inventory_decrement_rolls_back_everything` asserts nothing leaks).
**Consequences:** Clients branch on `code`; `details` is code-specific.

### Decision: Test seams
Production code calls `hooks.checkpoint(name)` at a few points inside the checkout and generation transactions. It is a no-op unless a test registers a hook, and it lets tests pause a transaction at an exact point or inject a failure. The trade-off is a small amount of test-only surface in production code, which was preferred to sleeps or monkeypatching internals.

## 4. Money and rounding
Integer cents, USD. Discount `(gross*pct + 50) // 100` (half-up), applied once to the order gross. Examples: 1005 @10% → 101, 5 @10% → 1, 1 @10% → 0. 100% coupon → net 0. Unit tests cover boundaries.

## 5. Error model
See the decision above and the table in [`SETUP.md`](SETUP.md). Distinguished conditions: validation, missing resources, insufficient stock, closed cart, coupon conflicts, idempotency conflicts, transient contention (503), pool exhaustion (503 `SERVICE_BUSY`), unexpected failure (500).

## 6. Implemented versus deferred

**Implemented:** carts and items, checkout with idempotency, coupons (generate/redeem), report, order and coupon lists, migrations and seed, Docker Compose with health checks, structured JSON logs with request IDs, readiness probe, bounded pool and lock/statement timeouts, graceful pool disposal on shutdown, real-PostgreSQL tests (see SETUP.md for the current count).

**Deferred (honestly not done):**
* Authentication and authorisation. `/admin/*` is **unprotected** and must be gated before any real deployment.
* Idempotency retention/cleanup; coupon expiry, ownership and abuse limits; refunds and cancellations (which would affect milestone counting).
* Real payments (would need pending/authorised states and an outbox, with no network call under lock).
* Inventory reservation, rate limiting, metrics, tracing, DB-trigger immutability of orders (explicitly deferred by decision U6; immutability relies on there being no update paths plus tests).
* Frontend.
* Git history: this folder is not currently a git repository, so no commits exist yet.

## 7. Evolution to multiple instances and scale
The service is stateless, and every lock or uniqueness guarantee lives in PostgreSQL (row locks, unique indexes, advisory lock), so running several API instances needs no code change. Limits: single primary, hot-product row contention, and the (admin-only, low-frequency) advisory lock. Next steps would be PgBouncer, idempotency TTL job, reporting from a replica or materialised view, a payment saga with an outbox, and per-product stock sharding only if contention is measured.

## 8. What I would examine next with two more hours
1. A test that provokes a *genuine* PostgreSQL deadlock. The retry/503 policy is tested (`test_transient_deadlock_is_retried_and_succeeds`, `test_persistent_deadlock_returns_503_and_leaves_no_state`) by injecting SQLSTATE `40P01` inside the transaction, so the real lock-ordering protection is still reasoned about rather than proven.
2. Idempotency TTL/cleanup and the behaviour after expiry.
3. Auth for `/admin/*` and audit logging.
4. Load test of hot-product contention and pool sizing with sync handlers.
5. Review whether add-to-cart stock validation should be advisory only.
6. A DB-level guard (or deferred trigger) for `gross = SUM(line_total)`.
