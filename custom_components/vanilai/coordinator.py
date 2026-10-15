"""Data update coordinator: resolve location, fan out to providers, fuse, learn."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from homeassistant.components import zone
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_EXTRA_WEATHER,
    CONF_LOCATION_ENTITY,
    CONF_LOCATION_NAME,
    CONF_MOVE_THRESHOLD_KM,
    CONF_OBS_ENTITY,
    CONF_REVERSE_GEOCODE,
    COORD_PRECISION,
    DEFAULT_MOVE_THRESHOLD_KM,
    DOMAIN,
    STALE_AFTER,
    UPDATE_INTERVAL,
)
from .fusion.aggregate import fuse
from .fusion.confidence import TempStats, temperature_stats
from .fusion.dedupe import drop_duplicates
from .fusion.verify import SkillTracker
from .geo import distance_km
from .geocode import reverse_geocode
from .observations import adjust_to_elevation, fetch_metar, sensor_observation
from .providers.base import HourPoint, MemberForecast, Provider
from .providers.ha_entities import HaEntitiesProvider
from .providers.met_no import MetNoProvider
from .providers.nws import NwsProvider
from .providers.open_meteo import OpenMeteoProvider

_LOGGER = logging.getLogger(__name__)

RETRY_DELAY_S = 2
BACKOFF_AFTER_FAILURES = 3
MAX_BACKOFF = timedelta(hours=2)
SAVE_DELAY_S = 60


@dataclass
class ProviderHealth:
    """Tracks a provider so a broken one is skipped instead of hammered."""

    failures: int = 0
    last_ok: datetime | None = None
    last_error: str | None = None
    retry_at: datetime | None = None

    def ok(self, now: datetime) -> None:
        self.failures, self.last_error, self.retry_at, self.last_ok = 0, None, None, now

    def fail(self, now: datetime, error: str) -> None:
        self.failures += 1
        self.last_error = error
        if self.failures >= BACKOFF_AFTER_FAILURES:
            wait = min(timedelta(minutes=5 * 2 ** (self.failures - BACKOFF_AFTER_FAILURES)), MAX_BACKOFF)
            self.retry_at = now + wait

    def skipping(self, now: datetime) -> bool:
        return self.retry_at is not None and now < self.retry_at

    def as_dict(self) -> dict:
        return {
            "ok": self.failures == 0,
            "failures": self.failures,
            "last_ok": self.last_ok.isoformat() if self.last_ok else None,
            "last_error": self.last_error,
        }


@dataclass
class FusedData:
    """Result published by the coordinator."""

    hours: list[HourPoint] = field(default_factory=list)
    members: list[str] = field(default_factory=list)
    series: dict[str, list[HourPoint]] = field(default_factory=dict)
    stats: dict[datetime, TempStats] = field(default_factory=dict)
    latitude: float = 0.0
    longitude: float = 0.0
    place: str | None = None  # name of the HA zone we are inside, if any
    place_name: str | None = None  # reverse-geocoded "City, Region"
    source: str = ""  # what supplied the location
    fetched_at: datetime | None = None
    stale: bool = False
    providers: dict[str, dict] = field(default_factory=dict)
    trust: dict[str, dict] = field(default_factory=dict)
    observation: dict | None = None
    learned_samples: int = 0
    scorecard: dict[str, dict] = field(default_factory=dict)
    accuracy: dict[str, float | None] = field(default_factory=dict)
    rain_scorecard: dict[str, dict] = field(default_factory=dict)
    rain: dict[str, float | None] = field(default_factory=dict)
    bias_mode: str = "relative"


RECHECK_S = 45


class VanilaiCoordinator(DataUpdateCoordinator[FusedData]):
    """Fetches all enabled providers, fuses them, and learns from observations."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.entry = entry
        session = async_get_clientsession(hass)
        self._session = session
        self.providers: list[Provider | HaEntitiesProvider] = [
            OpenMeteoProvider(session),
            MetNoProvider(session),
            NwsProvider(session),  # returns nothing outside the US
        ]
        registry = er.async_get(hass)
        extra = [
            e for e in (self._opt(CONF_EXTRA_WEATHER) or [])
            # Fusing our own output back into itself would be circular.
            if not ((reg := registry.async_get(e)) and reg.platform == DOMAIN)
        ]
        if extra:
            self.providers.append(HaEntitiesProvider(hass, extra))
        self.health: dict[str, ProviderHealth] = {}
        self.tracker = SkillTracker()
        self._store: Store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._last_fetched: tuple[float, float] | None = None
        self._geo_cache: tuple[tuple[float, float], str] | None = None
        self._last_good: FusedData | None = None

    async def _recheck(self, _now: datetime) -> None:
        await self.async_request_refresh()

    def _opt(self, key, default=None):
        return self.entry.options.get(key, self.entry.data.get(key, default))

    async def async_load(self) -> None:
        """Restore what was learned before the last restart."""
        if saved := await self._store.async_load():
            self.tracker = SkillTracker(saved)

    # -- location ----------------------------------------------------------

    def resolve_location(self) -> tuple[float, float]:
        """Location from the tracked entity, falling back to HA home."""
        entity_id = self._opt(CONF_LOCATION_ENTITY)
        if entity_id and (state := self.hass.states.get(entity_id)):
            lat = state.attributes.get("latitude")
            lon = state.attributes.get("longitude")
            if lat is not None and lon is not None:
                return float(lat), float(lon)
        return self.hass.config.latitude, self.hass.config.longitude

    def location_source(self) -> str:
        """Human-readable origin of the location currently in use."""
        entity_id = self._opt(CONF_LOCATION_ENTITY)
        if entity_id and (state := self.hass.states.get(entity_id)):
            if (
                state.attributes.get("latitude") is not None
                and state.attributes.get("longitude") is not None
            ):
                return state.name
        return "Home Assistant home"

    @callback
    def async_start_tracking(self) -> None:
        """Refresh early when the tracked entity moves past the threshold."""
        entity_id = self._opt(CONF_LOCATION_ENTITY)
        if not entity_id:
            return

        @callback
        def _moved(_: Event) -> None:
            if self._last_fetched is None:
                return
            lat, lon = self.resolve_location()
            threshold = self._opt(CONF_MOVE_THRESHOLD_KM, DEFAULT_MOVE_THRESHOLD_KM)
            if distance_km(lat, lon, *self._last_fetched) >= threshold:
                self.hass.async_create_task(self.async_request_refresh())

        self.entry.async_on_unload(
            async_track_state_change_event(self.hass, [entity_id], _moved)
        )

    async def _async_place_name(self, lat: float, lon: float) -> str | None:
        """User-set name, else reverse-geocode once per rounded location."""
        override = self._opt(CONF_LOCATION_NAME)
        if override and override.strip():
            return override.strip()
        if not self._opt(CONF_REVERSE_GEOCODE, True):
            return None
        if self._geo_cache and self._geo_cache[0] == (lat, lon):
            return self._geo_cache[1]
        name = await reverse_geocode(self._session, lat, lon)
        if name:
            self._geo_cache = ((lat, lon), name)
        return name

    # -- fetching ----------------------------------------------------------

    async def _fetch_one(
        self, provider: Provider | HaEntitiesProvider, lat: float, lon: float, now: datetime
    ) -> list[MemberForecast]:
        health = self.health.setdefault(provider.key, ProviderHealth())
        if health.skipping(now):
            _LOGGER.debug("Skipping %s until %s (backing off)", provider.key, health.retry_at)
            return []
        error = "unknown"
        for attempt in range(2):
            try:
                result = await provider.fetch(lat, lon)
            except Exception as err:  # noqa: BLE001 - a provider bug must not kill the rest
                error = f"{type(err).__name__}: {err}"
                if attempt == 0:
                    await asyncio.sleep(RETRY_DELAY_S)
                continue
            health.ok(now)
            return result
        health.fail(now, error)
        _LOGGER.warning("Provider %s failed: %s", provider.key, error)
        return []

    async def _async_observations(
        self, lat: float, lon: float, now: datetime
    ) -> tuple[dict | None, dict[int, float], dict[int, bool]]:
        """Real temperatures and rain reports to check forecasts against.

        Your own temperature sensor wins for temperature. A station's
        temperatures are shifted to your elevation so errors are absolute.
        Rain always comes from the station's present-weather reports.
        """
        obs = await fetch_metar(self._session, lat, lon)
        wet = obs.wet if obs else {}
        if entity_id := self._opt(CONF_OBS_ENTITY):
            state = self.hass.states.get(entity_id)
            temps = sensor_observation(state.state if state else None, now)
            return ({"station": entity_id, "name": "Your temperature sensor", "distance_km": 0.0}, temps, wet)
        if obs is None:
            return None, {}, {}
        temps = adjust_to_elevation(obs.temps, obs.elevation_m, self.hass.config.elevation)
        return (
            {"station": obs.station, "name": obs.name, "distance_km": obs.distance_km},
            temps,
            wet,
        )

    # -- update ------------------------------------------------------------

    async def _async_update_data(self) -> FusedData:
        now = dt_util.utcnow()
        lat, lon = self.resolve_location()
        # Privacy: only ~1 km precision ever leaves Home Assistant.
        rlat, rlon = round(lat, COORD_PRECISION), round(lon, COORD_PRECISION)

        results = await asyncio.gather(
            *(self._fetch_one(p, rlat, rlon, now) for p in self.providers)
        )
        members: list[MemberForecast] = [m for r in results for m in r]
        if any(getattr(p, "pending", False) for p in self.providers):
            # Other integrations may still be starting up: look again shortly.
            async_call_later(self.hass, RECHECK_S, self._recheck)
        members, copies = drop_duplicates(members)
        if copies:
            _LOGGER.debug("Ignored duplicate sources: %s", "; ".join(copies))

        if not members:
            # Every source failed: keep serving the last good forecast, flagged.
            if self._last_good and (now - self._last_good.fetched_at) < STALE_AFTER:
                _LOGGER.warning("No provider answered; serving the last good forecast")
                return replace(
                    self._last_good,
                    stale=True,
                    providers={k: h.as_dict() for k, h in self.health.items()},
                )
            raise UpdateFailed("No weather provider returned data")

        # Learn: score earlier forecasts against what was really observed,
        # then store this update's forecasts for future scoring.
        obs_info, obs_temps, obs_wet = await self._async_observations(rlat, rlon, now)
        self.tracker.note_members(members, now)
        # Your own sensor measures this spot, so absolute bias can be learned
        # from it; a distant station only supports bias relative to the others.
        absolute = bool(self._opt(CONF_OBS_ENTITY)) and bool(obs_temps)
        if obs_temps:
            self.tracker.score(obs_temps, now, lon=rlon, absolute=absolute)
        if obs_wet:
            self.tracker.score_rain(obs_wet, now)
        corrected = self.tracker.corrected(members, now, rlon, absolute)
        fused_hours = fuse(
            corrected,
            now=now,
            skill=lambda member, lead: self.tracker.weight(member, lead, now),
        )
        # Snapshots keep the raw forecasts: bias is learned from what sources said.
        self.tracker.record_snapshot(members, now, fused_hours)
        self._store.async_delay_save(self.tracker.to_dict, SAVE_DELAY_S)

        self._last_fetched = (lat, lon)
        zone_state = zone.async_active_zone(self.hass, lat, lon)
        data = FusedData(
            hours=fused_hours,
            members=[m.member for m in members],
            series={m.member: m.hours for m in members},
            stats=temperature_stats(corrected),
            latitude=rlat,
            longitude=rlon,
            place=zone_state.name if zone_state else None,
            place_name=await self._async_place_name(rlat, rlon),
            source=self.location_source(),
            fetched_at=now,
            providers={k: h.as_dict() for k, h in self.health.items()},
            trust=self.tracker.summary(now),
            scorecard=self.tracker.scorecard(),
            accuracy=self.tracker.headline(),
            rain_scorecard=self.tracker.rain_scorecard(),
            rain=self.tracker.rain_headline(),
            bias_mode="absolute" if absolute else "relative",
            observation=obs_info,
            learned_samples=self.tracker.samples_total,
        )
        self._last_good = data
        return data
