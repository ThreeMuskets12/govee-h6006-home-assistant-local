"""Light platform for ESP32 Bulb Relay."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ESP32BulbRelayApi, ESP32BulbRelayApiError
from .const import (
    CONF_BULBS,
    CONF_ESP32_HOSTS,
    DOMAIN,
    MAX_COLOR_TEMP_KELVIN,
    MIN_COLOR_TEMP_KELVIN,
)
from .coordinator import ESP32BulbRelayCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ESP32 Bulb Relay lights from a config entry."""
    coordinator: ESP32BulbRelayCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    
    hosts = config_entry.data.get(CONF_ESP32_HOSTS, [])
    bulb_config = config_entry.options.get(CONF_BULBS, {})
    
    entities: list[ESP32BulbRelayLight] = []
    
    for host in hosts:
        enabled_bulbs = bulb_config.get(host, [])
        api = coordinator.get_api(host)
        
        if api is None:
            continue
        
        for bulb_name in enabled_bulbs:
            entities.append(
                ESP32BulbRelayLight(
                    coordinator=coordinator,
                    host=host,
                    bulb_name=bulb_name,
                    api=api,
                )
            )
    
    async_add_entities(entities)

    # Register listener for options updates
    config_entry.async_on_unload(
        config_entry.add_update_listener(async_options_updated)
    )


async def async_options_updated(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(config_entry.entry_id)


class ESP32BulbRelayLight(CoordinatorEntity[ESP32BulbRelayCoordinator], LightEntity):
    """Representation of an ESP32 Bulb Relay light."""

    _attr_has_entity_name = True
    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
    _attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN

    def __init__(
        self,
        coordinator: ESP32BulbRelayCoordinator,
        host: str,
        bulb_name: str,
        api: ESP32BulbRelayApi,
    ) -> None:
        """Initialize the light."""
        super().__init__(coordinator)
        
        self._host = host
        self._bulb_name = bulb_name
        self._api = api
        
        # Entity attributes
        self._attr_unique_id = f"{host}_{bulb_name}".replace(".", "_").replace(" ", "_")
        self._attr_name = bulb_name
        
        # Device info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{host}_{bulb_name}")},
            name=f"{bulb_name}",
            manufacturer="ESP32 Bulb Relay",
            model="Smart Bulb",
            via_device=(DOMAIN, host),
        )
        
        # State tracking
        self._attr_is_on = False
        self._attr_brightness = 255
        self._attr_rgb_color = (255, 255, 255)
        self._attr_color_temp_kelvin = 4000
        self._attr_color_mode = ColorMode.COLOR_TEMP
        self._attr_available = True

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if not self.coordinator.last_update_success:
            return False
        
        if self.coordinator.data is None:
            return False
        
        host_data = self.coordinator.data.get("hosts", {}).get(self._host, {})
        if not host_data.get("online", False):
            return False
        
        # Check if this specific bulb is connected
        bulbs = host_data.get("bulbs", [])
        for bulb in bulbs:
            if bulb.get("name") == self._bulb_name:
                return bulb.get("connected", True)
        
        return False

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator.
        
        Bulb format from API: {"id": 0, "name": "lamp", "address": "...", "connected": true}
        """
        if self.coordinator.data is None:
            return
        
        host_data = self.coordinator.data.get("hosts", {}).get(self._host, {})
        bulbs = host_data.get("bulbs", [])
        
        # Find this bulb in the data by name
        for bulb in bulbs:
            if bulb.get("name") == self._bulb_name:
                # Store bulb metadata
                self._attr_extra_state_attributes = {
                    "bulb_id": bulb.get("id"),
                    "address": bulb.get("address"),
                    "connected": bulb.get("connected", False),
                    "esp32_host": self._host,
                }
                break
        
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        try:
            # Handle RGB color
            if ATTR_RGB_COLOR in kwargs:
                r, g, b = kwargs[ATTR_RGB_COLOR]
                await self._api.set_rgb(self._bulb_name, r, g, b)
                self._attr_rgb_color = (r, g, b)
                self._attr_color_mode = ColorMode.RGB
            
            # Handle color temperature
            elif ATTR_COLOR_TEMP_KELVIN in kwargs:
                temp = kwargs[ATTR_COLOR_TEMP_KELVIN]
                await self._api.set_temperature(self._bulb_name, temp)
                self._attr_color_temp_kelvin = temp
                self._attr_color_mode = ColorMode.COLOR_TEMP
            
            # Handle brightness
            if ATTR_BRIGHTNESS in kwargs:
                # Convert 0-255 to 0-100
                brightness_pct = int(kwargs[ATTR_BRIGHTNESS] * 100 / 255)
                await self._api.set_brightness(self._bulb_name, brightness_pct)
                self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
            
            # Turn on if not already adjusting other properties
            if not any(k in kwargs for k in [ATTR_RGB_COLOR, ATTR_COLOR_TEMP_KELVIN, ATTR_BRIGHTNESS]):
                await self._api.turn_on(self._bulb_name)
            elif ATTR_RGB_COLOR not in kwargs and ATTR_COLOR_TEMP_KELVIN not in kwargs:
                # If only brightness was set, also turn on
                await self._api.turn_on(self._bulb_name)
            
            self._attr_is_on = True
            self.async_write_ha_state()
            
        except ESP32BulbRelayApiError as err:
            _LOGGER.error("Failed to turn on %s: %s", self._bulb_name, err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        try:
            await self._api.turn_off(self._bulb_name)
            self._attr_is_on = False
            self.async_write_ha_state()
        except ESP32BulbRelayApiError as err:
            _LOGGER.error("Failed to turn off %s: %s", self._bulb_name, err)
