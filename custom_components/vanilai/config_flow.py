"""Config and options flow."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.helpers import selector

from .const import (
    CONF_EXTRA_WEATHER,
    CONF_LOCATION_ENTITY,
    CONF_LOCATION_NAME,
    CONF_MOVE_THRESHOLD_KM,
    CONF_OBS_ENTITY,
    CONF_REVERSE_GEOCODE,
    DEFAULT_MOVE_THRESHOLD_KM,
    DOMAIN,
)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    entity = defaults.get(CONF_LOCATION_ENTITY)
    return vol.Schema(
        {
            vol.Optional(
                CONF_LOCATION_ENTITY,
                **({"description": {"suggested_value": entity}} if entity else {}),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["person", "device_tracker"])
            ),
            vol.Optional(
                CONF_LOCATION_NAME,
                **(
                    {"description": {"suggested_value": defaults[CONF_LOCATION_NAME]}}
                    if defaults.get(CONF_LOCATION_NAME)
                    else {}
                ),
            ): selector.TextSelector(),
            vol.Optional(
                CONF_EXTRA_WEATHER,
                **(
                    {"description": {"suggested_value": defaults[CONF_EXTRA_WEATHER]}}
                    if defaults.get(CONF_EXTRA_WEATHER)
                    else {}
                ),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="weather", multiple=True)
            ),
            vol.Optional(
                CONF_OBS_ENTITY,
                **(
                    {"description": {"suggested_value": defaults[CONF_OBS_ENTITY]}}
                    if defaults.get(CONF_OBS_ENTITY)
                    else {}
                ),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
            vol.Required(
                CONF_REVERSE_GEOCODE,
                default=defaults.get(CONF_REVERSE_GEOCODE, True),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_MOVE_THRESHOLD_KM,
                default=defaults.get(CONF_MOVE_THRESHOLD_KM, DEFAULT_MOVE_THRESHOLD_KM),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.5, max=50, step=0.5, unit_of_measurement="km"
                )
            ),
        }
    )


class VanilaiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance setup: pick an optional tracked entity."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Vanilai", data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VanilaiOptionsFlow()


class VanilaiOptionsFlow(OptionsFlow):
    """Change the tracked entity or threshold after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current))
