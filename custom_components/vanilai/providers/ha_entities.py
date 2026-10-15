"""Fuse weather integrations you already have installed in Home Assistant.

Each chosen `weather.*` entity becomes its own source, read through HA's
`weather.get_forecasts` action, so no extra API keys are needed. We can't see
which model an integration uses, so each entity counts as its own family; a
copy of another source is dropped by the duplicate check.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.const import UnitOfLength, UnitOfPressure, UnitOfSpeed, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_conversion import (
    DistanceConverter,
    PressureConverter,
    SpeedConverter,
    TemperatureConverter,
)

from .base import HourPoint, MemberForecast

_LOGGER = logging.getLogger(__name__)
MAX_HOURS = 72


def _convert(converter, value, from_unit, to_unit):
    if value is None:
        return None
    try:
        return round(converter.convert(float(value), from_unit, to_unit), 2)
    except (TypeError, ValueError, HomeAssistantError):
        return None


def parse_forecast(
    entity_id: str, rows: list[dict[str, Any]], units
) -> MemberForecast | None:
    """Normalise HA forecast rows (in the user's unit system) to our schema."""
    hours: list[HourPoint] = []
    for row in rows[:MAX_HOURS]:
        raw_time = row.get("datetime")
        if not raw_time:
            continue
        time = datetime.fromisoformat(str(raw_time))
        if time.tzinfo is None:
            time = time.replace(tzinfo=timezone.utc)
        hours.append(
            HourPoint(
                time=time.astimezone(timezone.utc),
                temperature=_convert(
                    TemperatureConverter, row.get("temperature"),
                    units.temperature_unit, UnitOfTemperature.CELSIUS,
                ),
                humidity=row.get("humidity"),
                pressure=_convert(
                    PressureConverter, row.get("pressure"),
                    units.pressure_unit, UnitOfPressure.HPA,
                ),
                wind_speed=_convert(
                    SpeedConverter, row.get("wind_speed"),
                    units.wind_speed_unit, UnitOfSpeed.METERS_PER_SECOND,
                ),
                wind_bearing=_bearing(row.get("wind_bearing")),
                precipitation=_convert(
                    DistanceConverter, row.get("precipitation"),
                    units.accumulated_precipitation_unit, UnitOfLength.MILLIMETERS,
                ),
                precipitation_probability=row.get("precipitation_probability"),
                cloud_coverage=row.get("cloud_coverage"),
                condition=row.get("condition"),
            )
        )
    hours = [h for h in hours if h.temperature is not None]
    if not hours:
        return None
    return MemberForecast(member=f"ha:{entity_id}", family=f"ha:{entity_id}", hours=hours)


def _bearing(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None  # a compass string such as "NW" is not worth guessing


class HaEntitiesProvider:
    """Not a network `Provider`: it needs `hass`, so it has its own fetch()."""

    key = "ha_entities"

    def __init__(self, hass: HomeAssistant, entity_ids: list[str]) -> None:
        self._hass = hass
        self._entity_ids = entity_ids
        self.pending = False  # some entity wasn't ready; worth retrying soon

    async def fetch(self, lat: float, lon: float) -> list[MemberForecast]:  # noqa: ARG002
        members: list[MemberForecast] = []
        self.pending = False
        for entity_id in self._entity_ids:
            state = self._hass.states.get(entity_id)
            if state is None or state.state == "unavailable":
                _LOGGER.debug("%s is not available yet; skipping", entity_id)
                self.pending = True
                continue
            rows: list[dict[str, Any]] = []
            for kind in ("hourly", "daily"):  # daily-only entities still add signal
                try:
                    response = await self._hass.services.async_call(
                        "weather",
                        "get_forecasts",
                        {"entity_id": entity_id, "type": kind},
                        blocking=True,
                        return_response=True,
                    )
                except HomeAssistantError as err:
                    _LOGGER.debug("%s has no %s forecast: %s", entity_id, kind, err)
                    continue
                rows = (response or {}).get(entity_id, {}).get("forecast", [])
                if rows:
                    break
            if member := parse_forecast(entity_id, rows, self._hass.config.units):
                members.append(member)
        return members
