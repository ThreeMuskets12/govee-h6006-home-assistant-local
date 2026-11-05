"""Data coordinator for ESP32 Bulb Relay."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ESP32BulbRelayApiError,
    ESP32BulbRelayBulbNotFoundError,
    ESP32BulbRelayCommandError,
    ESP32BulbRelayOutOfSyncError,
    ESP32BulbRelaySerialApi,
    ESP32BulbRelayTimeoutError,
)
from .const import (
    DOMAIN,
    MAX_MISSED_SCANS,
    POLL_TIMEOUT,
    RECOVERY_BOOT_TIMEOUT,
    RECOVERY_BOOT_WAIT,
    RECOVERY_HOLD,
    RESET_AFTER_MISSED_SCANS,
    RESET_BACKOFF_INITIAL,
    RESET_BACKOFF_MAX,
    RESET_MIN_SILENCE,
    UPDATE_INTERVAL,
)

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
        self._missed_scans: dict[str, int] = {}  # port -> consecutive failed scans
        self._resets: dict[str, tuple[int, float]] = {}  # port -> (failures, next allowed at)
        self._recovery_task: asyncio.Task | None = None  # At most one, across all ports
        self._closing = False

    @property
    def serial_ports(self) -> list[str]:
        """Return the list of configured serial ports."""
        return self._serial_ports.copy()

    @property
    def all_ports_online(self) -> bool:
        """Return True if every configured port answered its last scan."""
        return all(self._port_online.get(port, False) for port in self._serial_ports)

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
        self._missed_scans.pop(port, None)
        self._resets.pop(port, None)

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
        _LOGGER.debug("Scanning port %s", port)
        
        if port not in self._apis:
            _LOGGER.debug("Creating new API client for port %s", port)
            api = ESP32BulbRelaySerialApi(port)
            self._apis[port] = api
        
        api = self._apis[port]

        # Its own recovery reports back; meanwhile the port must be left alone
        if api.recovering:
            _LOGGER.debug("Skipping port %s: recovery in progress", port)
            self._port_online[port] = False
            return []

        try:
            if not api.is_connected:
                _LOGGER.debug("Connecting to port %s", port)
                await api.connect()
            
            _LOGGER.debug("Querying /bulbs on port %s (timeout=%ds)", port, POLL_TIMEOUT)
            bulbs = await api.get_bulbs(timeout=POLL_TIMEOUT)
            self._port_online[port] = True
            self._missed_scans[port] = 0
            if port in self._resets:
                del self._resets[port]
                _LOGGER.info("ESP32 on %s is answering again after a recovery", port)

            _LOGGER.info(
                "Port %s scan successful: %d bulbs found: %s",
                port,
                len(bulbs),
                [(b.get("name"), b.get("connected")) for b in bulbs]
            )
            
            return bulbs
        except ESP32BulbRelayApiError as err:
            # An outage fails every scan, so only its first gets a warning. For a flood,
            # the API client's own warning is that one.
            missed = self._missed_scans.get(port, 0)
            quiet = missed or isinstance(err, ESP32BulbRelayOutOfSyncError)
            level = logging.DEBUG if quiet else logging.WARNING
            _LOGGER.log(level, "Failed to scan port %s: %s", port, err)
            self._port_online[port] = False
            self._missed_scans[port] = missed + 1
            return []

    async def async_rescan_all_ports(self) -> dict[str, str]:
        """Rescan all configured ports and rebuild bulb->port mapping.
        
        Returns the new bulb->port mapping.
        """
        _LOGGER.debug("Rescanning all %d ports: %s", len(self._serial_ports), self._serial_ports)
        
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
                        _LOGGER.debug(
                            "Found bulb '%s' on port %s (connected=%s)",
                            bulb_name,
                            port,
                            bulb.get("connected")
                        )
                        
            except Exception as err:
                _LOGGER.error("Error scanning port %s: %s", port, err)
                self._port_online[port] = False
                self._missed_scans[port] = self._missed_scans.get(port, 0) + 1
        
        # Update the mappings
        old_mapping = self._bulb_port_map
        
        # A board that missed a scan is busy, not empty: keep routing to its other bulbs
        for bulb_name, port in old_mapping.items():
            if (
                bulb_name not in new_mapping
                and port in self._serial_ports
                and 0 < self._missed_scans.get(port, 0) <= MAX_MISSED_SCANS
            ):
                new_mapping[bulb_name] = port
                new_bulb_data[bulb_name] = self._bulb_data[bulb_name]
        
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
        
        _LOGGER.debug(
            "Rescan complete: bulb_port_map=%s",
            new_mapping
        )

        self._async_start_recovery()

        return new_mapping

    def _troubled_ports(self) -> list[str]:
        """Return the ports whose boards need a recovery.

        A flood is never a busy-but-healthy board, so it counts at once. Plain silence only
        counts past the missed-scans threshold and the silence gate.
        """
        troubled = []
        for port in self.serial_ports:
            api = self._apis.get(port)
            if api is None:
                continue
            if api.out_of_sync or (
                self._missed_scans.get(port, 0) >= RESET_AFTER_MISSED_SCANS
                and api.silent_for(RESET_MIN_SILENCE)
            ):
                troubled.append(port)
        return troubled

    def _async_start_recovery(self) -> None:
        """Start a recovery if a troubled board is due one.

        It runs in the background: it can take a minute per board, healthy ports must keep
        polling meanwhile, and at startup it must not hold up Home Assistant.
        """
        if self._closing or (self._recovery_task and not self._recovery_task.done()):
            return

        now = asyncio.get_running_loop().time()
        troubled = self._troubled_ports()
        # One due board brings the others along: a sibling left looping would drag it back
        if any(now >= self._resets.get(port, (0, 0.0))[1] for port in troubled):
            self._recovery_task = self.hass.async_create_background_task(
                self._async_recover(troubled), name=f"{DOMAIN} coordinated recovery"
            )

    def _recovery_failed(self, port: str) -> int:
        """Count a failed recovery of a port and return the seconds until the next."""
        failures = self._resets.get(port, (0, 0.0))[0]
        backoff = min(RESET_BACKOFF_INITIAL * 2**failures, RESET_BACKOFF_MAX)
        self._resets[port] = (failures + 1, asyncio.get_running_loop().time() + backoff)
        return backoff

    async def _async_hold(self, port: str, api: ESP32BulbRelaySerialApi, min_silence: float) -> bool:
        """Hold one board in reset and return whether it is held.

        Never raises, or the gather below would move on while a sibling is still going in.
        """
        try:
            return await api.hold_in_reset(min_silence)
        except Exception as err:
            _LOGGER.warning(
                "%s; next try no sooner than %d s", err, self._recovery_failed(port)
            )
            return False

    async def _async_recover(self, troubled: list[str]) -> None:
        """Bring troubled boards back one at a time, so that no two boots overlap.

        Overlapping boots can sag the shared 5 V supply enough to reset each other, and that
        loop survives resetting one board at a time because the others keep it going. So
        every troubled board is held in reset first, which also leaves the supply to recover.
        """
        apis = {port: self._apis[port] for port in troubled}
        try:
            held = await asyncio.gather(
                *(
                    self._async_hold(port, api, 0 if api.out_of_sync else RESET_MIN_SILENCE)
                    for port, api in apis.items()
                )
            )
            held_ports = [port for port, is_held in zip(apis, held) if is_held]
            if not held_ports:
                return

            _LOGGER.warning(
                "Coordinated recovery: holding %s in reset, to release one at a time",
                ", ".join(held_ports),
            )
            await asyncio.sleep(RECOVERY_HOLD)

            still_held = []
            for port in held_ports:
                if await apis[port].release_and_wait(RECOVERY_BOOT_TIMEOUT):
                    self._port_online[port] = True
                    self._missed_scans[port] = 0
                    self._resets.pop(port, None)
                    _LOGGER.info("ESP32 on %s released and answering", port)
                    continue

                _LOGGER.warning(
                    "ESP32 on %s did not come up within %d s; next recovery no sooner than %d s",
                    port, RECOVERY_BOOT_TIMEOUT, self._recovery_failed(port)
                )
                if await self._async_hold(port, apis[port], 0):
                    still_held.append(port)

            # A slow board may only need longer than the timeout, so none waits out the
            # backoff in reset. They still go one at a time, as together they could loop.
            for port in still_held:
                await apis[port].release_hold()
                await asyncio.sleep(RECOVERY_BOOT_WAIT)
        finally:
            await asyncio.gather(*(api.release_hold() for api in apis.values()))

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from all ESP32s and update bulb->port mapping.
        
        This method is designed to be resilient:
        - Partial failures (some ports offline) don't fail the whole update
        - We return whatever data we could get
        - Entities use cached data if their bulb wasn't reachable
        """
        _LOGGER.debug("Starting coordinator data update")
        
        # Rescan all ports to update mapping
        # This won't raise - it handles errors internally per-port
        await self.async_rescan_all_ports()
        
        # Build data structure for entities
        data: dict[str, Any] = {
            "bulbs": {},
            "ports": {},
        }
        
        # Add port status
        ports_online = 0
        for port in self._serial_ports:
            online = self._port_online.get(port, False)
            data["ports"][port] = {
                "online": online,
            }
            if online:
                ports_online += 1
            _LOGGER.debug("Port %s: online=%s", port, online)
        
        # Add bulb data with current port info
        for bulb_name, bulb_info in self._bulb_data.items():
            port = self._bulb_port_map.get(bulb_name)
            port_online = self._port_online.get(port, False) if port else False
            is_connected = bulb_info.get("connected", False)
            
            data["bulbs"][bulb_name] = {
                **bulb_info,
                "current_port": port,
                "port_online": port_online,
            }
            
            _LOGGER.debug(
                "Bulb '%s': connected=%s, port=%s, port_online=%s",
                bulb_name,
                is_connected,
                port,
                port_online
            )
        
        _LOGGER.info(
            "Coordinator update complete: %d/%d ports online, %d bulbs tracked",
            ports_online,
            len(self._serial_ports),
            len(data["bulbs"])
        )
        
        # Return data even if some/all ports are offline
        # The entity availability is determined by individual bulb "connected" status
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
        except ESP32BulbRelayBulbNotFoundError:
            pass
        except ESP32BulbRelayTimeoutError:
            # The board is busy; rescanning every port can't help.
            raise
        except ESP32BulbRelayCommandError:
            # The bulb is on this port, so a rescan can't help here either. Older
            # firmware fails healthy bulbs while reconnecting another, so give a bulb
            # that last reported connected one more try.
            if not self._bulb_data.get(bulb_name, {}).get("connected"):
                raise
            return await method(bulb_name, *args, **kwargs)
        except ESP32BulbRelayApiError:
            pass
        
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
        _LOGGER.debug("Shutting down coordinator with %d API connections", len(self._apis))

        # Stopping the recovery lets go of any board it holds
        self._closing = True
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            await asyncio.wait({self._recovery_task}, timeout=5.0)

        for port, api in self._apis.items():
            try:
                _LOGGER.debug("Closing connection to %s", port)
                # Each close has its own internal timeout, but add a wrapper just in case
                await asyncio.wait_for(api.close(), timeout=5.0)
            except asyncio.TimeoutError:
                _LOGGER.warning("Timeout closing connection to %s", port)
            except Exception as err:
                _LOGGER.debug("Error closing %s: %s", port, err)
        
        self._apis.clear()
        self._bulb_port_map.clear()
        self._bulb_data.clear()
        _LOGGER.debug("Coordinator shutdown complete")
