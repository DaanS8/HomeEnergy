"""Read local connection settings and open a Psycopg connection."""

import os
from pathlib import Path

import psycopg
from dotenv import dotenv_values


class DatabaseConfigurationError(ValueError):
    """Missing or invalid settings, with no credential values in the message."""


def connect() -> psycopg.Connection:
    """Open a connection using working-directory .env, then process overrides.

    All five PG settings are required so a missing configuration cannot silently
    connect to another database. Values are passed as keyword arguments rather
    than interpolated into a connection string. The caller owns the connection
    and transaction; use ``with connect() as connection`` for automatic cleanup.
    """
    try:
        settings = dotenv_values(Path(".env"), interpolate=False, encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise DatabaseConfigurationError("Cannot read .env. Check its permissions and UTF-8 encoding.") from None
    settings.update(os.environ)
    required = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")
    missing = [key for key in required if not settings.get(key) or not settings[key].strip()]
    if missing:
        raise DatabaseConfigurationError("Set required database settings: " + ", ".join(missing) + ".")
    try:
        port = int(settings["PGPORT"])
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise DatabaseConfigurationError("PGPORT must be an integer between 1 and 65535.") from None
    return psycopg.connect(
        host=settings["PGHOST"].strip(),
        port=port,
        dbname=settings["PGDATABASE"].strip(),
        user=settings["PGUSER"].strip(),
        password=settings["PGPASSWORD"],
        connect_timeout=5,
    )
