# Checkout and Rewards API: setup, usage and API reference

Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2 · Alembic · Pytest + HTTPX · Docker Compose.
Design rationale is in [`DECISIONS.md`](DECISIONS.md).

## Prerequisites
* Docker + Docker Compose v2
* Python 3.12 (only needed to run the tests / dev server on the host)

## Quick start
```bash
make up                      # copies .env.example -> .env, builds, starts db + api (migrates + seeds)
curl localhost:8000/health/ready
open http://localhost:8000/docs     # interactive OpenAPI
```
Equivalent without `make`: `cp .env.example .env && docker compose up -d --build`.

## UI (POC)

```bash
cd ui && npm install --no-audit && npm run build
npm run dev
```

## Configuration (`.env`, see `.env.example`)
| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | local compose DB | SQLAlchemy URL (`postgresql+psycopg://…`). Inside compose the host is `db`. |
| `REWARD_EVERY_N_ORDERS` | 5 | `n`: every nth successful order makes one coupon eligible |
| `REWARD_DISCOUNT_PERCENT` | 10 | `x`: percent off, snapshotted on each coupon at generation |
| `MAX_LINE_QUANTITY` / `MAX_CART_ITEMS` | 100 / 50 | per-line quantity cap (≤100, mirrored by a DB CHECK) / distinct products per cart |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_SECONDS` | 10 / 10 / 10 | connection pool |
| `DB_LOCK_TIMEOUT_MS`, `DB_STATEMENT_TIMEOUT_MS` | 5000 / 15000 | bound lock waits and statements |
| `LOG_LEVEL` | INFO | JSON logs on stdout |

## Migrations and seed data
```bash
make migrate                 # alembic upgrade head (host)  – the container also does this on start
```
`0001` creates the schema; `0002` seeds 6 products (product 6 is a limited-edition item with 3 units).
The seed uses `INSERT … ON CONFLICT (id) DO NOTHING`, so restarts never reset inventory.

## Tests
```bash
make venv                    # once
make test                    # starts the db container, then runs pytest
```
Tests use the **real PostgreSQL** `checkout_test` database (created automatically, schema rebuilt by
Alembic, data truncated per test). Concurrency tests run genuinely overlapping requests on separate
connections; coordination is done with `threading` primitives and by observing `pg_stat_activity`
(a request is *seen blocked on a lock in PostgreSQL* before the holder is released) – no `sleep`-based
ordering. Current result: **75 passed** (`.venv/bin/pytest -q`).

## Reset local data
```bash
make reset                   # docker compose down -v && up  (drops the volume)
```

---
## API reference
All bodies are JSON. Money is **integer cents, USD**. Every error uses one envelope:
```json
{"error":{"code":"INSUFFICIENT_STOCK","message":"…","details":{},"request_id":"…"}}
```
`X-Request-ID` is accepted/echoed and appears in logs and in every error body.
**Admin endpoints (`/admin/*`) are privileged but intentionally unauthenticated in this assignment.**

| Method & path | Body | Success | Error codes |
|---|---|---|---|
| `GET /products` | – | 200 list | – |
| `POST /carts` | – | 201 Cart | – |
| `GET /carts/{id}` | – | 200 Cart | 404 `CART_NOT_FOUND` |
| `POST /carts/{id}/items` | `{product_id, quantity}` | 201 Cart | 404 `CART_NOT_FOUND`; 422 `VALIDATION_ERROR`, `PRODUCT_NOT_FOUND`, `CART_FULL`; 409 `CART_NOT_OPEN`, `ITEM_ALREADY_IN_CART`, `INSUFFICIENT_STOCK` |
| `PUT /carts/{id}/items/{product_id}` | `{quantity}` | 200 Cart | 404 `CART_NOT_FOUND`, `ITEM_NOT_IN_CART`; 422 `VALIDATION_ERROR`; 409 `CART_NOT_OPEN`, `INSUFFICIENT_STOCK` |
| `DELETE /carts/{id}/items/{product_id}` | – | 200 Cart | 404 `CART_NOT_FOUND`, `ITEM_NOT_IN_CART`; 409 `CART_NOT_OPEN` |
| `POST /carts/{id}/checkout` | header `Idempotency-Key` (required); `{coupon_code?}` | 201 Order (replay: same body + `Idempotent-Replayed: true`) | 400 `IDEMPOTENCY_KEY_REQUIRED`/`_INVALID`; 404 `CART_NOT_FOUND`; 409 `IDEMPOTENCY_KEY_REUSED`, `CART_NOT_OPEN`, `INSUFFICIENT_STOCK`, `COUPON_ALREADY_REDEEMED`; 422 `CART_EMPTY`, `COUPON_NOT_FOUND`, `VALIDATION_ERROR`; 503 `TRANSIENT_CONFLICT` (+`Retry-After`) |
| `GET /orders/{id}` | – | 200 Order | 404 `ORDER_NOT_FOUND` |
| **ADMIN** `POST /admin/coupons` | – | 201 Coupon | 409 `NO_ELIGIBLE_MILESTONE` |
| **ADMIN** `GET /admin/coupons` | – | 200 list | – |
| **ADMIN** `GET /admin/orders?limit&offset` | – | 200 list | 422 |
| **ADMIN** `GET /admin/reports/summary` | – | 200 Report | – |
| `GET /health/live`, `/health/ready` | – | 200 | ready: 503 if DB unreachable |

Unexpected failures return `500 INTERNAL_ERROR` with no SQL/stack trace; pool exhaustion returns `503 SERVICE_BUSY`.

### Example session
```bash
B=localhost:8000
CART=$(curl -s -X POST $B/carts | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -s -X POST $B/carts/$CART/items -H 'content-type: application/json' -d '{"product_id":1,"quantity":2}'
curl -s -X POST $B/carts/$CART/checkout -H 'content-type: application/json' \
     -H 'Idempotency-Key: order-0001' -d '{}'                       # 201
curl -s -i -X POST $B/carts/$CART/checkout -H 'content-type: application/json' \
     -H 'Idempotency-Key: order-0001' -d '{}'                       # 201 + Idempotent-Replayed: true
curl -s -X POST $B/admin/coupons                                    # 409 until the nth order exists
curl -s $B/admin/reports/summary
```
Redeem: `-d '{"coupon_code":"RWD-XXXXXXXXXXXX"}'` (case-insensitive).

## Time spent
To be stated by the submitter (the brief asks for an approximate figure). AI usage is described in `DECISIONS.md`.
