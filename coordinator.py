"""Data coordinator for ESP32 Bulb Relay."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ESP32BulbRelayApi, ESP32BulbRelayApiError
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class ESP32BulbRelayCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to manage fetching data from ESP32 Bulb Relays."""

    def __init__(
        self,
        hass: HomeAssistant,
        esp32_hosts: list[str],
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self._esp32_hosts = esp32_hosts
        self._apis: dict[str, ESP32BulbRelayApi] = {}
        self._enabled_bulbs: dict[str, set[str]] = {}  # host -> set of bulb names

    @property
    def esp32_hosts(self) -> list[str]:
        """Return the list of ESP32 hosts."""
        return self._esp32_hosts.copy()

    def get_api(self, host: str) -> ESP32BulbRelayApi | None:
        """Get the API client for a specific host."""
        return self._apis.get(host)

    def set_enabled_bulbs(self, host: str, bulb_names: set[str]) -> None:
        """Set which bulbs are enabled for a host."""
        self._enabled_bulbs[host] = bulb_names

    def get_enabled_bulbs(self, host: str) -> set[str]:
        """Get the enabled bulbs for a host."""
        return self._enabled_bulbs.get(host, set())

    def is_bulb_enabled(self, host: str, bulb_name: str) -> bool:
        """Check if a bulb is enabled."""
        if host not in self._enabled_bulbs:
            return True  # All bulbs enabled by default
        return bulb_name in self._enabled_bulbs[host]

    async def add_esp32_host(self, host: str) -> list[dict[str, Any]]:
        """Add a new ESP32 host and return its bulbs.
        
        Bulb format from API: {"id": 0, "name": "lamp", "address": "...", "connected": true}
        """
        if host not in self._esp32_hosts:
            self._esp32_hosts.append(host)
        
        api = ESP32BulbRelayApi(host, session=self.hass.helpers.aiohttp_client.async_get_clientsession(self.hass))
        self._apis[host] = api
        
        try:
            bulbs = await api.get_bulbs()
            # Enable all bulbs by default - extract names from bulb dicts
            self._enabled_bulbs[host] = {bulb["name"] for bulb in bulbs if "name" in bulb}
            return bulbs
        except ESP32BulbRelayApiError as err:
            raise UpdateFailed(f"Error connecting to {host}: {err}") from err

    async def remove_esp32_host(self, host: str) -> None:
        """Remove an ESP32 host."""
        if host in self._esp32_hosts:
            self._esp32_hosts.remove(host)
        if host in self._apis:
            del self._apis[host]
        if host in self._enabled_bulbs:
            del self._enabled_bulbs[host]

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from all ESP32s.
        
        Bulb format from API: {"id": 0, "name": "lamp", "address": "...", "connected": true}
        """
        data: dict[str, Any] = {"hosts": {}}
        
        for host in self._esp32_hosts:
            if host not in self._apis:
                api = ESP32BulbRelayApi(
                    host, 
                    session=self.hass.helpers.aiohttp_client.async_get_clientsession(self.hass)
                )
                self._apis[host] = api
            
            api = self._apis[host]
            
            try:
                bulbs = await api.get_bulbs()
                data["hosts"][host] = {
                    "online": True,
                    "bulbs": bulbs,
                }
                
                # Initialize enabled bulbs if not set - extract names from bulb dicts
                if host not in self._enabled_bulbs:
                    self._enabled_bulbs[host] = {
                        bulb["name"] for bulb in bulbs if "name" in bulb
                    }
            except ESP32BulbRelayApiError as err:
                _LOGGER.warning("Error fetching data from %s: %s", host, err)
                data["hosts"][host] = {
                    "online": False,
                    "bulbs": [],
                    "error": str(err),
                }
        
        return data

    async def async_connect_bulb(self, host: str, bulb_name: str) -> bool:
        """Connect a bulb (debug function)."""
        api = self._apis.get(host)
        if not api:
            raise ValueError(f"Unknown ESP32 host: {host}")
        return await api.connect(bulb_name)

    async def async_disconnect_bulb(self, host: str, bulb_name: str) -> bool:
        """Disconnect a bulb (debug function)."""
        api = self._apis.get(host)
        if not api:
            raise ValueError(f"Unknown ESP32 host: {host}")
        return await api.disconnect(bulb_name)
