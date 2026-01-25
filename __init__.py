"""The ESP32 Bulb Relay integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import aiohttp_client
import homeassistant.helpers.config_validation as cv

from .api import ESP32BulbRelayApi
from .const import (
    ATTR_BULB_NAME,
    ATTR_ESP32_HOST,
    CONF_BULBS,
    CONF_ESP32_HOSTS,
    DOMAIN,
    SERVICE_CONNECT_BULB,
    SERVICE_DISCONNECT_BULB,
    SERVICE_REFRESH_BULBS,
)
from .coordinator import ESP32BulbRelayCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LIGHT]

# Service schemas
SERVICE_CONNECT_DISCONNECT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ESP32_HOST): cv.string,
        vol.Required(ATTR_BULB_NAME): cv.string,
    }
)

SERVICE_REFRESH_SCHEMA = vol.Schema({})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ESP32 Bulb Relay from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    
    # Get ESP32 hosts from config
    esp32_hosts = entry.data.get(CONF_ESP32_HOSTS, [])
    
    # Create coordinator
    coordinator = ESP32BulbRelayCoordinator(hass, esp32_hosts)
    
    # Initialize APIs for each host
    session = aiohttp_client.async_get_clientsession(hass)
    for host in esp32_hosts:
        api = ESP32BulbRelayApi(host, session=session)
        coordinator._apis[host] = api
        
        # Set enabled bulbs from options
        bulb_config = entry.options.get(CONF_BULBS, {})
        enabled_bulbs = set(bulb_config.get(host, []))
        coordinator.set_enabled_bulbs(host, enabled_bulbs)
    
    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()
    
    # Store coordinator
    hass.data[DOMAIN][entry.entry_id] = coordinator
    
    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # Register services
    await _async_setup_services(hass)
    
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    
    # Remove services if no more entries
    if not hass.data[DOMAIN]:
        _async_unload_services(hass)
    
    return unload_ok


async def _async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for the integration."""
    
    async def handle_connect_bulb(call: ServiceCall) -> None:
        """Handle connect bulb service call."""
        host = call.data[ATTR_ESP32_HOST]
        bulb_name = call.data[ATTR_BULB_NAME]
        
        _LOGGER.info("Debug: Connecting bulb %s on %s", bulb_name, host)
        
        for entry_id, coordinator in hass.data[DOMAIN].items():
            if isinstance(coordinator, ESP32BulbRelayCoordinator):
                if host in coordinator.esp32_hosts:
                    try:
                        await coordinator.async_connect_bulb(host, bulb_name)
                        _LOGGER.info("Successfully connected bulb %s", bulb_name)
                        return
                    except Exception as err:
                        _LOGGER.error("Failed to connect bulb %s: %s", bulb_name, err)
                        raise
        
        _LOGGER.error("ESP32 host %s not found", host)

    async def handle_disconnect_bulb(call: ServiceCall) -> None:
        """Handle disconnect bulb service call."""
        host = call.data[ATTR_ESP32_HOST]
        bulb_name = call.data[ATTR_BULB_NAME]
        
        _LOGGER.info("Debug: Disconnecting bulb %s on %s", bulb_name, host)
        
        for entry_id, coordinator in hass.data[DOMAIN].items():
            if isinstance(coordinator, ESP32BulbRelayCoordinator):
                if host in coordinator.esp32_hosts:
                    try:
                        await coordinator.async_disconnect_bulb(host, bulb_name)
                        _LOGGER.info("Successfully disconnected bulb %s", bulb_name)
                        return
                    except Exception as err:
                        _LOGGER.error("Failed to disconnect bulb %s: %s", bulb_name, err)
                        raise
        
        _LOGGER.error("ESP32 host %s not found", host)

    async def handle_refresh_bulbs(call: ServiceCall) -> None:
        """Handle refresh bulbs service call."""
        _LOGGER.info("Refreshing all bulbs")
        
        for entry_id, coordinator in hass.data[DOMAIN].items():
            if isinstance(coordinator, ESP32BulbRelayCoordinator):
                await coordinator.async_refresh()

    # Only register services if not already registered
    if not hass.services.has_service(DOMAIN, SERVICE_CONNECT_BULB):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CONNECT_BULB,
            handle_connect_bulb,
            schema=SERVICE_CONNECT_DISCONNECT_SCHEMA,
        )
    
    if not hass.services.has_service(DOMAIN, SERVICE_DISCONNECT_BULB):
        hass.services.async_register(
            DOMAIN,
            SERVICE_DISCONNECT_BULB,
            handle_disconnect_bulb,
            schema=SERVICE_CONNECT_DISCONNECT_SCHEMA,
        )
    
    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH_BULBS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REFRESH_BULBS,
            handle_refresh_bulbs,
            schema=SERVICE_REFRESH_SCHEMA,
        )


def _async_unload_services(hass: HomeAssistant) -> None:
    """Unload services."""
    hass.services.async_remove(DOMAIN, SERVICE_CONNECT_BULB)
    hass.services.async_remove(DOMAIN, SERVICE_DISCONNECT_BULB)
    hass.services.async_remove(DOMAIN, SERVICE_REFRESH_BULBS)
