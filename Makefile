.PHONY: venv up down migrate test reset logs run

PY := .venv/bin

venv:
	python3.12 -m venv .venv && $(PY)/pip install -q -r requirements-dev.txt

# Start PostgreSQL + API (the API container applies migrations on start)
up:
	cp -n .env.example .env || true
	docker compose up -d --build

down:
	docker compose down

# Apply migrations (and the repeatable seed) from the host against $$DATABASE_URL
migrate:
	$(PY)/alembic upgrade head

# Run the API on the host with auto-reload (requires `docker compose up -d db`)
run:
	$(PY)/uvicorn app.main:app --reload --port 8000

# Real-PostgreSQL test suite (creates/uses the checkout_test database on the compose DB)
test:
	docker compose up -d db --wait
	$(PY)/pytest -q

# Destroy ALL local data (including the volume) and start fresh
reset:
	docker compose down -v
	docker compose up -d --build

logs:
	docker compose logs -f api
