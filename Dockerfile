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
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock README.md .python-version ./
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable

ENV PATH="/app/.venv/bin:${PATH}"
CMD ["uvicorn", "home_energy.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
