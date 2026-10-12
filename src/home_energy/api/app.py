"""App factory and lifespan wiring; constructing the app performs no I/O."""

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from time import perf_counter

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool

from ..database import initialize_schema
from ..database.connection import connect
from ..prices.report import DayAheadReportService, PriceReport
from ..usage.report import EVUsageReport, EVUsageReportService
from .errors import error_response, register_error_handlers
from .routes import router


logger = logging.getLogger("uvicorn.error")


def check_database() -> None:
    with connect() as connection:
        connection.execute("SET LOCAL statement_timeout = '3s'")
        connection.execute(
            "SELECT 1 FROM home_energy.energy_balance_days, home_energy.measurement_samples, "
            "home_energy.daily_metrics, home_energy.day_ahead_price_days, home_energy.price_intervals LIMIT 0"
        )


def create_app(*, report_service: Callable[[date, Decimal], PriceReport] | None = None,
               ev_report_service: Callable[[date | None], EVUsageReport] | None = None,
               readiness_probe: Callable[[], None] | None = None,
               schema_initializer: Callable[[], None] | None = None) -> FastAPI:
    initializer = schema_initializer if schema_initializer is not None else initialize_schema

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await run_in_threadpool(initializer)
        except Exception as exc:
            response = error_response(exc)
            raise RuntimeError(f"Home Energy startup failed (HTTP {response.status_code}). Check server settings.") from None
        yield

    app = FastAPI(title="Home Energy API", version="0.2.0", lifespan=lifespan,
                  description="Daily consumption, EV blocks, and Belgian day-ahead price reports. "
                              "Dates use Europe/Brussels; power uses kW and energy uses kWh.",
                  openapi_tags=[{"name": "reports", "description": "Prepare completed energy reports."},
                                {"name": "health", "description": "Process and database health."}])
    app.state.report_service = report_service if report_service is not None else DayAheadReportService()
    app.state.ev_report_service = ev_report_service if ev_report_service is not None else EVUsageReportService()
    app.state.readiness_probe = readiness_probe if readiness_probe is not None else check_database
    register_error_handlers(app)
    app.include_router(router)

    @app.middleware("http")
    async def request_outcome(request: Request, call_next):
        started = perf_counter()
        status = 500
        try:
            try:
                response = await call_next(request)
            except Exception as exc:
                # Catch unknown failures here so Uvicorn never logs raw exception chains.
                response = error_response(exc)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            logger.info("Home Energy request method=%s path=%s status=%s duration_ms=%.1f",
                        request.method, getattr(route, "path", "<unmatched>"), status,
                        (perf_counter() - started) * 1000)

    return app
