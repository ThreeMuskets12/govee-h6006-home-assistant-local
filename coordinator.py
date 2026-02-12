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
    """Coordinator to manage fetching data from ESP32 Bulb Relays.
    
    This coordinator handles dynamic port assignment - bulbs are identified by name,
    not by which port they're connected to. On each update, we scan all configured
    ports and build a mapping of bulb_name -> port.
    """

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
        self._serial_ports = list(serial_ports)  # Configured ports to scan
        self._apis: dict[str, ESP32BulbRelaySerialApi] = {}  # port -> API client
        self._bulb_port_map: dict[str, str] = {}  # bulb_name -> port (dynamic mapping)
        self._bulb_data: dict[str, dict[str, Any]] = {}  # bulb_name -> bulb info
        self._enabled_bulbs: set[str] = set()  # Bulb names user has enabled
        self._port_online: dict[str, bool] = {}  # port -> online status

    @property
    def serial_ports(self) -> list[str]:
        """Return the list of configured serial ports."""
        return self._serial_ports.copy()

    @property
    def bulb_port_map(self) -> dict[str, str]:
        """Return the current bulb -> port mapping."""
        return self._bulb_port_map.copy()

    def get_api_for_bulb(self, bulb_name: str) -> ESP32BulbRelaySerialApi | None:
        """Get the API client for a specific bulb based on current mapping."""
        port = self._bulb_port_map.get(bulb_name)
        if port:
            return self._apis.get(port)
        return None

    def get_api(self, port: str) -> ESP32BulbRelaySerialApi | None:
        """Get the API client for a specific port."""
        return self._apis.get(port)

    def get_bulb_port(self, bulb_name: str) -> str | None:
        """Get the current port for a bulb."""
        return self._bulb_port_map.get(bulb_name)

    def set_enabled_bulbs(self, bulb_names: set[str]) -> None:
        """Set which bulbs are enabled."""
        self._enabled_bulbs = set(bulb_names)

    def get_enabled_bulbs(self) -> set[str]:
        """Get the enabled bulbs."""
        return self._enabled_bulbs.copy()

    def is_bulb_enabled(self, bulb_name: str) -> bool:
        """Check if a bulb is enabled."""
        return bulb_name in self._enabled_bulbs

    def add_serial_port(self, port: str) -> None:
        """Add a new serial port to scan."""
        if port not in self._serial_ports:
            self._serial_ports.append(port)

    def remove_serial_port(self, port: str) -> None:
        """Remove a serial port."""
        if port in self._serial_ports:
            self._serial_ports.remove(port)
        if port in self._apis:
            # Don't await close here, just remove reference
            self._apis.pop(port, None)
        self._port_online.pop(port, None)

    def enable_bulb(self, bulb_name: str) -> None:
        """Enable a bulb."""
        self._enabled_bulbs.add(bulb_name)

    def disable_bulb(self, bulb_name: str) -> None:
        """Disable a bulb."""
        self._enabled_bulbs.discard(bulb_name)

    async def async_scan_port(self, port: str) -> list[dict[str, Any]]:
        """Scan a single port and return discovered bulbs.
        
        Returns list of bulb dicts: {"id": 0, "name": "lamp", "address": "...", "connected": true}
        """
        if port not in self._apis:
            api = ESP32BulbRelaySerialApi(port)
            self._apis[port] = api
        
        api = self._apis[port]
        
        try:
            if not api.is_connected:
                await api.connect()
            
            bulbs = await api.get_bulbs()
            self._port_online[port] = True
            return bulbs
        except ESP32BulbRelayApiError as err:
            _LOGGER.warning("Failed to scan port %s: %s", port, err)
            self._port_online[port] = False
            return []

    async def async_rescan_all_ports(self) -> dict[str, str]:
        """Rescan all configured ports and rebuild bulb->port mapping.
        
        Returns the new bulb->port mapping.
        """
        new_mapping: dict[str, str] = {}
        new_bulb_data: dict[str, dict[str, Any]] = {}
        
        for port in self._serial_ports:
            try:
                bulbs = await self.async_scan_port(port)
                
                for bulb in bulbs:
                    bulb_name = bulb.get("name")
                    if bulb_name:
                        new_mapping[bulb_name] = port
                        new_bulb_data[bulb_name] = bulb
                        
            except Exception as err:
                _LOGGER.error("Error scanning port %s: %s", port, err)
        
        # Update the mappings
        old_mapping = self._bulb_port_map
        self._bulb_port_map = new_mapping
        self._bulb_data = new_bulb_data
        
        # Log any changes
        for bulb_name, new_port in new_mapping.items():
            old_port = old_mapping.get(bulb_name)
            if old_port and old_port != new_port:
                _LOGGER.info(
                    "Bulb '%s' moved from %s to %s",
                    bulb_name, old_port, new_port
                )
        
        # Log bulbs that were lost
        for bulb_name in old_mapping:
            if bulb_name not in new_mapping and bulb_name in self._enabled_bulbs:
                _LOGGER.warning(
                    "Bulb '%s' no longer found on any port",
                    bulb_name
                )
        
        return new_mapping

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from all ESP32s and update bulb->port mapping."""
        # Rescan all ports to update mapping
        await self.async_rescan_all_ports()
        
        # Build data structure for entities
        data: dict[str, Any] = {
            "bulbs": {},
            "ports": {},
        }
        
        # Add port status
        for port in self._serial_ports:
            data["ports"][port] = {
                "online": self._port_online.get(port, False),
            }
        
        # Add bulb data with current port info
        for bulb_name, bulb_info in self._bulb_data.items():
            port = self._bulb_port_map.get(bulb_name)
            data["bulbs"][bulb_name] = {
                **bulb_info,
                "current_port": port,
                "port_online": self._port_online.get(port, False) if port else False,
            }
        
        return data

    async def async_send_command(
        self,
        bulb_name: str,
        command_func: str,
        *args,
        **kwargs,
    ) -> Any:
        """Send a command to a bulb, with auto-rescan on failure.
        
        Args:
            bulb_name: Name of the bulb
            command_func: Name of the API method to call (e.g., "turn_on", "set_brightness")
            *args, **kwargs: Arguments to pass to the command
            
        Returns:
            Result from the command
            
        Raises:
            ESP32BulbRelayApiError: If command fails even after rescan
        """
        api = self.get_api_for_bulb(bulb_name)
        
        if api is None:
            # Bulb not found, try rescanning
            _LOGGER.debug("Bulb '%s' not found, rescanning ports...", bulb_name)
            await self.async_rescan_all_ports()
            api = self.get_api_for_bulb(bulb_name)
            
            if api is None:
                raise ESP32BulbRelayApiError(
                    f"Bulb '{bulb_name}' not found on any configured port"
                )
        
        # Get the method to call
        method = getattr(api, command_func, None)
        if method is None:
            raise ESP32BulbRelayApiError(f"Unknown command: {command_func}")
        
        try:
            return await method(bulb_name, *args, **kwargs)
        except ESP32BulbRelayApiError:
            # Command failed, maybe bulb moved to different port
            _LOGGER.debug(
                "Command '%s' failed for bulb '%s', rescanning ports...",
                command_func, bulb_name
            )
            await self.async_rescan_all_ports()
            
            # Try again with potentially new port
            api = self.get_api_for_bulb(bulb_name)
            if api is None:
                raise ESP32BulbRelayApiError(
                    f"Bulb '{bulb_name}' not found on any configured port after rescan"
                )
            
            method = getattr(api, command_func)
            return await method(bulb_name, *args, **kwargs)

    async def async_connect_bulb(self, bulb_name: str) -> dict[str, Any]:
        """Connect a bulb (debug function)."""
        return await self.async_send_command(bulb_name, "connect_bulb")

    async def async_disconnect_bulb(self, bulb_name: str) -> dict[str, Any]:
        """Disconnect a bulb (debug function)."""
        return await self.async_send_command(bulb_name, "disconnect_bulb")

    async def async_shutdown(self) -> None:
        """Shutdown all API connections."""
        for api in self._apis.values():
            try:
                await api.close()
            except Exception:
                pass
        self._apis.clear()
        self._bulb_port_map.clear()
        self._bulb_data.clear()
