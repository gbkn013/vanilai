"""US National Weather Service (api.weather.gov). Keyless, US only.

Human-edited forecasts, so it counts as its own family. The hourly forecast
has no rain *amount* or pressure; those fields are simply left empty.
"""
from __future__ import annotations

import re
from datetime import timezone
from datetime import datetime
from typing import Any

import aiohttp

from ..normalize import compass_to_degrees, nws_text_to_condition
from .base import HourPoint, MemberForecast, Provider

POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"
USER_AGENT = "vanilai-homeassistant/0.1 (github.com/your-user/vanilai)"
US_BBOX = (24.0, -125.0, 50.0, -66.0)  # contiguous US, S, W, N, E
MAX_HOURS = 72
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def covers(lat: float, lon: float) -> bool:
    south, west, north, east = US_BBOX
    return south <= lat <= north and west <= lon <= east


def parse_wind_ms(text: str | None) -> float | None:
    """'7 km/h' or '5 to 10 km/h' -> m/s (midpoint of a range)."""
    if not text:
        return None
    nums = [float(n) for n in _NUMBER.findall(text)]
    if not nums:
        return None
    kmh = sum(nums) / len(nums)
    return round(kmh / 3.6, 2)


class NwsProvider(Provider):
    key = "nws"

    def __init__(self, session: aiohttp.ClientSession) -> None:
        super().__init__(session)
        self._grid: dict[tuple[float, float], str] = {}

    async def _get(self, url: str, **params: Any) -> dict:
        async with self._session.get(
            url,
            params=params or None,
            headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def fetch(self, lat: float, lon: float) -> list[MemberForecast]:
        if not covers(lat, lon):
            return []
        if (lat, lon) not in self._grid:
            points = await self._get(POINTS_URL.format(lat=lat, lon=lon))
            self._grid[(lat, lon)] = points["properties"]["forecastHourly"]
        data = await self._get(self._grid[(lat, lon)], units="si")
        return parse_response(data)


def parse_response(data: dict[str, Any]) -> list[MemberForecast]:
    hours: list[HourPoint] = []
    for period in data.get("properties", {}).get("periods", [])[:MAX_HOURS]:
        celsius = period.get("temperature")
        if celsius is not None and period.get("temperatureUnit") == "F":
            celsius = round((celsius - 32) * 5 / 9, 1)
        hours.append(
            HourPoint(
                time=datetime.fromisoformat(period["startTime"]).astimezone(timezone.utc),
                temperature=celsius,
                humidity=(period.get("relativeHumidity") or {}).get("value"),
                wind_speed=parse_wind_ms(period.get("windSpeed")),
                wind_bearing=compass_to_degrees(period.get("windDirection")),
                precipitation_probability=(
                    period.get("probabilityOfPrecipitation") or {}
                ).get("value"),
                condition=nws_text_to_condition(
                    period.get("shortForecast"), bool(period.get("isDaytime", True))
                ),
            )
        )
    if not hours:
        return []
    return [MemberForecast(member="nws:hourly", family="nws", hours=hours)]
