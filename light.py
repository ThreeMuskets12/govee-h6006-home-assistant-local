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
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ESP32BulbRelayApiError
from .const import (
    CONF_BULBS,
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
    
    # Get enabled bulbs (flat list)
    enabled_bulbs = config_entry.options.get(CONF_BULBS, [])
    
    entities: list[ESP32BulbRelayLight] = []
    
    for bulb_name in enabled_bulbs:
        entities.append(
            ESP32BulbRelayLight(
                coordinator=coordinator,
                bulb_name=bulb_name,
            )
        )
    
    async_add_entities(entities)


class ESP32BulbRelayLight(CoordinatorEntity[ESP32BulbRelayCoordinator], LightEntity):
    """Representation of an ESP32 Bulb Relay light.
    
    This entity is identified by bulb name only, not by port.
    The coordinator handles dynamic port mapping.
    """

    _attr_has_entity_name = True
    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
    _attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN

    def __init__(
        self,
        coordinator: ESP32BulbRelayCoordinator,
        bulb_name: str,
    ) -> None:
        """Initialize the light."""
        super().__init__(coordinator)
        
        self._bulb_name = bulb_name
        
        # Entity attributes - use only bulb name for unique ID
        self._attr_unique_id = f"esp32_bulb_{bulb_name}".replace(" ", "_").replace("/", "_")
        self._attr_name = bulb_name
        
        # Device info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"bulb_{bulb_name}")},
            name=bulb_name,
            manufacturer="ESP32 Bulb Relay",
            model="Smart Bulb",
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
        
        # Check if bulb is found on any port
        bulb_data = self.coordinator.data.get("bulbs", {}).get(self._bulb_name)
        if bulb_data is None:
            return False
        
        # Check if the port it's on is online
        if not bulb_data.get("port_online", False):
            return False
        
        # Check if the bulb itself is connected
        return bulb_data.get("connected", True)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data is None:
            return
        
        bulb_data = self.coordinator.data.get("bulbs", {}).get(self._bulb_name)
        
        if bulb_data:
            current_port = bulb_data.get("current_port", "unknown")
            self._attr_extra_state_attributes = {
                "bulb_id": bulb_data.get("id"),
                "address": bulb_data.get("address"),
                "connected": bulb_data.get("connected", False),
                "current_port": current_port,
            }
        else:
            self._attr_extra_state_attributes = {
                "connected": False,
                "current_port": "not found",
            }
        
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        try:
            # Handle RGB color
            if ATTR_RGB_COLOR in kwargs:
                r, g, b = kwargs[ATTR_RGB_COLOR]
                await self.coordinator.async_send_command(
                    self._bulb_name, "set_rgb", r, g, b
                )
                self._attr_rgb_color = (r, g, b)
                self._attr_color_mode = ColorMode.RGB
            
            # Handle color temperature
            elif ATTR_COLOR_TEMP_KELVIN in kwargs:
                temp = kwargs[ATTR_COLOR_TEMP_KELVIN]
                await self.coordinator.async_send_command(
                    self._bulb_name, "set_temperature", temp
                )
                self._attr_color_temp_kelvin = temp
                self._attr_color_mode = ColorMode.COLOR_TEMP
            
            # Handle brightness
            if ATTR_BRIGHTNESS in kwargs:
                # Convert 0-255 to 0-100
                brightness_pct = int(kwargs[ATTR_BRIGHTNESS] * 100 / 255)
                await self.coordinator.async_send_command(
                    self._bulb_name, "set_brightness", brightness_pct
                )
                self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
            
            # Turn on if not already adjusting other properties
            if not any(k in kwargs for k in [ATTR_RGB_COLOR, ATTR_COLOR_TEMP_KELVIN, ATTR_BRIGHTNESS]):
                await self.coordinator.async_send_command(
                    self._bulb_name, "turn_on"
                )
            elif ATTR_RGB_COLOR not in kwargs and ATTR_COLOR_TEMP_KELVIN not in kwargs:
                # If only brightness was set, also turn on
                await self.coordinator.async_send_command(
                    self._bulb_name, "turn_on"
                )
            
            self._attr_is_on = True
            self.async_write_ha_state()
            
        except ESP32BulbRelayApiError as err:
            _LOGGER.error("Failed to turn on %s: %s", self._bulb_name, err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        try:
            await self.coordinator.async_send_command(
                self._bulb_name, "turn_off"
            )
            self._attr_is_on = False
            self.async_write_ha_state()
        except ESP32BulbRelayApiError as err:
            _LOGGER.error("Failed to turn off %s: %s", self._bulb_name, err)
