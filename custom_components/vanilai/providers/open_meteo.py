"""Open-Meteo provider: one member per underlying weather model.

Open-Meteo serves the raw output of many national weather services. Models
are listed in a registry; global ones are always used and regional ones are
enabled only when the location falls inside their coverage box (no geocoding
or country lookup needed, so nothing extra leaves Home Assistant).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp

from ..normalize import wmo_to_condition
from .base import HourPoint, MemberForecast, Provider

_LOGGER = logging.getLogger(__name__)

API_URL = "https://api.open-meteo.com/v1/forecast"


@dataclass(frozen=True)
class ModelSpec:
    """One Open-Meteo model and where it applies."""

    id: str
    family: str  # models sharing a family are correlated (weighted down)
    agency: str  # who runs it, for display
    bbox: tuple[float, float, float, float] | None = None  # S, W, N, E

    def covers(self, lat: float, lon: float) -> bool:
        if self.bbox is None:
            return True
        south, west, north, east = self.bbox
        return south <= lat <= north and west <= lon <= east


# Always on. The "seamless" variants already blend in each agency's regional
# high-resolution model (e.g. HRRR, ICON-D2, AROME) wherever one exists.
GLOBAL_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec("ecmwf_ifs025", "ecmwf", "ECMWF"),
    ModelSpec("gfs_seamless", "gfs", "NOAA (US)"),
    ModelSpec("icon_seamless", "icon", "DWD (Germany)"),
    ModelSpec("gem_seamless", "gem", "ECCC (Canada)"),
    ModelSpec("jma_seamless", "jma", "JMA (Japan)"),
    ModelSpec("meteofrance_seamless", "meteofrance", "Météo-France"),
    ModelSpec("ukmo_seamless", "ukmo", "Met Office (UK)"),
    ModelSpec("cma_grapes_global", "cma", "CMA (China)"),
)

# Enabled only inside their coverage box. Outside it Open-Meteo would either
# reject the request or return a copy of a global model.
REGIONAL_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec("metno_seamless", "metno", "MET Norway", (54.0, 3.0, 72.0, 33.0)),
    ModelSpec("dmi_seamless", "dmi", "DMI (Denmark)", (53.0, 3.0, 66.0, 20.0)),
    ModelSpec("knmi_seamless", "knmi", "KNMI (Netherlands)", (49.0, 0.0, 56.0, 9.0)),
    ModelSpec(
        "meteoswiss_icon_seamless", "icon", "MeteoSwiss", (45.0, 5.5, 48.5, 11.0)
    ),
    ModelSpec(
        "italia_meteo_arpae_icon_2i", "icon", "ItaliaMeteo (Italy)", (35.5, 6.0, 48.0, 19.0)
    ),
)

HOURLY_VARS = [
    "temperature_2m",
    "relative_humidity_2m",
    "pressure_msl",
    "wind_speed_10m",
    "wind_direction_10m",
    "precipitation",
    "precipitation_probability",
    "cloud_cover",
    "weather_code",
    "is_day",
]


def models_for(lat: float, lon: float) -> tuple[list[ModelSpec], list[ModelSpec]]:
    """(global models, regional models covering this location)."""
    return list(GLOBAL_MODELS), [m for m in REGIONAL_MODELS if m.covers(lat, lon)]


class OpenMeteoProvider(Provider):
    """Fetch the applicable models from Open-Meteo."""

    key = "open_meteo"

    async def _request(self, lat: float, lon: float, specs: list[ModelSpec]) -> dict:
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": ",".join(HOURLY_VARS),
            "models": ",".join(s.id for s in specs),
            "wind_speed_unit": "ms",
            "timezone": "UTC",
            "forecast_days": 7,
        }
        async with self._session.get(
            API_URL, params=params, timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def fetch(self, lat: float, lon: float) -> list[MemberForecast]:
        global_specs, regional_specs = models_for(lat, lon)
        # The global set must succeed; its failure means the provider failed.
        members = parse_response(
            await self._request(lat, lon, global_specs), global_specs
        )
        if not regional_specs:
            return members
        # Regional models are a bonus: a rejected one must never cost us the rest.
        try:
            members += parse_response(
                await self._request(lat, lon, regional_specs), regional_specs
            )
        except aiohttp.ClientError as err:
            _LOGGER.debug("Regional models rejected together (%s); trying singly", err)
            for spec in regional_specs:
                try:
                    members += parse_response(
                        await self._request(lat, lon, [spec]), [spec]
                    )
                except aiohttp.ClientError as single_err:
                    _LOGGER.debug("Regional model %s unavailable: %s", spec.id, single_err)
        return members


def parse_response(
    data: dict[str, Any], specs: list[ModelSpec] | tuple[ModelSpec, ...] | None = None
) -> list[MemberForecast]:
    """Turn an Open-Meteo response into members, one per model with data.

    A request for several models suffixes every variable with `_<model>`; a
    request for exactly one model returns the bare variable names.
    """
    specs = list(specs if specs is not None else GLOBAL_MODELS)
    hourly = data.get("hourly", {})
    times = [
        datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        for t in hourly.get("time", [])
    ]
    members: list[MemberForecast] = []
    for spec in specs:

        def col(var: str, spec: ModelSpec = spec) -> list[Any] | None:
            values = hourly.get(f"{var}_{spec.id}")
            if values is None and len(specs) == 1:
                values = hourly.get(var)
            return values

        temps = col("temperature_2m")
        if not temps or all(v is None for v in temps):
            continue  # model has no data at this location

        def at(var: str, i: int, col=col) -> Any:
            values = col(var)
            return values[i] if values and i < len(values) else None

        hours = []
        for i, t in enumerate(times):
            is_day = at("is_day", i)
            hours.append(
                HourPoint(
                    time=t,
                    temperature=at("temperature_2m", i),
                    humidity=at("relative_humidity_2m", i),
                    pressure=at("pressure_msl", i),
                    wind_speed=at("wind_speed_10m", i),
                    wind_bearing=at("wind_direction_10m", i),
                    precipitation=at("precipitation", i),
                    precipitation_probability=at("precipitation_probability", i),
                    cloud_coverage=at("cloud_cover", i),
                    condition=wmo_to_condition(at("weather_code", i), is_day != 0),
                )
            )
        members.append(
            MemberForecast(
                member=f"open_meteo:{spec.id}", family=spec.family, hours=hours
            )
        )
    return members
