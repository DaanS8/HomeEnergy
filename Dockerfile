FROM python:3.10-slim-bookworm

RUN python -m pip install --no-cache-dir uv==0.10.1

# ZoneInfo needs timezone data on Linux too, for the existing API client.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    HOME=/home/app

COPY pyproject.toml uv.lock README.md .python-version ./
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /app/.cache /app/outputs \
    && chown app:app /app/.cache /app/outputs

ENV PATH="/app/.venv/bin:${PATH}"
ARG VERSION=0.2.0
LABEL org.opencontainers.image.source="https://github.com/DaanS8/TrackingPowerUsage" \
      org.opencontainers.image.description="Home Energy API for Belgian day-ahead prices and home energy reports" \
      org.opencontainers.image.version="${VERSION}"
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=8).close()"
CMD ["uvicorn", "home_energy.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
