"""Config flow for ESP32 Bulb Relay integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from .api import ESP32BulbRelaySerialApi, ESP32BulbRelayApiError, list_serial_ports
from .const import (
    CONF_BULBS,
    CONF_SERIAL_PORT,
    CONF_SERIAL_PORTS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Shorter timeout for setup/test connections
SETUP_TIMEOUT = 15


class ESP32BulbRelayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ESP32 Bulb Relay."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_bulbs: list[dict[str, Any]] = []
        self._serial_port: str = ""
        self._available_ports: list[dict[str, str]] = []
        _LOGGER.debug("Config flow initialized")

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step - Select serial port."""
        errors: dict[str, str] = {}

        _LOGGER.debug("async_step_user called with input: %s", user_input)

        # Get available serial ports
        _LOGGER.debug("Listing serial ports...")
        self._available_ports = await list_serial_ports()
        _LOGGER.info("Found %d serial ports: %s", 
                    len(self._available_ports), 
                    [p["device"] for p in self._available_ports])
        
        if not self._available_ports:
            _LOGGER.warning("No serial ports found")
            return self.async_abort(reason="no_serial_ports")

        if user_input is not None:
            port = user_input[CONF_SERIAL_PORT]
            self._serial_port = port
            _LOGGER.info("User selected port: %s", port)

            # Test connection and get bulbs
            api = ESP32BulbRelaySerialApi(port, timeout=SETUP_TIMEOUT)

            try:
                _LOGGER.info("Attempting to connect to %s...", port)
                await api.connect()
                _LOGGER.info("Connected to %s, querying bulbs...", port)
                
                # Use shorter timeout for initial query
                bulbs = await api.get_bulbs(timeout=SETUP_TIMEOUT)
                _LOGGER.info("Got bulbs from %s: %s", port, bulbs)
                
                await api.close()
                
                if not bulbs:
                    _LOGGER.warning("No bulbs found on %s", port)
                    errors["base"] = "no_bulbs"
                else:
                    _LOGGER.info("Found %d bulbs on %s", len(bulbs), port)
                    self._discovered_bulbs = bulbs
                    return await self.async_step_select_bulbs()
                    
            except ESP32BulbRelayApiError as err:
                _LOGGER.error("Connection error on %s: %s", port, err)
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected error connecting to %s: %s", port, err)
                errors["base"] = "cannot_connect"
            finally:
                try:
                    await api.close()
                except Exception as close_err:
                    _LOGGER.debug("Error closing API: %s", close_err)

        # Build port selection options
        port_options = {p["device"]: p["name"] for p in self._available_ports}
        _LOGGER.debug("Showing port selection form with options: %s", list(port_options.keys()))

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SERIAL_PORT): vol.In(port_options),
                }
            ),
            errors=errors,
        )

    async def async_step_select_bulbs(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let user select which bulbs to add."""
        if user_input is not None:
            selected_bulbs = user_input.get(CONF_BULBS, [])
            
            if not selected_bulbs:
                return self.async_show_form(
                    step_id="select_bulbs",
                    data_schema=self._get_bulb_selection_schema(),
                    errors={"base": "no_bulbs_selected"},
                )

            # Create unique ID based on first port (will scan all ports dynamically)
            await self.async_set_unique_id(f"esp32_bulb_relay")
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title="ESP32 Bulb Relay",
                data={
                    # Store ports as a list to scan
                    CONF_SERIAL_PORTS: [self._serial_port],
                },
                options={
                    # Store bulbs as flat list (not grouped by port)
                    CONF_BULBS: selected_bulbs,
                },
            )

        return self.async_show_form(
            step_id="select_bulbs",
            data_schema=self._get_bulb_selection_schema(),
        )

    def _get_bulb_selection_schema(self) -> vol.Schema:
        """Get schema for bulb selection."""
        bulb_names = [bulb["name"] for bulb in self._discovered_bulbs if "name" in bulb]
        bulb_options = {name: name for name in bulb_names}

        return vol.Schema(
            {
                vol.Required(CONF_BULBS, default=bulb_names): cv.multi_select(bulb_options),
            }
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ESP32BulbRelayOptionsFlow:
        """Get the options flow for this handler."""
        return ESP32BulbRelayOptionsFlow(config_entry)


class ESP32BulbRelayOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for ESP32 Bulb Relay."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._port_to_add: str = ""
        self._discovered_bulbs: list[dict[str, Any]] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options - main menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options={
                "manage_bulbs": "Manage Bulbs",
                "add_esp32": "Add ESP32 Port",
                "remove_esp32": "Remove ESP32 Port",
                "rescan_ports": "Rescan All Ports",
                "debug_commands": "Debug Commands",
            },
        )

    async def async_step_manage_bulbs(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage which bulbs are enabled/disabled."""
        errors: dict[str, str] = {}
        
        ports = self._config_entry.data.get(CONF_SERIAL_PORTS, [])
        current_bulbs = set(self._config_entry.options.get(CONF_BULBS, []))
        
        if user_input is not None:
            selected_bulbs = user_input.get(CONF_BULBS, [])
            
            return self.async_create_entry(
                title="",
                data={
                    **self._config_entry.options,
                    CONF_BULBS: selected_bulbs,
                },
            )

        # Scan all ports to get available bulbs
        all_bulbs: dict[str, str] = {}  # bulb_name -> port (for display)
        
        for port in ports:
            api = ESP32BulbRelaySerialApi(port)
            try:
                await api.connect()
                bulbs = await api.get_bulbs()
                await api.close()
                
                for bulb in bulbs:
                    bulb_name = bulb.get("name")
                    if bulb_name:
                        all_bulbs[bulb_name] = port
            except ESP32BulbRelayApiError:
                _LOGGER.warning("Could not connect to %s", port)
            finally:
                try:
                    await api.close()
                except Exception:
                    pass

        if not all_bulbs:
            return self.async_abort(reason="no_bulbs_found")

        # Create options with port info in label
        bulb_options = {name: f"{name} ({port})" for name, port in all_bulbs.items()}
        
        # Default to currently enabled bulbs that still exist
        default_selection = [b for b in current_bulbs if b in all_bulbs]

        return self.async_show_form(
            step_id="manage_bulbs",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BULBS, default=default_selection): cv.multi_select(bulb_options),
                }
            ),
            errors=errors,
            description_placeholders={
                "ports": ", ".join(ports),
                "bulb_count": str(len(all_bulbs)),
            },
        )

    async def async_step_add_esp32(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Add a new ESP32 serial port."""
        errors: dict[str, str] = {}
        
        # Get available serial ports
        available_ports = await list_serial_ports()
        existing_ports = list(self._config_entry.data.get(CONF_SERIAL_PORTS, []))
        
        # Filter out already configured ports
        new_ports = [p for p in available_ports if p["device"] not in existing_ports]
        
        if not new_ports:
            return self.async_abort(reason="no_new_serial_ports")

        if user_input is not None:
            port = user_input[CONF_SERIAL_PORT]
            self._port_to_add = port

            if port in existing_ports:
                errors["base"] = "already_configured"
            else:
                # Test connection
                api = ESP32BulbRelaySerialApi(port)

                try:
                    await api.connect()
                    bulbs = await api.get_bulbs()
                    await api.close()
                    
                    if bulbs:
                        self._discovered_bulbs = bulbs
                        return await self.async_step_select_new_bulbs()
                    else:
                        errors["base"] = "no_bulbs"
                except ESP32BulbRelayApiError:
                    errors["base"] = "cannot_connect"
                finally:
                    try:
                        await api.close()
                    except Exception:
                        pass

        # Build port selection options
        port_options = {p["device"]: p["name"] for p in new_ports}

        return self.async_show_form(
            step_id="add_esp32",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SERIAL_PORT): vol.In(port_options),
                }
            ),
            errors=errors,
        )

    async def async_step_select_new_bulbs(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Select bulbs from newly added ESP32."""
        if user_input is not None:
            selected_bulbs = user_input.get(CONF_BULBS, [])
            
            # Update config entry with new port
            existing_ports = list(self._config_entry.data.get(CONF_SERIAL_PORTS, []))
            existing_ports.append(self._port_to_add)
            
            # Add new bulbs to existing list
            current_bulbs = list(self._config_entry.options.get(CONF_BULBS, []))
            for bulb in selected_bulbs:
                if bulb not in current_bulbs:
                    current_bulbs.append(bulb)

            # Update the config entry data with new port
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data={
                    **self._config_entry.data,
                    CONF_SERIAL_PORTS: existing_ports,
                },
            )

            return self.async_create_entry(
                title="",
                data={
                    **self._config_entry.options,
                    CONF_BULBS: current_bulbs,
                },
            )

        # Build selection schema
        bulb_names = [bulb["name"] for bulb in self._discovered_bulbs if "name" in bulb]
        bulb_options = {name: name for name in bulb_names}

        return self.async_show_form(
            step_id="select_new_bulbs",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BULBS, default=bulb_names): cv.multi_select(bulb_options),
                }
            ),
            description_placeholders={
                "port": self._port_to_add,
                "count": str(len(bulb_names)),
            },
        )

    async def async_step_remove_esp32(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Remove an ESP32 serial port."""
        ports = list(self._config_entry.data.get(CONF_SERIAL_PORTS, []))
        
        if not ports:
            return self.async_abort(reason="no_ports")
        
        if len(ports) == 1:
            return self.async_abort(reason="cannot_remove_last")

        if user_input is not None:
            port_to_remove = user_input["port_to_remove"]
            
            # Update config entry - remove port
            ports.remove(port_to_remove)

            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data={
                    **self._config_entry.data,
                    CONF_SERIAL_PORTS: ports,
                },
            )

            # Note: We don't remove bulbs - they might be on another port
            # or the port might come back. The coordinator handles unavailable bulbs.
            
            return self.async_create_entry(title="", data=self._config_entry.options)

        port_options = {port: port for port in ports}

        return self.async_show_form(
            step_id="remove_esp32",
            data_schema=vol.Schema(
                {
                    vol.Required("port_to_remove"): vol.In(port_options),
                }
            ),
        )

    async def async_step_rescan_ports(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Rescan all ports to find bulbs."""
        ports = self._config_entry.data.get(CONF_SERIAL_PORTS, [])
        current_bulbs = set(self._config_entry.options.get(CONF_BULBS, []))
        
        # Scan all ports
        found_bulbs: dict[str, str] = {}  # bulb_name -> port
        port_status: dict[str, str] = {}  # port -> status
        
        for port in ports:
            api = ESP32BulbRelaySerialApi(port)
            try:
                await api.connect()
                bulbs = await api.get_bulbs()
                await api.close()
                
                port_status[port] = f"Online ({len(bulbs)} bulbs)"
                for bulb in bulbs:
                    bulb_name = bulb.get("name")
                    if bulb_name:
                        found_bulbs[bulb_name] = port
            except ESP32BulbRelayApiError as err:
                port_status[port] = f"Offline: {err}"
            finally:
                try:
                    await api.close()
                except Exception:
                    pass
        
        # Check which enabled bulbs were found
        found_enabled = [b for b in current_bulbs if b in found_bulbs]
        missing_enabled = [b for b in current_bulbs if b not in found_bulbs]
        new_discovered = [b for b in found_bulbs if b not in current_bulbs]
        
        # Build status message
        status_lines = ["**Port Status:**"]
        for port, status in port_status.items():
            status_lines.append(f"- {port}: {status}")
        
        status_lines.append("")
        status_lines.append(f"**Enabled bulbs found:** {len(found_enabled)}")
        if missing_enabled:
            status_lines.append(f"**Missing bulbs:** {', '.join(missing_enabled)}")
        if new_discovered:
            status_lines.append(f"**New bulbs discovered:** {', '.join(new_discovered)}")
        
        # Just show results and return
        return self.async_abort(
            reason="rescan_complete",
            description_placeholders={
                "status": "\n".join(status_lines),
            },
        )

    async def async_step_debug_commands(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Debug commands menu."""
        return self.async_show_menu(
            step_id="debug_commands",
            menu_options={
                "debug_connect": "Connect Bulb",
                "debug_disconnect": "Disconnect Bulb",
            },
        )

    async def async_step_debug_connect(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Connect a bulb (debug)."""
        return await self._async_step_debug_action(user_input, "connect")

    async def async_step_debug_disconnect(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Disconnect a bulb (debug)."""
        return await self._async_step_debug_action(user_input, "disconnect")

    async def _async_step_debug_action(
        self, user_input: dict[str, Any] | None, action: str
    ) -> FlowResult:
        """Handle debug connect/disconnect action."""
        errors: dict[str, str] = {}
        
        enabled_bulbs = self._config_entry.options.get(CONF_BULBS, [])
        
        if not enabled_bulbs:
            return self.async_abort(reason="no_bulbs")

        if user_input is not None:
            bulb_name = user_input["bulb"]
            
            # Find which port has this bulb
            ports = self._config_entry.data.get(CONF_SERIAL_PORTS, [])
            target_port = None
            
            for port in ports:
                api = ESP32BulbRelaySerialApi(port)
                try:
                    await api.connect()
                    bulbs = await api.get_bulbs()
                    bulb_names = [b.get("name") for b in bulbs]
                    if bulb_name in bulb_names:
                        target_port = port
                        # Execute command
                        if action == "connect":
                            await api.connect_bulb(bulb_name)
                        else:
                            await api.disconnect_bulb(bulb_name)
                        await api.close()
                        return self.async_create_entry(title="", data=self._config_entry.options)
                except ESP32BulbRelayApiError:
                    pass
                finally:
                    try:
                        await api.close()
                    except Exception:
                        pass
            
            if target_port is None:
                errors["base"] = "bulb_not_found"
            else:
                errors["base"] = "command_failed"

        bulb_options = {name: name for name in enabled_bulbs}
        step_id = f"debug_{action}"
        
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required("bulb"): vol.In(bulb_options),
                }
            ),
            errors=errors,
        )
