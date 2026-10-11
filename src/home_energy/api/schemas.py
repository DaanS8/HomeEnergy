"""Explicit public request, response, and error schemas."""

import re
from datetime import date as CalendarDate
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from ..prices.analysis import DEFAULT_THRESHOLD
from ..prices.repository import validate_price_range


class DayAheadReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: CalendarDate = Field(description="Calendar date YYYY-MM-DD in Europe/Brussels; at most tomorrow.",
                       examples=["2026-10-12"])
    threshold_eur_per_mwh: Decimal = Field(default=DEFAULT_THRESHOLD, allow_inf_nan=False,
                                         description="Actual prices strictly above this finite EUR/MWh threshold warn.",
                                         examples=["300"])

    @field_validator("date", mode="before")
    @classmethod
    def iso_date(cls, value):
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Use a calendar date YYYY-MM-DD.")
        return value

    @field_validator("date")
    @classmethod
    def allowed_date(cls, value):
        validate_price_range(value)
        return value


class HighPriceRangeResponse(BaseModel):
    start_at: AwareDatetime = Field(description="Inclusive start, ISO 8601 with Brussels UTC offset.")
    end_at: AwareDatetime = Field(description="Exclusive end, ISO 8601 with Brussels UTC offset.")


class DayAheadReportResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "date": "2026-10-12", "market": "BE", "timezone": "Europe/Brussels",
        "threshold_eur_per_mwh": "300", "fetched_at": "2026-10-11T17:00:00Z",
        "status": "warning", "image": "<base64 PNG>", "image_mime_type": "image/png",
        "image_filename": "day-ahead-2026-10-12.png", "warning": "van 17:00 tot 19:00",
        "high_price_ranges": [{"start_at": "2026-10-12T17:00:00+02:00",
                               "end_at": "2026-10-12T19:00:00+02:00"}],
    }]})

    date: CalendarDate
    market: Literal["BE"] = "BE"
    timezone: Literal["Europe/Brussels"] = "Europe/Brussels"
    threshold_eur_per_mwh: str = Field(description="Exact Decimal threshold in EUR/MWh, encoded as a string.")
    fetched_at: AwareDatetime = Field(description="Timestamp of the stored provider snapshot.")
    status: Literal["success", "warning"]
    image: str = Field(description="PNG encoded as plain base64, without a data-URL prefix.",
                       json_schema_extra={"contentEncoding": "base64", "contentMediaType": "image/png"})
    image_mime_type: Literal["image/png"] = "image/png"
    image_filename: str
    warning: str | None = Field(description="Plain Dutch warning summary, or null; no Discord formatting.")
    high_price_ranges: list[HighPriceRangeResponse]


class ErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    error: str = Field(description="Sanitized human-readable message.")
    code: Literal["invalid_request", "prices_unavailable", "provider_unavailable",
                  "invalid_provider_data", "database_unavailable", "configuration_error",
                  "internal_error", "not_found", "method_not_allowed", "http_error"] = Field(
                      description="Stable code for workflow branching.")
    retryable: bool = Field(description="Retrying unchanged input could help; caller chooses delay and limit.")


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
