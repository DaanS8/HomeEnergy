"""Exercise the release image and PostgreSQL in a disposable, isolated Docker stack."""

import argparse
import base64
import io
import json
import secrets
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import dotenv_values
from PIL import Image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="home-energy:0.1.0")
    parser.add_argument("--port", type=int, default=18001)
    parser.add_argument("--live-provider", action="store_true", help="Also fetch yesterday using the local ENTSOE_API_KEY")
    args = parser.parse_args()
    project = "home-energy-smoke-" + secrets.token_hex(4)
    password = secrets.token_urlsafe(24)
    env = {"PGHOST": "db", "PGPORT": "5432", "PGDATABASE": "smoke", "PGUSER": "smoke", "PGPASSWORD": password}
    if args.live_provider:
        key = dotenv_values(".env", interpolate=False, encoding="utf-8-sig").get("ENTSOE_API_KEY")
        if not key or key.startswith("YOUR-"):
            parser.error("A real ENTSOE_API_KEY is required for --live-provider.")
        env["ENTSOE_API_KEY"] = key
    compose = {"services": {
        "db": {"image": "postgres:17-bookworm", "environment": {"POSTGRES_DB": "smoke", "POSTGRES_USER": "smoke", "POSTGRES_PASSWORD": password},
               "volumes": ["data:/var/lib/postgresql/data"],
               "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U smoke -d smoke"], "interval": "2s", "timeout": "5s", "retries": 30}},
        "app": {"image": args.image, "init": True, "environment": env, "ports": [f"127.0.0.1:{args.port}:8000"],
                "depends_on": {"db": {"condition": "service_healthy"}},
                "security_opt": ["no-new-privileges:true"], "cap_drop": ["ALL"]}},
        "volumes": {"data": {}}}
    with tempfile.TemporaryDirectory(prefix=project) as directory:
        config = Path(directory) / "compose.json"
        config.write_text(json.dumps(compose), encoding="utf-8")
        prefix = ["docker", "compose", "-p", project, "-f", str(config)]

        def docker(*command, input=None):
            result = subprocess.run([*prefix, *command], input=input, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(f"Docker {command[0]} failed: {result.stderr}")
            return result.stdout.strip()

        base = f"http://127.0.0.1:{args.port}"

        def ready():
            for _ in range(45):
                try:
                    if requests.get(base + "/health/ready", timeout=10).status_code == 200:
                        return
                except requests.RequestException:
                    pass
                time.sleep(1)
            raise RuntimeError("API did not become ready.")

        def report(day, threshold="300"):
            response = requests.post(base + "/api/v1/reports/day-ahead", json={"date": day, "threshold_eur_per_mwh": threshold}, timeout=180)
            if response.status_code != 200:
                # The API error envelope is deliberately sanitized.
                raise RuntimeError(f"Report failed: HTTP {response.status_code}: {response.json()}")
            body = response.json()
            data = base64.b64decode(body["image"], validate=True)
            with Image.open(io.BytesIO(data)) as image:
                assert image.format == "PNG" and image.width > 500 and image.height > 200
                image.verify()
            return body, data

        try:
            docker("up", "-d", "--wait", "--wait-timeout", "120")
            ready()
            assert docker("exec", "-T", "app", "id", "-u") == "10001"
            assert requests.get(base + "/health/live", timeout=10).json() == {"status": "ok"}
            schema = requests.get(base + "/openapi.json", timeout=10).json()
            assert "/api/v1/reports/day-ahead" in schema["paths"]
            invalid = requests.post(base + "/api/v1/reports/day-ahead", json={"date": "bad"}, timeout=10)
            assert invalid.status_code == 422 and not invalid.json()["retryable"]
            seed = '''from datetime import date, datetime, timezone
from decimal import Decimal
from home_energy.prices.models import DayAheadPriceDay, PriceInterval, day_bounds, interval_duration
from home_energy.prices.repository import DayAheadPriceRepository
day = date(2000, 1, 1)
start, end = day_bounds(day)
step = interval_duration(day)
intervals = []
while start < end:
    intervals.append(PriceInterval(start, start + step, Decimal("100")))
    start += step
DayAheadPriceRepository().save_day(DayAheadPriceDay(day, "BE", "Europe/Brussels", datetime.now(timezone.utc), intervals))
'''
            docker("exec", "-T", "app", "python", "-", input=seed)
            first, _ = report("2000-01-01")
            assert first["status"] == "success" and first["warning"] is None
            warning, _ = report("2000-01-01", "50")
            assert warning["status"] == "warning" and warning["high_price_ranges"]
            docker("restart", "db", "app")
            ready()
            again, _ = report("2000-01-01")
            assert first["fetched_at"] == again["fetched_at"]
            docker("stop", "db")
            unavailable = requests.get(base + "/health/ready", timeout=15)
            assert unavailable.status_code == 503 and unavailable.json()["code"] == "database_unavailable"
            assert requests.get(base + "/health/live", timeout=10).status_code == 200
            docker("start", "db")
            ready()
            print("PASS: non-root startup, health, OpenAPI, validation, success/warning PNGs, restart persistence, database outage/recovery.", flush=True)
            if args.live_provider:
                day = (datetime.now(ZoneInfo("Europe/Brussels")) - timedelta(days=1)).date().isoformat()
                live, png = report(day)
                cached, _ = report(day)
                assert live["fetched_at"] == cached["fetched_at"]
                output = Path("dist/smoke-test")
                output.mkdir(parents=True, exist_ok=True)
                (output / live["image_filename"]).write_bytes(png)
                print(f"PASS: live ENTSO-E report for {day}, cached repeat; PNG saved to {output}.", flush=True)
        finally:
            # Removes only this unique test project's containers and disposable volume.
            docker("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
