#!/usr/bin/env python3
"""End-to-end test: throwaway Home Assistant + the real config flow.

Starts an isolated HA container (port 18123, empty config, NOT your live HA),
mounts this repo's integration read-only, onboards a user through the API,
adds the integration via its config flow, then checks the entities.

    python3 tests/e2e/run_e2e.py [--keep] [--lat 59.91 --lon 10.75 --tz Europe/Oslo]
                                 [--option key=value ...]

Needs: docker, network access (it calls the real weather APIs).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

IMAGE = "ghcr.io/home-assistant/home-assistant:stable"
NAME = "vanilai-e2e"
PORT = 18123
BASE = f"http://localhost:{PORT}"
CLIENT_ID = f"{BASE}/"
REPO = Path(__file__).resolve().parents[2]


def sh(*cmd: str, check: bool = True) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True)
    if check and out.returncode:
        raise SystemExit(f"command failed: {' '.join(cmd)}\n{out.stderr}")
    return out.stdout + out.stderr


def call(path: str, body=None, token: str | None = None, form: bool = False):
    headers = {}
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as err:
        raise SystemExit(f"HTTP {err.code} on {path}: {err.read().decode()[:400]}")


def wait_for(fn, what: str, timeout: int = 180, every: float = 3):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            value = fn()
            if value:
                return value
        except (urllib.error.URLError, ConnectionError, OSError, SystemExit) as err:
            last = err
        time.sleep(every)
    raise SystemExit(f"timed out waiting for {what} ({last})")


def start(cfg: Path, lat: float, lon: float, tz: str) -> None:
    sh("docker", "rm", "-f", NAME, check=False)
    (cfg / "configuration.yaml").write_text(
        "default_config:\n"
        "demo:\n"
        "homeassistant:\n"
        f"  latitude: {lat}\n  longitude: {lon}\n  elevation: 10\n"
        "  unit_system: metric\n"
        f"  time_zone: {tz}\n  name: E2E\n"
        "logger:\n  default: warning\n  logs:\n    custom_components.vanilai: debug\n"
    )
    sh(
        "docker", "run", "-d", "--name", NAME, "-p", f"{PORT}:8123",
        "-v", f"{cfg}:/config",
        "-v", f"{REPO}/custom_components/vanilai:/config/custom_components/vanilai:ro",
        IMAGE,
    )


def teardown(cfg: Path) -> None:
    sh("docker", "rm", "-f", NAME, check=False)
    # HA writes root-owned files; remove them as root.
    sh("docker", "run", "--rm", "--entrypoint", "sh", "-v", f"{cfg}:/c", IMAGE,
       "-c", "rm -rf /c/* /c/.[!.]*", check=False)


def onboard() -> str:
    wait_for(lambda: call("/api/onboarding"), "HA onboarding API")
    code = call(
        "/api/onboarding/users",
        {"client_id": CLIENT_ID, "name": "e2e", "username": "e2e",
         "password": "e2e-test-password", "language": "en"},
    )["auth_code"]
    token = call(
        "/auth/token",
        {"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID},
        form=True,
    )["access_token"]
    return token


def add_integration(token: str, options: dict) -> None:
    flow = call("/api/config/config_entries/flow",
                {"handler": "vanilai", "show_advanced_options": False}, token)
    assert flow["type"] == "form", flow
    user_input = {"reverse_geocode": True, "move_threshold_km": 2.0, **options}
    done = call(f"/api/config/config_entries/flow/{flow['flow_id']}", user_input, token)
    assert done["type"] == "create_entry", done


def states(token: str) -> dict:
    return {s["entity_id"]: s for s in call("/api/states", token=token)
            if s["entity_id"].split(".")[1].startswith(("vanilai", "e2e"))}


class Checks:
    def __init__(self) -> None:
        self.failed: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not ok:
            self.failed.append(label)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, default=59.91)
    ap.add_argument("--lon", type=float, default=10.75)
    ap.add_argument("--tz", default="Europe/Oslo")
    ap.add_argument("--keep", action="store_true", help="leave the container running")
    ap.add_argument("--option", action="append", default=[], metavar="K=V")
    args = ap.parse_args()
    options = {}
    for o in args.option:
        k, v = o.split("=", 1)
        try:
            options[k] = json.loads(v)
        except ValueError:
            options[k] = v

    cfg = Path(tempfile.mkdtemp(prefix="vanilai-e2e-"))
    checks = Checks()
    try:
        print(f"starting isolated HA at {args.lat},{args.lon} ...")
        start(cfg, args.lat, args.lon, args.tz)
        token = onboard()
        print("onboarded; adding integration through its config flow ...")
        add_integration(token, options)

        def ready():
            st = states(token)
            core = ["sensor.vanilai_fused_temperature", "sensor.vanilai_summary",
                    "sensor.vanilai_confidence"]
            return st if all(
                k in st and st[k]["state"] not in ("unknown", "unavailable") for k in core
            ) else None

        st = wait_for(ready, "vanilai sensors to populate", timeout=240)
        print("entities populated. checks:")

        def val(eid): return st[eid]["state"]
        fused = float(val("sensor.vanilai_fused_temperature"))
        sources = int(float(val("sensor.vanilai_independent_sources")))
        conf = float(val("sensor.vanilai_confidence"))
        members = [k for k in st if "open_meteo" in k]
        weather = [k for k in st if k.startswith("weather.")]

        checks.check(-60 < fused < 60, "fused temperature is plausible", f"{fused}°C")
        checks.check(sources >= 6, "at least 6 independent sources", str(sources))
        checks.check(0 <= conf <= 100, "confidence within 0-100", str(conf))
        checks.check(len(members) >= 6, "per-model sensors exist", str(len(members)))
        checks.check(len(weather) == 1, "one weather entity", str(weather))
        checks.check(bool(val("sensor.vanilai_summary")), "summary text present",
                     val("sensor.vanilai_summary")[:90])
        loc = val("sensor.vanilai_location")
        checks.check(bool(loc) and loc != "unknown", "location named", loc)
        fc = st["sensor.vanilai_fused_temperature"]["attributes"].get("forecast", [])
        checks.check(len(fc) >= 24, "48h chart forecast attribute", f"{len(fc)} rows")

        trust = st.get("sensor.vanilai_source_trust")
        checks.check(trust is not None and trust["state"] in
                     ("learning", "calibrated", "no observations nearby"),
                     "source trust sensor", trust["state"] if trust else "missing")
        status = st.get("sensor.vanilai_data_status")
        checks.check(status is not None and status["state"] in ("fresh", "degraded"),
                     "data status sensor", status["state"] if status else "missing")
        if status:
            provs = status["attributes"].get("providers", {})
            checks.check({"open_meteo", "met_no", "nws"} <= set(provs),
                         "all providers reported health", ",".join(sorted(provs)))
            checks.check(provs.get("open_meteo", {}).get("ok") is True, "Open-Meteo healthy")
            checks.check(provs.get("met_no", {}).get("ok") is True, "MET Norway healthy")
        nr = st.get("sensor.vanilai_next_rain")
        fe = st.get("sensor.vanilai_forecast_error")
        checks.check(fe is not None, "forecast error sensor", fe["state"] if fe else "missing")
        rs = st.get("sensor.vanilai_rain_forecast_skill")
        checks.check(rs is not None, "rain skill sensor", rs["state"] if rs else "missing")
        checks.check(nr is not None, "next rain sensor", nr["state"] if nr else "missing")
        srcs = (trust or {}).get("attributes", {}).get("sources", {})
        checks.check("met_no:locationforecast" in srcs, "MET Norway counted as a source")
        if "extra_weather_entities" in options:
            def fused_demo():  # other integrations may load after ours; we retry
                t = states(token).get("sensor.vanilai_source_trust")
                return [k for k in (t or {}).get("attributes", {}).get("sources", {})
                        if k.startswith("ha:")]
            try:
                demo = wait_for(fused_demo, "existing weather entities to be fused", timeout=150)
            except SystemExit:
                demo = []
            checks.check(bool(demo), "your existing weather entities are fused", ",".join(demo))

        logs = sh("docker", "logs", NAME, check=False)
        bad = [l for l in logs.splitlines()
               if "vanilai" in l.lower() and any(w in l for w in ("ERROR", "Traceback", "Exception"))]
        checks.check(not bad, "no vanilai errors in HA log", bad[0][:120] if bad else "")

        print("\nsample:")
        for k in sorted(st):
            if k.startswith("sensor.vanilai") and "open_meteo" not in k:
                print(f"  {k} = {st[k]['state'][:100]}")
        print(f"  models: {', '.join(m.replace('sensor.vanilai_open_meteo_','').replace('_temperature','') for m in sorted(members))}")
    finally:
        if args.keep:
            print(f"\nleft running: {BASE} (user e2e / e2e-test-password); config {cfg}")
        else:
            teardown(cfg)
    print("\nRESULT:", "FAILED: " + ", ".join(checks.failed) if checks.failed else "all checks passed")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
