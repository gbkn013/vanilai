"""Constants for Vanilai."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "vanilai"

CONF_LOCATION_ENTITY = "location_entity"
CONF_MOVE_THRESHOLD_KM = "move_threshold_km"
CONF_REVERSE_GEOCODE = "reverse_geocode"
CONF_LOCATION_NAME = "location_name"
CONF_EXTRA_WEATHER = "extra_weather_entities"
CONF_OBS_ENTITY = "observation_entity"

DEFAULT_MOVE_THRESHOLD_KM = 2.0
# Weather models refresh every 1-6 h, so hourly polling loses nothing.
UPDATE_INTERVAL = timedelta(minutes=60)
# If every source fails, keep serving the last good forecast this long.
STALE_AFTER = timedelta(hours=6)



def device_info(entry):
    """Shared device so every entity gets a clean `vanilai_*` entity id."""
    from homeassistant.helpers.device_registry import DeviceEntryType
    from homeassistant.helpers.entity import DeviceInfo

    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Vanilai",
        manufacturer="Vanilai",
        entry_type=DeviceEntryType.SERVICE,
    )


# Coordinates are rounded before being sent to third parties (~1 km).
COORD_PRECISION = 2
