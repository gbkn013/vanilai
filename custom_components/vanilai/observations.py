"""Real-world temperature observations used to check forecast accuracy.

Primary source: the nearest airport METAR (AviationWeather.gov, keyless).
Optionally a Home Assistant temperature sensor you own, which wins when set.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .geo import distance_km

URL = "https://aviationweather.gov/api/data/metar"
USER_AGENT = "vanilai-homeassistant/0.1 (github.com/your-user/vanilai)"
SEARCH_RADIUS_DEG = 1.2  # ~130 km box around the location
MAX_STATION_KM = 100.0
MIN_REPORTS = 6  # in 24 h, to count as an active station
HOURS = 24


@dataclass
class Observations:
    station: str
    name: str
    distance_km: float
    temps: dict[int, float]  # unix timestamp (rounded to the hour) -> °C
    wet: dict[int, bool] = field(default_factory=dict)  # hour -> any rain reported
    elevation_m: float | None = None


LAPSE_C_PER_M = 0.0065  # standard atmosphere
MAX_ELEVATION_ADJUST_C = 5.0
_PRECIP_CODES = ("RA", "DZ", "SN", "SG", "PL", "GR", "GS", "UP", "SH", "TS")


def reports_rain(wx: str | None) -> bool:
    """True if a METAR present-weather string says precipitation is falling here.

    Mist, haze and fog are not rain; "VC" (in the vicinity) means not at the station.
    """
    for token in (wx or "").split():
        token = token.lstrip("+-")
        if token.startswith("VC"):
            continue
        if any(code in token for code in _PRECIP_CODES):
            return True
    return False


def adjust_to_elevation(
    temps: dict[int, float], station_m: float | None, home_m: float | None
) -> dict[int, float]:
    """Shift station temperatures to your elevation (colder up, warmer down)."""
    if station_m is None or home_m is None:
        return temps
    shift = LAPSE_C_PER_M * (station_m - home_m)
    shift = max(-MAX_ELEVATION_ADJUST_C, min(MAX_ELEVATION_ADJUST_C, shift))
    return {h: round(t + shift, 2) for h, t in temps.items()}


def _hour(ts: int) -> int:
    return int(round(ts / 3600.0)) * 3600


def parse_metar(
    reports: list[dict[str, Any]], lat: float, lon: float
) -> Observations | None:
    """Nearest active station's hourly temperatures, or None."""
    by_station: dict[str, list[dict[str, Any]]] = {}
    for r in reports:
        if r.get("temp") is None or r.get("obsTime") is None or r.get("lat") is None:
            continue
        by_station.setdefault(r["icaoId"], []).append(r)

    best: tuple[float, str] | None = None
    for icao, rows in by_station.items():
        if len(rows) < MIN_REPORTS:
            continue
        d = distance_km(lat, lon, rows[0]["lat"], rows[0]["lon"])
        if d <= MAX_STATION_KM and (best is None or d < best[0]):
            best = (d, icao)
    if best is None:
        return None

    distance, icao = best
    closest: dict[int, tuple[int, float]] = {}  # hour -> (seconds from hour, temp)
    for r in by_station[icao]:
        hour = _hour(r["obsTime"])
        offset = abs(r["obsTime"] - hour)
        if hour not in closest or offset < closest[hour][0]:
            closest[hour] = (offset, float(r["temp"]))
    wet: dict[int, bool] = {}
    for r in by_station[icao]:
        hour = _hour(r["obsTime"])
        wet[hour] = wet.get(hour, False) or reports_rain(r.get("wxString"))
    return Observations(
        wet=wet,
        elevation_m=by_station[icao][0].get("elev"),
        station=icao,
        name=by_station[icao][0].get("name", icao),
        distance_km=round(distance, 1),
        temps={h: t for h, (_, t) in closest.items()},
    )


async def fetch_metar(
    session: aiohttp.ClientSession, lat: float, lon: float
) -> Observations | None:
    """Never raises: observations are a bonus, not a requirement."""
    box = SEARCH_RADIUS_DEG
    params = {
        "bbox": f"{lat - box:.2f},{lon - box:.2f},{lat + box:.2f},{lon + box:.2f}",
        "format": "json",
        "hours": HOURS,
    }
    try:
        async with session.get(
            URL,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            if resp.status == 204:  # no content: no stations in the box
                return None
            return parse_metar(await resp.json(content_type=None), lat, lon)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
        return None


def sensor_observation(value: str | float | None, now: datetime) -> dict[int, float]:
    """A single reading from the user's own temperature sensor, at this hour."""
    try:
        temp = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return {}
    return {_hour(int(now.astimezone(timezone.utc).timestamp())): temp}
