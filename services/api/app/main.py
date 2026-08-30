"""FastAPI application factory.

Binds to 127.0.0.1 only. Public traffic reaches this service through the Next.js
rewrite, which is itself behind the hosting panel's nginx. There is no path from the
internet to this port (SECURITY.md §2).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from thedrop_config import get_settings

from app import __version__
from app.logging_config import configure_logging, request_id_var
from app.routers import admin, health, public, worker

logger = logging.getLogger(__name__)

# Sent on every response. The CSP has no wildcard: ad and analytics origins are added
# explicitly, one at a time, when those features actually land.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info(
        "api starting",
        extra={"version": __version__, "environment": settings.environment.value},
    )
    yield
    logger.info("api stopping")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="THE DROP API",
        version=__version__,
        lifespan=lifespan,
        # No public API docs in production. They enumerate the admin surface.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "unhandled error",
                extra={"path": request.url.path, "method": request.method},
            )
            response = JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                # Never leak an exception message to a client; it may contain a
                # connection string or a fragment of untrusted source content.
                content={"detail": "Internal server error", "requestId": request_id},
            )
        finally:
            request_id_var.reset(token)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
            )

        logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    @app.exception_handler(OperationalError)
    async def database_unavailable_handler(request: Request, exc: OperationalError) -> JSONResponse:
        """A database outage is 503, not 500.

        500 says "this endpoint is broken" and invites someone to go debugging the
        route. 503 says "a dependency is down", which is both accurate and the signal
        a load balancer, an uptime monitor and the web app's fallback path all expect.

        The driver's message is logged but never returned: it contains the connection
        string, including the user and host.
        """
        logger.error(
            "database unavailable",
            extra={"path": request.url.path, "method": request.method, "error": str(exc.orig)},
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": "Database unavailable",
                "hint": "Is PostgreSQL running? See docs/DEPLOYMENT.md §4.",
                "requestId": getattr(request.state, "request_id", None),
            },
            headers={"Retry-After": "30"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Echo the field errors but not the submitted values -- input may be sensitive.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": "Validation failed",
                "errors": [
                    {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                    for e in exc.errors()
                ],
                "requestId": getattr(request.state, "request_id", None),
            },
        )

    app.include_router(health.router)
    app.include_router(public.router)
    app.include_router(admin.router)
    app.include_router(worker.router)

    return app


app = create_app()
