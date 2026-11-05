"""The ESP32 Bulb Relay integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntry

from .api import ESP32BulbRelaySerialApi
from .const import (
    ATTR_BULB_NAME,
    BULB_DEVICE_PREFIX,
    CONF_BULBS,
    CONF_SERIAL_PORTS,
    DOMAIN,
    SERVICE_CONNECT_BULB,
    SERVICE_DISCONNECT_BULB,
    SERVICE_REFRESH_BULBS,
    SERVICE_RESCAN_PORTS,
)
from .coordinator import ESP32BulbRelayCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LIGHT]

# Service schemas
SERVICE_BULB_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_BULB_NAME): cv.string,
    }
)

SERVICE_REFRESH_SCHEMA = vol.Schema({})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ESP32 Bulb Relay from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    
    # Get serial ports from config
    serial_ports = entry.data.get(CONF_SERIAL_PORTS, [])
    
    # Get enabled bulbs from options (flat list)
    enabled_bulbs = set(entry.options.get(CONF_BULBS, []))
    
    _LOGGER.info(
        "Setting up ESP32 Bulb Relay with %d ports and %d enabled bulbs",
        len(serial_ports),
        len(enabled_bulbs)
    )
    
    # Create coordinator
    coordinator = ESP32BulbRelayCoordinator(hass, serial_ports)
    coordinator.set_enabled_bulbs(enabled_bulbs)
    
    # Initialize API connections for each port
    # Note: ESP32s may be booting and connecting to BLE bulbs, so we don't
    # fail if initial connection doesn't work - the coordinator will retry
    for port in serial_ports:
        api = ESP32BulbRelaySerialApi(port)
        coordinator._apis[port] = api
        
        try:
            _LOGGER.info("Connecting to %s...", port)
            await api.connect()
            _LOGGER.info("Connected to %s", port)
        except Exception as err:
            # Don't fail setup - ESP32 might still be booting
            _LOGGER.warning(
                "Could not connect to %s on startup (ESP32 may still be booting): %s",
                port, err
            )
    
    # Try initial scan, but don't fail if it doesn't work
    # ESP32s may need time to finish their BLE connections
    _LOGGER.info("Attempting initial port scan (ESP32s may still be booting)...")
    try:
        await coordinator.async_rescan_all_ports()
    except Exception as err:
        _LOGGER.warning("Initial scan incomplete: %s (will retry)", err)
    
    # Store coordinator BEFORE first refresh so entities can be created
    hass.data[DOMAIN][entry.entry_id] = coordinator
    
    # Do first refresh - if it fails, that's okay, coordinator will retry
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.warning(
            "First data refresh failed (ESP32s may still be booting): %s",
            err
        )
        # Don't raise - let the coordinator retry on its schedule
    
    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # Register services
    await _async_setup_services(hass)
    
    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    
    _LOGGER.info("ESP32 Bulb Relay setup complete (background updates will continue)")
    
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    coordinator: ESP32BulbRelayCoordinator = hass.data[DOMAIN][entry.entry_id]
    
    enabled_bulbs = set(entry.options.get(CONF_BULBS, []))
    new_ports = entry.data.get(CONF_SERIAL_PORTS, [])
    current_ports = coordinator.serial_ports
    
    # Device removal applies its change directly. Reloading would reopen every serial
    # port, which can reset the ESP32s and drop all of their bulbs.
    if enabled_bulbs == coordinator.get_enabled_bulbs() and set(new_ports) == set(current_ports):
        return
    
    # Update enabled bulbs
    coordinator.set_enabled_bulbs(enabled_bulbs)
    
    # Update serial ports if changed
    # Add new ports
    for port in new_ports:
        if port not in current_ports:
            coordinator.add_serial_port(port)
            api = ESP32BulbRelaySerialApi(port)
            coordinator._apis[port] = api
            try:
                await api.connect()
            except Exception as err:
                _LOGGER.warning("Failed to connect to new port %s: %s", port, err)
    
    # Remove old ports
    for port in current_ports:
        if port not in new_ports:
            coordinator.remove_serial_port(port)
    
    # Rescan to update bulb->port mapping
    await coordinator.async_rescan_all_ports()
    
    # Reload the integration to recreate entities
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Allow removing a device once no board reports its bulb."""
    coordinator: ESP32BulbRelayCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is None:
        _LOGGER.warning("Can't remove %s: integration not loaded", device_entry.name)
        return False
    
    bulb_names = {
        identifier.removeprefix(BULB_DEVICE_PREFIX)
        for domain, identifier in device_entry.identifiers
        if domain == DOMAIN and identifier.startswith(BULB_DEVICE_PREFIX)
    }
    
    # Ask the boards now: coordinator.data can be a poll interval old, and a board
    # that doesn't answer could be the one holding this bulb
    reported = await coordinator.async_rescan_all_ports()
    if not coordinator.all_ports_online:
        _LOGGER.warning("Can't remove %s: not every ESP32 answered", device_entry.name)
        return False
    
    if present := bulb_names & reported.keys():
        _LOGGER.warning("Can't remove %s: reported by an ESP32", ", ".join(sorted(present)))
        return False
    
    enabled_bulbs = list(entry.options.get(CONF_BULBS, []))
    remaining_bulbs = [bulb for bulb in enabled_bulbs if bulb not in bulb_names]
    if remaining_bulbs != enabled_bulbs:
        coordinator.set_enabled_bulbs(set(remaining_bulbs))
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_BULBS: remaining_bulbs}
        )
    
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    if unload_ok:
        coordinator: ESP32BulbRelayCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    
    # Remove services if no more entries
    if not hass.data[DOMAIN]:
        _async_unload_services(hass)
    
    return unload_ok


async def _async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for the integration."""
    
    async def handle_connect_bulb(call: ServiceCall) -> None:
        """Handle connect bulb service call."""
        bulb_name = call.data[ATTR_BULB_NAME]
        
        _LOGGER.info("Debug: Connecting bulb %s", bulb_name)
        
        for entry_id, coordinator in hass.data[DOMAIN].items():
            if isinstance(coordinator, ESP32BulbRelayCoordinator):
                try:
                    await coordinator.async_connect_bulb(bulb_name)
                    _LOGGER.info("Successfully connected bulb %s", bulb_name)
                    return
                except Exception as err:
                    _LOGGER.error("Failed to connect bulb %s: %s", bulb_name, err)
                    raise
        
        _LOGGER.error("No coordinator found for bulb %s", bulb_name)

    async def handle_disconnect_bulb(call: ServiceCall) -> None:
        """Handle disconnect bulb service call."""
        bulb_name = call.data[ATTR_BULB_NAME]
        
        _LOGGER.info("Debug: Disconnecting bulb %s", bulb_name)
        
        for entry_id, coordinator in hass.data[DOMAIN].items():
            if isinstance(coordinator, ESP32BulbRelayCoordinator):
                try:
                    await coordinator.async_disconnect_bulb(bulb_name)
                    _LOGGER.info("Successfully disconnected bulb %s", bulb_name)
                    return
                except Exception as err:
                    _LOGGER.error("Failed to disconnect bulb %s: %s", bulb_name, err)
                    raise
        
        _LOGGER.error("No coordinator found for bulb %s", bulb_name)

    async def handle_refresh_bulbs(call: ServiceCall) -> None:
        """Handle refresh bulbs service call."""
        _LOGGER.info("Refreshing all bulbs")
        
        for entry_id, coordinator in hass.data[DOMAIN].items():
            if isinstance(coordinator, ESP32BulbRelayCoordinator):
                await coordinator.async_refresh()

    async def handle_rescan_ports(call: ServiceCall) -> None:
        """Handle rescan ports service call."""
        _LOGGER.info("Rescanning all ports")
        
        for entry_id, coordinator in hass.data[DOMAIN].items():
            if isinstance(coordinator, ESP32BulbRelayCoordinator):
                mapping = await coordinator.async_rescan_all_ports()
                _LOGGER.info("Port rescan complete. Bulb mapping: %s", mapping)
                await coordinator.async_refresh()

    # Register services if not already registered
    if not hass.services.has_service(DOMAIN, SERVICE_CONNECT_BULB):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CONNECT_BULB,
            handle_connect_bulb,
            schema=SERVICE_BULB_SCHEMA,
        )
    
    if not hass.services.has_service(DOMAIN, SERVICE_DISCONNECT_BULB):
        hass.services.async_register(
            DOMAIN,
            SERVICE_DISCONNECT_BULB,
            handle_disconnect_bulb,
            schema=SERVICE_BULB_SCHEMA,
        )
    
    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH_BULBS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REFRESH_BULBS,
            handle_refresh_bulbs,
            schema=SERVICE_REFRESH_SCHEMA,
        )
    
    if not hass.services.has_service(DOMAIN, SERVICE_RESCAN_PORTS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RESCAN_PORTS,
            handle_rescan_ports,
            schema=SERVICE_REFRESH_SCHEMA,
        )


def _async_unload_services(hass: HomeAssistant) -> None:
    """Unload services."""
    hass.services.async_remove(DOMAIN, SERVICE_CONNECT_BULB)
    hass.services.async_remove(DOMAIN, SERVICE_DISCONNECT_BULB)
    hass.services.async_remove(DOMAIN, SERVICE_REFRESH_BULBS)
    hass.services.async_remove(DOMAIN, SERVICE_RESCAN_PORTS)
