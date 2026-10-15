"""MET Norway Locationforecast: the Norwegian Meteorological Institute's API.

Global and keyless. Inside the Nordics it is MET's own high-resolution model;
elsewhere it is built on ECMWF, so it shares the ECMWF family's vote.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiohttp

from ..normalize import metno_symbol_to_condition
from .base import HourPoint, MemberForecast, Provider

API_URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
# MET requires an identifying User-Agent with a way to contact the project.
USER_AGENT = "vanilai-homeassistant/0.1 github.com/your-user/vanilai"
NORDIC_BBOX = (54.0, 3.0, 72.0, 33.0)  # S, W, N, E
MAX_HOURS = 72


def family_for(lat: float, lon: float) -> str:
    south, west, north, east = NORDIC_BBOX
    return "metno" if south <= lat <= north and west <= lon <= east else "ecmwf"


class MetNoProvider(Provider):
    key = "met_no"

    def __init__(self, session: aiohttp.ClientSession) -> None:
        super().__init__(session)
        self._etag: str | None = None
        self._cached: list[MemberForecast] | None = None
        self._cached_for: tuple[float, float] | None = None

    async def fetch(self, lat: float, lon: float) -> list[MemberForecast]:
        headers = {"User-Agent": USER_AGENT}
        if self._etag and self._cached is not None and self._cached_for == (lat, lon):
            headers["If-None-Match"] = self._etag  # polite: let MET answer 304
        async with self._session.get(
            API_URL,
            params={"lat": lat, "lon": lon},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 304 and self._cached is not None:
                return self._cached
            resp.raise_for_status()
            data = await resp.json()
            self._etag = resp.headers.get("ETag")
        self._cached = parse_response(data, family_for(lat, lon))
        self._cached_for = (lat, lon)
        return self._cached


def _hour(entry: dict[str, Any]) -> HourPoint | None:
    """One hourly point, or None for the coarser 6-hourly tail."""
    block = entry["data"].get("next_1_hours")
    if not block:
        return None
    instant = entry["data"]["instant"]["details"]
    details = block.get("details", {})
    time = datetime.fromisoformat(entry["time"].replace("Z", "+00:00")).astimezone(
        timezone.utc
    )
    return HourPoint(
        time=time,
        temperature=instant.get("air_temperature"),
        humidity=instant.get("relative_humidity"),
        pressure=instant.get("air_pressure_at_sea_level"),
        wind_speed=instant.get("wind_speed"),
        wind_bearing=instant.get("wind_from_direction"),
        precipitation=details.get("precipitation_amount"),
        precipitation_probability=details.get("probability_of_precipitation"),
        cloud_coverage=instant.get("cloud_area_fraction"),
        condition=metno_symbol_to_condition(block.get("summary", {}).get("symbol_code")),
    )


def parse_response(data: dict[str, Any], family: str = "ecmwf") -> list[MemberForecast]:
    hours = [
        h
        for entry in data.get("properties", {}).get("timeseries", [])
        if (h := _hour(entry)) is not None
    ][:MAX_HOURS]
    if not hours:
        return []
    return [MemberForecast(member="met_no:locationforecast", family=family, hours=hours)]
