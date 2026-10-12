"""Synchronous endpoints with HTTP serialization and documented outcomes."""

import base64
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request

from .schemas import (DayAheadReportRequest, DayAheadReportResponse, ErrorResponse,
                      HealthResponse, HighPriceRangeResponse, DailyUsageRequest,
                      ConsumptionIntervalResponse, EVBlockResponse, EVUsageReportResponse)


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


@router.post("/api/v1/reports/ev/daily", response_model=EVUsageReportResponse,
             operation_id="prepare_daily_ev_report", tags=["reports"],
             summary="Prepare a completed daily consumption and EV charging report",
             description="Return consumption readings, totals, EV blocks, and a base64 PNG of measured total consumption "
                         "with calibrated EV blocks overlaid. Only dates at least two days ago are accepted; "
                         "omit date for two days ago in Europe/Brussels. Missing complete days are stored once. "
                         "Days without detected EV charging still produce an image with zero estimated EV energy. "
                         "Detection requires total power strictly above calibrated EV power for at least two hours, "
                         "with two-hour context before and after the day. No baseload is subtracted. "
                         "Decimal powers and energies are strings; all times carry Brussels offsets.",
             responses={status: {"model": ErrorResponse, "description": description} for status, description in (
                 (422, "Invalid date; select a date at least two days ago."),
                 (502, "FusionSolar request or incomplete consumption data."),
                 (503, "Database unavailable."), (500, "Configuration or image processing failure."))})
def daily_ev_report(payload: DailyUsageRequest, request: Request):
    completed = request.app.state.ev_report_service(payload.date)
    report = completed.usage
    zone = ZoneInfo("Europe/Brussels")
    return EVUsageReportResponse(
        date=report.day, fetched_at=report.fetched_at, total_kwh=str(report.total_kwh),
        ev_total_kwh=str(report.ev_total_kwh), image=base64.b64encode(completed.png).decode("ascii"),
        image_filename=f"ev-usage-{report.day}.png",
        consumption=[ConsumptionIntervalResponse(
            start_at=s.start_at.astimezone(zone), end_at=s.end_at.astimezone(zone),
            power_kw=str(s.power_kw), ev_power_kw=str(s.ev_power_kw),
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
