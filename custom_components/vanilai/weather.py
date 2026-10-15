"""Weather entity exposing the fused forecast."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone

from homeassistant.components.weather import (
    Forecast,
    WeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, device_info
from .coordinator import FusedData, VanilaiCoordinator
from .providers.base import HourPoint


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([VanilaiWeather(hass.data[DOMAIN][entry.entry_id], entry)])


def _hour_forecast(p: HourPoint) -> Forecast:
    return Forecast(
        datetime=p.time.isoformat(),
        native_temperature=p.temperature,
        native_pressure=p.pressure,
        native_wind_speed=p.wind_speed,
        wind_bearing=p.wind_bearing,
        native_precipitation=p.precipitation,
        precipitation_probability=(
            None if p.precipitation_probability is None
            else round(p.precipitation_probability)
        ),
        humidity=p.humidity,
        cloud_coverage=p.cloud_coverage,
        condition=p.condition,
    )


def _avg(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


class VanilaiWeather(CoordinatorEntity[VanilaiCoordinator], WeatherEntity):
    """Fused weather for the active location."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_wind_speed_unit = UnitOfSpeed.METERS_PER_SECOND
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.MILLIMETERS
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_HOURLY | WeatherEntityFeature.FORECAST_DAILY
    )

    def __init__(self, coordinator: VanilaiCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_weather"
        self._attr_device_info = device_info(entry)

    @property
    def _data(self) -> FusedData:
        return self.coordinator.data

    @property
    def _now(self) -> HourPoint | None:
        """The fused hour closest to (but not after) the current time."""
        now = datetime.now(timezone.utc)
        past = [h for h in self._data.hours if h.time <= now]
        return past[-1] if past else (self._data.hours[0] if self._data.hours else None)

    # Current conditions ---------------------------------------------------

    @property
    def native_temperature(self) -> float | None:
        return self._now.temperature if self._now else None

    @property
    def native_pressure(self) -> float | None:
        return self._now.pressure if self._now else None

    @property
    def humidity(self) -> float | None:
        return self._now.humidity if self._now else None

    @property
    def native_wind_speed(self) -> float | None:
        return self._now.wind_speed if self._now else None

    @property
    def wind_bearing(self) -> float | None:
        return self._now.wind_bearing if self._now else None

    @property
    def cloud_coverage(self) -> float | None:
        return self._now.cloud_coverage if self._now else None

    @property
    def condition(self) -> str | None:
        return self._now.condition if self._now else None

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "sources": self._data.members,
            "latitude_used": self._data.latitude,
            "longitude_used": self._data.longitude,
            "stale": self._data.stale,
            "updated": self._data.fetched_at.isoformat() if self._data.fetched_at else None,
        }

    # Forecasts ------------------------------------------------------------

    async def async_forecast_hourly(self) -> list[Forecast]:
        now = datetime.now(timezone.utc)
        return [_hour_forecast(h) for h in self._data.hours if h.time >= now][:72]

    async def async_forecast_daily(self) -> list[Forecast]:
        days: dict = defaultdict(list)
        for h in self._data.hours:
            days[dt_util.as_local(h.time).date()].append(h)

        out: list[Forecast] = []
        for day, hours in sorted(days.items()):
            temps = [h.temperature for h in hours if h.temperature is not None]
            conditions = [
                h.condition for h in hours
                if h.condition and h.condition != "clear-night"
            ]
            precip = [h.precipitation for h in hours if h.precipitation is not None]
            probs = [
                h.precipitation_probability for h in hours
                if h.precipitation_probability is not None
            ]
            out.append(
                Forecast(
                    datetime=datetime.combine(
                        day, datetime.min.time(), tzinfo=dt_util.DEFAULT_TIME_ZONE
                    ).isoformat(),
                    native_temperature=max(temps) if temps else None,
                    native_templow=min(temps) if temps else None,
                    native_precipitation=round(sum(precip), 1) if precip else None,
                    precipitation_probability=round(max(probs)) if probs else None,
                    native_wind_speed=_avg([h.wind_speed for h in hours]),
                    humidity=_avg([h.humidity for h in hours]),
                    condition=(
                        Counter(conditions).most_common(1)[0][0] if conditions else None
                    ),
                )
            )
        return out
