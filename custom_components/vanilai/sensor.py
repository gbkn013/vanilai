"""Comparison sensors: fused temperature, spread, confidence, per source."""
from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, device_info
from .coordinator import VanilaiCoordinator
from .fusion.confidence import confidence
from .fusion.verify import MIN_SAMPLES
from .providers.base import HourPoint
from .summary import first_rain, summarize
from homeassistant.util import dt as dt_util

CHART_HOURS = 48


def _upcoming(points: list[HourPoint]) -> list[HourPoint]:
    """The current hour onward, limited to the chart window."""
    now = datetime.now(timezone.utc)
    start = 0
    for i, p in enumerate(points):
        if p.time <= now:
            start = i
    return points[start : start + CHART_HOURS]


def _lead_hours(t: datetime) -> float:
    return (t - datetime.now(timezone.utc)).total_seconds() / 3600


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: VanilaiCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            FusedTemperature(coordinator, entry),
            TemperatureSpread(coordinator, entry),
            Confidence(coordinator, entry),
            SourcesUsed(coordinator, entry),
            Location(coordinator, entry),
            Summary(coordinator, entry),
            NextRain(coordinator, entry),
            SourceTrust(coordinator, entry),
            ForecastError(coordinator, entry),
            RainSkill(coordinator, entry),
            DataStatus(coordinator, entry),
        ]
    )

    known: set[str] = set()

    @callback
    def _add_members() -> None:
        new = [m for m in coordinator.data.series if m not in known]
        if new:
            known.update(new)
            async_add_entities([MemberTemperature(coordinator, entry, m) for m in new])

    _add_members()
    entry.async_on_unload(coordinator.async_add_listener(_add_members))


class _Base(CoordinatorEntity[VanilaiCoordinator], SensorEntity):
    _attr_has_entity_name = True
    # Big chart payloads don't belong in the recorder database.
    _unrecorded_attributes = frozenset({"forecast", "sources"})

    def __init__(self, coordinator: VanilaiCoordinator, entry: ConfigEntry, key: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = device_info(entry)

    @property
    def _fused_now(self) -> HourPoint | None:
        hours = _upcoming(self.coordinator.data.hours)
        return hours[0] if hours else None


class FusedTemperature(_Base):
    _attr_name = "Fused temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "fused_temperature")

    @property
    def native_value(self) -> float | None:
        return self._fused_now.temperature if self._fused_now else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        rows = []
        for p in _upcoming(data.hours):
            s = data.stats.get(p.time)
            rows.append(
                {
                    "datetime": p.time.isoformat(),
                    "temperature": p.temperature,
                    "min": s.minimum if s else None,
                    "max": s.maximum if s else None,
                    "precipitation_probability": p.precipitation_probability,
                    "confidence": confidence(s, _lead_hours(p.time)),
                }
            )
        return {"forecast": rows}


class TemperatureSpread(_Base):
    _attr_name = "Temperature spread"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "temperature_spread")

    @property
    def native_value(self) -> float | None:
        now = self._fused_now
        s = self.coordinator.data.stats.get(now.time) if now else None
        return round(s.maximum - s.minimum, 2) if s else None


class Confidence(_Base):
    _attr_name = "Confidence"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "confidence")

    @property
    def native_value(self) -> int | None:
        now = self._fused_now
        if not now:
            return None
        return confidence(self.coordinator.data.stats.get(now.time), 0)


class SourcesUsed(_Base):
    _attr_name = "Independent sources"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "sources_used")

    @property
    def native_value(self) -> int | None:
        now = self._fused_now
        s = self.coordinator.data.stats.get(now.time) if now else None
        return s.families if s else None

    @property
    def extra_state_attributes(self) -> dict:
        return {"sources": self.coordinator.data.members}


class Summary(_Base):
    """Plain-language outlook, e.g. for a dashboard header or a notification."""

    _attr_name = "Summary"
    _attr_icon = "mdi:text-short"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "summary")

    def _summary(self) -> tuple[dict, float | None]:
        data = self.coordinator.data
        hours = _upcoming(data.hours)[:24]
        scores = [
            confidence(data.stats.get(h.time), _lead_hours(h.time)) for h in hours
        ]
        score = sum(scores) / len(scores) if scores else None
        return summarize(hours, score, dt_util.DEFAULT_TIME_ZONE), score

    @property
    def native_value(self) -> str:
        return self._summary()[0]["text"]

    @property
    def extra_state_attributes(self) -> dict:
        info, score = self._summary()
        return {
            "confidence_label": info["label"],
            "confidence_score": None if score is None else round(score),
            "low": info.get("low"),
            "high": info.get("high"),
            "sky": info.get("sky"),
        }


class NextRain(_Base):
    """When rain is next expected (for alerts); unknown when none is due in 24 h."""

    _attr_name = "Next rain"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:weather-rainy"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "next_rain")

    @property
    def native_value(self) -> datetime | None:
        wet = first_rain(_upcoming(self.coordinator.data.hours)[:24])
        return wet.time if wet else None

    @property
    def extra_state_attributes(self) -> dict:
        hours = _upcoming(self.coordinator.data.hours)[:24]
        peak = max((h.precipitation_probability or 0 for h in hours), default=0)
        return {"peak_probability_24h": round(peak)}


class SourceTrust(_Base):
    """What was learned about each source's accuracy at this location."""

    _attr_name = "Source trust"
    _attr_icon = "mdi:scale-balance"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "source_trust")

    @property
    def native_value(self) -> str:
        data = self.coordinator.data
        if data.observation is None:
            return "no observations nearby"
        return "calibrated" if data.learned_samples >= MIN_SAMPLES * 4 else "learning"

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        return {
            "observation": data.observation,
            "samples": data.learned_samples,
            "sources": data.trust,
        }


class ForecastError(_Base):
    """How far the fused forecast is from reality (0-24 h, learned)."""

    _attr_name = "Forecast error"
    _attr_icon = "mdi:target"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = "measurement"
    _unrecorded_attributes = frozenset({"scorecard"})

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "forecast_error")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.accuracy.get("fused_mae")

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        fused, base = data.accuracy.get("fused_mae"), data.accuracy.get("consensus_mae")
        return {
            "equal_vote_error": base,
            "improvement_c": round(base - fused, 2) if fused is not None and base is not None else None,
            "bias_correction": data.bias_mode,
            "scorecard": data.scorecard,
        }


class RainSkill(_Base):
    """How good the fused rain chance is, compared with guessing the usual rain frequency."""

    _attr_name = "Rain forecast skill"
    _attr_icon = "mdi:weather-pouring"
    _attr_native_unit_of_measurement = PERCENTAGE
    _unrecorded_attributes = frozenset({"sources"})

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "rain_skill")

    @property
    def native_value(self) -> int | None:
        skill = self.coordinator.data.rain.get("rain_skill")
        return None if skill is None else round(100 * skill)

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        return {
            "brier_score": data.rain.get("brier"),
            "wet_share": data.rain.get("wet_share"),
            "note": "100 = perfect, 0 = no better than the usual rain frequency",
            "sources": data.rain_scorecard,
        }


class DataStatus(_Base):
    """Is the data fresh, and are all providers healthy?"""

    _attr_name = "Data status"
    _attr_icon = "mdi:cloud-check-outline"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "data_status")

    @property
    def native_value(self) -> str:
        data = self.coordinator.data
        if data.stale:
            return "stale"
        return "degraded" if any(not p["ok"] for p in data.providers.values()) else "fresh"

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        age = (
            round((datetime.now(timezone.utc) - data.fetched_at).total_seconds() / 60)
            if data.fetched_at
            else None
        )
        return {
            "updated": data.fetched_at.isoformat() if data.fetched_at else None,
            "age_minutes": age,
            "providers": data.providers,
        }


class Location(_Base):
    """Where the forecast is for. Exposes lat/lon so a map card can plot it."""

    _attr_name = "Location"
    _attr_icon = "mdi:map-marker"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "location")

    @property
    def native_value(self) -> str:
        d = self.coordinator.data
        return d.place_name or d.place or f"{d.latitude:.2f}, {d.longitude:.2f}"

    @property
    def extra_state_attributes(self) -> dict:
        d = self.coordinator.data
        return {
            "zone": d.place,
            "latitude": d.latitude,
            "longitude": d.longitude,
            "source": d.source,
        }


class MemberTemperature(_Base):
    """One source's own temperature, for overlaying against the fused line."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator, entry, member: str):
        super().__init__(coordinator, entry, f"member_{member}")
        self._member = member
        self._attr_name = f"{member.replace(':', ' ')} temperature"

    @property
    def available(self) -> bool:
        return super().available and self._member in self.coordinator.data.series

    @property
    def native_value(self) -> float | None:
        points = _upcoming(self.coordinator.data.series.get(self._member, []))
        return points[0].temperature if points else None

    @property
    def extra_state_attributes(self) -> dict:
        points = _upcoming(self.coordinator.data.series.get(self._member, []))
        return {
            "forecast": [
                {"datetime": p.time.isoformat(), "temperature": p.temperature}
                for p in points
            ]
        }
