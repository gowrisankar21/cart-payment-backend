import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.api import admin, carts, health, orders, products
from app.core.config import Settings, get_default_settings
from app.core.db import Database
from app.core.errors import AppError
from app.core.logging import configure_logging, request_id_ctx

log = logging.getLogger("app.http")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


def error_body(code: str, message: str, details: dict | None = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id_ctx.get(),
        }
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_default_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Graceful shutdown: in-flight requests finish (uvicorn), then the pool closes.
        yield
        app.state.db.dispose()

    app = FastAPI(
        title="Checkout and Rewards API",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = Database(settings)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        token = request_id_ctx.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # last-resort: never leak internals
            log.exception("unhandled_exception")
            response = JSONResponse(
                status_code=500,
                content=error_body("INTERNAL_ERROR", "An unexpected error occurred."),
            )
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        response.headers["X-Request-ID"] = rid
        log.info(
            "request",
            extra={
                "extra_fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": elapsed_ms,
                }
            },
        )
        request_id_ctx.reset(token)
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        issues = [
            {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"], "type": e["type"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_body("VALIDATION_ERROR", "Request validation failed.", {"issues": issues}),
        )

    @app.exception_handler(PoolTimeoutError)
    async def pool_timeout_handler(request: Request, exc: PoolTimeoutError):
        log.error("db_pool_timeout")
        return JSONResponse(
            status_code=503,
            content=error_body("SERVICE_BUSY", "The service is busy. Please retry shortly."),
            headers={"Retry-After": "1"},
        )

    app.include_router(health.router)
    app.include_router(products.router)
    app.include_router(carts.router)
    app.include_router(orders.router)
    app.include_router(admin.router)
    return app


app = create_app()  # uvicorn app.main:app
