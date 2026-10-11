"""Synchronous endpoints with HTTP serialization and documented outcomes."""

import base64
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

from .schemas import (DayAheadReportRequest, DayAheadReportResponse, ErrorResponse,
                      HealthResponse, HighPriceRangeResponse)


router = APIRouter()


def _error_docs(statuses):
    descriptions = {
        422: "Invalid date, threshold, or request fields.",
        502: "Price provider unavailable, or invalid provider data.",
        503: "Prices not yet published, or database unavailable.",
        500: "Invalid server configuration or report processing failure.",
    }
    return {status: {"model": ErrorResponse, "description": descriptions[status],
                     "content": {"application/json": {"example": {
                         "status": "error", "error": descriptions[status],
                         "code": {422: "invalid_request", 502: "provider_unavailable",
                                  503: "prices_unavailable", 500: "internal_error"}[status],
                         "retryable": status in (502, 503),
                     }}}} for status in statuses}


@router.post("/api/v1/reports/day-ahead", response_model=DayAheadReportResponse,
             operation_id="prepare_day_ahead_report", tags=["reports"],
             summary="Prepare a stored day-ahead price report",
             description="Fetch missing prices, persist a complete day, and return analysis and a base64 PNG. "
                         "Existing days are reused. Warnings concern actual prices; smoothing is display-only. "
                         "Both success and warning reports return HTTP 200.",
             responses=_error_docs((422, 502, 503, 500)))
def day_ahead_report(payload: DayAheadReportRequest, request: Request):
    report = request.app.state.report_service(payload.date, payload.threshold_eur_per_mwh)
    zone = ZoneInfo(report.timezone)
    return DayAheadReportResponse(
        date=report.day, market=report.market, timezone=report.timezone,
        threshold_eur_per_mwh=str(report.threshold), fetched_at=report.fetched_at,
        status="warning" if report.high_price_ranges else "success",
        image=base64.b64encode(report.png).decode("ascii"),
        image_filename=f"day-ahead-{report.day}.png", warning=report.warning,
        high_price_ranges=[HighPriceRangeResponse(start_at=r.start_at.astimezone(zone),
                                                 end_at=r.end_at.astimezone(zone))
                           for r in report.high_price_ranges],
    )


@router.get("/health/live", response_model=HealthResponse, operation_id="check_liveness",
            tags=["health"], summary="Check that the HTTP process is alive")
def live():
    return HealthResponse()


@router.get("/health/ready", response_model=HealthResponse, operation_id="check_readiness",
            tags=["health"], summary="Check PostgreSQL and the initialized application schema",
            description="Checks database reads only; never calls ENTSO-E or FusionSolar.",
            responses=_error_docs((503, 500)))
def ready(request: Request):
    request.app.state.readiness_probe()
    return HealthResponse()
