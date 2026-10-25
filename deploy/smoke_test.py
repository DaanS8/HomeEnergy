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
from importlib.metadata import version
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import dotenv_values
from PIL import Image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=f"home-energy:{version('home-energy')}")
    parser.add_argument("--expected-version", default=version("home-energy"))
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

        def post_report(path, body):
            response = requests.post(base + path, json=body, timeout=180)
            if response.status_code != 200:
                # The API error envelope is deliberately sanitized.
                raise RuntimeError(f"Report failed: HTTP {response.status_code}: {response.json()}")
            body = response.json()
            data = base64.b64decode(body["image"], validate=True)
            with Image.open(io.BytesIO(data)) as image:
                assert image.format == "PNG" and image.width > 500 and image.height > 200
                image.verify()
            return body, data

        def report(day, threshold="300"):
            return post_report("/api/v1/reports/day-ahead", {"date": day, "threshold_eur_per_mwh": threshold})

        def ev_report(day):
            return post_report("/api/v1/reports/ev/daily", {"date": day})

        try:
            docker("up", "-d", "--wait", "--wait-timeout", "120")
            ready()
            assert docker("exec", "-T", "app", "id", "-u") == "10001"
            assert requests.get(base + "/health/live", timeout=10).json() == {"status": "ok"}
            schema = requests.get(base + "/openapi.json", timeout=10).json()
            assert "/api/v1/reports/day-ahead" in schema["paths"]
            assert "/api/v1/reports/ev/daily" in schema["paths"]
            assert schema["info"]["version"] == args.expected_version
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
            usage_seed = '''from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from home_energy.usage.constants import PLANT_ID
from home_energy.usage.energy_balance import parse_energy_balance
from home_energy.usage.repository import EnergyBalanceRepository
zone = ZoneInfo("Europe/Brussels")
charge_start = datetime(2000, 1, 1, 23, 30, tzinfo=zone)
charge_end = datetime(2000, 1, 2, 1, 30, tzinfo=zone)
repository = EnergyBalanceRepository()
for offset in range(-1, 4):
    day = date(2000, 1, 1) + timedelta(days=offset)
    start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    labels, values = [], []
    while start < end:
        local = start.astimezone(zone)
        labels.append(local.isoformat())
        values.append("2.4" if charge_start <= local < charge_end else "0.2")
        start += timedelta(minutes=5)
    payload = {"data": {"stationDn": PLANT_ID, "xAxis": labels,
                        "usePower": values, "totalUsePower": "42.750"}}
    repository.save_day(parse_energy_balance(payload, plant_id=PLANT_ID, day=day,
                                             timezone_name="Europe/Brussels"))
'''
            docker("exec", "-T", "app", "python", "-", input=usage_seed)
            first, _ = report("2000-01-01")
            assert first["status"] == "success" and first["warning"] is None
            warning, _ = report("2000-01-01", "50")
            assert warning["status"] == "warning" and warning["high_price_ranges"]
            usage, _ = ev_report("2000-01-01")
            assert usage["status"] == "complete" and usage["total_kwh"] == "42.750"
            assert usage["ev_total_kwh"] == "1.0795" and len(usage["ev_blocks"]) == 1
            assert len(usage["consumption"]) == 288
            assert usage["ev_blocks"][0]["end_at"] == "2000-01-02T00:00:00+01:00"
            cached_usage, _ = ev_report("2000-01-01")
            assert cached_usage["ev_total_kwh"] == usage["ev_total_kwh"]
            no_charge, _ = ev_report("2000-01-03")
            assert no_charge["ev_total_kwh"] == "0" and no_charge["ev_blocks"] == []
            docker("restart", "db", "app")
            ready()
            again, _ = report("2000-01-01")
            assert first["fetched_at"] == again["fetched_at"]
            usage_again, _ = ev_report("2000-01-01")
            assert usage_again["consumption"] == usage["consumption"]
            assert usage_again["ev_total_kwh"] == usage["ev_total_kwh"]
            docker("stop", "db")
            unavailable = requests.get(base + "/health/ready", timeout=15)
            assert unavailable.status_code == 503 and unavailable.json()["code"] == "database_unavailable"
            assert requests.get(base + "/health/live", timeout=10).status_code == 200
            docker("start", "db")
            ready()
            print("PASS: non-root startup, health, OpenAPI version, validation, price success/warning PNGs, EV PNG and midnight energy, cached reports, restart persistence, database outage/recovery.", flush=True)
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
