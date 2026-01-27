"""Data coordinator for ESP32 Bulb Relay."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ESP32BulbRelaySerialApi, ESP32BulbRelayApiError
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class ESP32BulbRelayCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to manage fetching data from ESP32 Bulb Relays."""

    def __init__(
        self,
        hass: HomeAssistant,
        serial_ports: list[str],
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self._serial_ports = serial_ports
        self._apis: dict[str, ESP32BulbRelaySerialApi] = {}
        self._enabled_bulbs: dict[str, set[str]] = {}  # port -> set of bulb names

    @property
    def serial_ports(self) -> list[str]:
        """Return the list of serial ports."""
        return self._serial_ports.copy()

    def get_api(self, port: str) -> ESP32BulbRelaySerialApi | None:
        """Get the API client for a specific port."""
        return self._apis.get(port)

    def set_enabled_bulbs(self, port: str, bulb_names: set[str]) -> None:
        """Set which bulbs are enabled for a port."""
        self._enabled_bulbs[port] = bulb_names

    def get_enabled_bulbs(self, port: str) -> set[str]:
        """Get the enabled bulbs for a port."""
        return self._enabled_bulbs.get(port, set())

    def is_bulb_enabled(self, port: str, bulb_name: str) -> bool:
        """Check if a bulb is enabled."""
        if port not in self._enabled_bulbs:
            return True  # All bulbs enabled by default
        return bulb_name in self._enabled_bulbs[port]

    async def add_serial_port(self, port: str) -> list[dict[str, Any]]:
        """Add a new serial port and return its bulbs.
        
        Bulb format from API: {"id": 0, "name": "lamp", "address": "...", "connected": true}
        """
        if port not in self._serial_ports:
            self._serial_ports.append(port)
        
        api = ESP32BulbRelaySerialApi(port)
        self._apis[port] = api
        
        try:
            await api.connect()
            bulbs = await api.get_bulbs()
            # Enable all bulbs by default - extract names from bulb dicts
            self._enabled_bulbs[port] = {bulb["name"] for bulb in bulbs if "name" in bulb}
            return bulbs
        except ESP32BulbRelayApiError as err:
            raise UpdateFailed(f"Error connecting to {port}: {err}") from err

    async def remove_serial_port(self, port: str) -> None:
        """Remove a serial port."""
        if port in self._serial_ports:
            self._serial_ports.remove(port)
        if port in self._apis:
            try:
                await self._apis[port].close()
            except Exception:
                pass
            del self._apis[port]
        if port in self._enabled_bulbs:
            del self._enabled_bulbs[port]

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from all ESP32s.
        
        Bulb format from API: {"id": 0, "name": "lamp", "address": "...", "connected": true}
        """
        data: dict[str, Any] = {"ports": {}}
        
        for port in self._serial_ports:
            if port not in self._apis:
                api = ESP32BulbRelaySerialApi(port)
                self._apis[port] = api
            
            api = self._apis[port]
            
            try:
                if not api.is_connected:
                    await api.connect()
                
                bulbs = await api.get_bulbs()
                data["ports"][port] = {
                    "online": True,
                    "bulbs": bulbs,
                }
                
                # Initialize enabled bulbs if not set - extract names from bulb dicts
                if port not in self._enabled_bulbs:
                    self._enabled_bulbs[port] = {
                        bulb["name"] for bulb in bulbs if "name" in bulb
                    }
            except ESP32BulbRelayApiError as err:
                _LOGGER.warning("Error fetching data from %s: %s", port, err)
                data["ports"][port] = {
                    "online": False,
                    "bulbs": [],
                    "error": str(err),
                }
        
        return data

    async def async_connect_bulb(self, port: str, bulb_name: str) -> bool:
        """Connect a bulb (debug function)."""
        api = self._apis.get(port)
        if not api:
            raise ValueError(f"Unknown serial port: {port}")
        return await api.connect_bulb(bulb_name)

    async def async_disconnect_bulb(self, port: str, bulb_name: str) -> bool:
        """Disconnect a bulb (debug function)."""
        api = self._apis.get(port)
        if not api:
            raise ValueError(f"Unknown serial port: {port}")
        return await api.disconnect_bulb(bulb_name)

    async def async_shutdown(self) -> None:
        """Shutdown all API connections."""
        for api in self._apis.values():
            try:
                await api.close()
            except Exception:
                pass
        self._apis.clear()
