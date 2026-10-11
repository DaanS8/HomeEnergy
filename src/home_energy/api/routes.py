"""Synchronous endpoints with HTTP serialization and documented outcomes."""

import base64
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

from .schemas import (DayAheadReportRequest, DayAheadReportResponse, ErrorResponse,
                      HealthResponse, HighPriceRangeResponse, DailyUsageRequest, DailyUsageResponse,
                      ConsumptionIntervalResponse, EVBlockResponse)


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


@router.post("/api/v1/usage/daily", response_model=DailyUsageResponse,
             operation_id="ingest_daily_usage", tags=["usage"], summary="Ingest consumption and return daily EV blocks",
             description="Reuse stored past days, fetch missing FusionSolar data, and return consumption and EV blocks. "
                         "Omit date for today. Today is fetched fresh and never stored as a completed day. "
                         "EV power is the mean of accepted clean-stop drops, rounded to 0.001 kW. "
                         "EV regions require total power strictly above calibrated EV power for at least two hours, "
                         "with two-hour context before and after the day. All times carry Brussels offsets. "
                         "No baseload is calculated or subtracted. Decimal powers and energies are strings.",
             responses={status: {"model": ErrorResponse, "description": description} for status, description in (
                 (422, "Invalid daily date or request fields."), (502, "FusionSolar request or data failure."),
                 (503, "Database unavailable."), (500, "Configuration or processing failure."))})
def daily_usage(payload: DailyUsageRequest, request: Request):
    report = request.app.state.usage_service(payload.date)
    zone = ZoneInfo("Europe/Brussels")
    return DailyUsageResponse(
        date=report.day, status="complete" if report.complete else "provisional", fetched_at=report.fetched_at,
        total_kwh=str(report.total_kwh) if report.total_kwh is not None else None,
        ev_total_kwh=str(report.ev_total_kwh),
        consumption=[ConsumptionIntervalResponse(
            start_at=s.start_at.astimezone(zone), end_at=s.end_at.astimezone(zone),
            power_kw=str(s.power_kw) if s.power_kw is not None else None,
            ev_power_kw=str(s.ev_power_kw) if s.ev_power_kw is not None else None,
        ) for s in report.consumption],
        ev_blocks=[EVBlockResponse(start_at=b.start_at.astimezone(zone), end_at=b.end_at.astimezone(zone),
                                  power_kw=str(b.power_kw), energy_kwh=str(b.energy_kwh))
                   for b in report.ev_blocks],
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
