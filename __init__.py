"""The ESP32 Bulb Relay integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv

from .api import ESP32BulbRelaySerialApi
from .const import (
    ATTR_BULB_NAME,
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
    
    # Create coordinator
    coordinator = ESP32BulbRelayCoordinator(hass, serial_ports)
    coordinator.set_enabled_bulbs(enabled_bulbs)
    
    # Initialize API connections for each port
    for port in serial_ports:
        api = ESP32BulbRelaySerialApi(port)
        coordinator._apis[port] = api
        
        try:
            await api.connect()
        except Exception as err:
            _LOGGER.warning("Failed to connect to %s: %s", port, err)
    
    # Do initial scan to build bulb->port mapping
    await coordinator.async_rescan_all_ports()
    
    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()
    
    # Store coordinator
    hass.data[DOMAIN][entry.entry_id] = coordinator
    
    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # Register services
    await _async_setup_services(hass)
    
    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    coordinator: ESP32BulbRelayCoordinator = hass.data[DOMAIN][entry.entry_id]
    
    # Update enabled bulbs
    enabled_bulbs = set(entry.options.get(CONF_BULBS, []))
    coordinator.set_enabled_bulbs(enabled_bulbs)
    
    # Update serial ports if changed
    new_ports = entry.data.get(CONF_SERIAL_PORTS, [])
    current_ports = coordinator.serial_ports
    
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
