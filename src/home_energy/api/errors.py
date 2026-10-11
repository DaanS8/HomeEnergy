"""Translate typed failures to a stable envelope without leaking upstream details."""

import logging

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from ..database.connection import DatabaseConfigurationError
from ..filling import FillError
from ..prices.entsoe import (InvalidPriceDataError, PriceAPIError, PriceConfigurationError,
                             PricesUnavailableError)
from ..prices.report import ReportRequestError
from ..usage.daily import DailyUsageRequestError, UsageConfigurationError
from ..usage.energy_balance import EnergyBalanceValidationError
from ..usage.fusionsolar import FusionSolarError
from .schemas import ErrorResponse


logger = logging.getLogger("uvicorn.error")


def error_response(exc: Exception) -> JSONResponse:
    while isinstance(exc, FillError):
        exc = exc.cause
    if isinstance(exc, (RequestValidationError, ReportRequestError, DailyUsageRequestError)):
        status, code, retryable = 422, "invalid_request", False
        message = "Invalid request. Check the endpoint's date limits and request fields."
    elif isinstance(exc, (DatabaseConfigurationError, PriceConfigurationError, UsageConfigurationError)):
        status, code, retryable = 500, "configuration_error", False
        message = "Home Energy configuration is invalid. Check server settings."
    elif isinstance(exc, PricesUnavailableError):
        status, code, retryable = 503, "prices_unavailable", True
        message = "No published prices are available for the requested day. Retry later."
    elif isinstance(exc, InvalidPriceDataError):
        status, code, retryable = 502, "invalid_provider_data", False
        message = "The provider returned invalid or incomplete price data."
    elif isinstance(exc, PriceAPIError):
        status, code, retryable = 502, "provider_unavailable", True
        message = "The price provider request failed. Retry later."
    elif isinstance(exc, EnergyBalanceValidationError):
        status, code, retryable = 502, "invalid_provider_data", False
        message = "FusionSolar returned invalid consumption data."
    elif isinstance(exc, FusionSolarError):
        status, code, retryable = 502, "provider_unavailable", True
        message = "The FusionSolar request failed. Check authentication or retry later."
    elif isinstance(exc, psycopg.Error):
        status, code, retryable = 503, "database_unavailable", True
        message = "The database is unavailable or its schema is not usable."
    elif isinstance(exc, HTTPException):
        status, retryable = exc.status_code, False
        code = {404: "not_found", 405: "method_not_allowed"}.get(status, "http_error")
        message = {404: "Endpoint not found.", 405: "Method not allowed."}.get(status, "HTTP request failed.")
    else:
        status, code, retryable = 500, "internal_error", False
        message = "Report processing failed. Check server logs."
    if status >= 500:
        # Do not log str(exc), chained tracebacks, or provider URLs with API keys.
        logger.error("Home Energy failure code=%s type=%s", code, type(exc).__name__)
    body = ErrorResponse(error=message, code=code, retryable=retryable)
    headers = exc.headers if isinstance(exc, HTTPException) else None
    return JSONResponse(status_code=status, content=body.model_dump(), headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    async def handler(request: Request, exc: Exception):
        return error_response(exc)

    for error_type in (RequestValidationError, ReportRequestError, FillError, PriceAPIError,
                       PriceConfigurationError, DatabaseConfigurationError, psycopg.Error, HTTPException,
                       DailyUsageRequestError, UsageConfigurationError, EnergyBalanceValidationError, FusionSolarError):
        app.add_exception_handler(error_type, handler)
