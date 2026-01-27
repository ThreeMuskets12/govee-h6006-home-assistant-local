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


class ESP32BulbRelayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ESP32 Bulb Relay."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_bulbs: list[dict[str, Any]] = []
        self._serial_port: str = ""
        self._available_ports: list[dict[str, str]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step - Select serial port."""
        errors: dict[str, str] = {}

        # Get available serial ports
        self._available_ports = await list_serial_ports()
        
        if not self._available_ports:
            return self.async_abort(reason="no_serial_ports")

        if user_input is not None:
            port = user_input[CONF_SERIAL_PORT]
            self._serial_port = port

            # Check if already configured
            await self.async_set_unique_id(f"esp32_bulb_relay_{port.replace('/', '_')}")
            self._abort_if_unique_id_configured()

            # Test connection and get bulbs
            api = ESP32BulbRelaySerialApi(port)

            try:
                await api.connect()
                bulbs = await api.get_bulbs()
                await api.close()
                
                if not bulbs:
                    errors["base"] = "no_bulbs"
                else:
                    self._discovered_bulbs = bulbs
                    return await self.async_step_select_bulbs()
            except ESP32BulbRelayApiError as err:
                _LOGGER.error("Connection error: %s", err)
                errors["base"] = "cannot_connect"
            finally:
                try:
                    await api.close()
                except Exception:
                    pass

        # Build port selection options
        port_options = {p["device"]: p["name"] for p in self._available_ports}

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

            return self.async_create_entry(
                title=f"ESP32 Bulb Relay ({self._serial_port})",
                data={
                    CONF_SERIAL_PORTS: [self._serial_port],
                },
                options={
                    CONF_BULBS: {
                        self._serial_port: selected_bulbs,
                    },
                },
            )

        return self.async_show_form(
            step_id="select_bulbs",
            data_schema=self._get_bulb_selection_schema(),
        )

    def _get_bulb_selection_schema(self) -> vol.Schema:
        """Get schema for bulb selection.
        
        Bulb format from API: {"id": 0, "name": "lamp", "address": "...", "connected": true}
        """
        bulb_names = [bulb["name"] for bulb in self._discovered_bulbs if "name" in bulb]
        
        # Create dict for multi_select: {value: label}
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
                "add_esp32": "Add ESP32",
                "remove_esp32": "Remove ESP32",
                "debug_commands": "Debug Commands (Connect/Disconnect)",
            },
        )

    async def async_step_manage_bulbs(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage which bulbs are enabled/disabled."""
        errors: dict[str, str] = {}
        
        ports = self._config_entry.data.get(CONF_SERIAL_PORTS, [])
        current_bulbs = dict(self._config_entry.options.get(CONF_BULBS, {}))
        
        if user_input is not None:
            # Update bulb selections for each port
            new_bulbs = {}
            for port in ports:
                key = f"bulbs_{port.replace('/', '_').replace('.', '_')}"
                if key in user_input:
                    new_bulbs[port] = user_input[key]
                else:
                    new_bulbs[port] = current_bulbs.get(port, [])
            
            return self.async_create_entry(
                title="",
                data={
                    **self._config_entry.options,
                    CONF_BULBS: new_bulbs,
                },
            )

        # Build schema with bulbs for each port
        schema_dict = {}
        
        for port in ports:
            api = ESP32BulbRelaySerialApi(port)
            try:
                await api.connect()
                bulbs = await api.get_bulbs()
                await api.close()
                
                bulb_names = [bulb["name"] for bulb in bulbs if "name" in bulb]
                
                current_selection = current_bulbs.get(port, bulb_names)
                key = f"bulbs_{port.replace('/', '_').replace('.', '_')}"
                
                if bulb_names:
                    bulb_options = {name: name for name in bulb_names}
                    schema_dict[
                        vol.Optional(key, default=current_selection)
                    ] = cv.multi_select(bulb_options)
            except ESP32BulbRelayApiError:
                _LOGGER.warning("Could not connect to %s", port)
                errors[f"bulbs_{port}"] = "cannot_connect"
            finally:
                try:
                    await api.close()
                except Exception:
                    pass

        if not schema_dict:
            return self.async_abort(reason="no_ports")

        return self.async_show_form(
            step_id="manage_bulbs",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={
                "ports": ", ".join(ports),
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
            
            # Update options with new bulbs
            current_bulbs = dict(self._config_entry.options.get(CONF_BULBS, {}))
            current_bulbs[self._port_to_add] = selected_bulbs

            # Update the config entry data
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
            
            # Update config entry
            ports.remove(port_to_remove)
            current_bulbs = dict(self._config_entry.options.get(CONF_BULBS, {}))
            if port_to_remove in current_bulbs:
                del current_bulbs[port_to_remove]

            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data={
                    **self._config_entry.data,
                    CONF_SERIAL_PORTS: ports,
                },
            )

            return self.async_create_entry(
                title="",
                data={
                    **self._config_entry.options,
                    CONF_BULBS: current_bulbs,
                },
            )

        # Create friendly names for ports
        port_options = {port: port for port in ports}

        return self.async_show_form(
            step_id="remove_esp32",
            data_schema=vol.Schema(
                {
                    vol.Required("port_to_remove"): vol.In(port_options),
                }
            ),
        )

    async def async_step_debug_commands(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Debug commands menu for connect/disconnect."""
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
        
        ports = self._config_entry.data.get(CONF_SERIAL_PORTS, [])
        bulb_options = self._config_entry.options.get(CONF_BULBS, {})
        
        # Build list of all bulbs with their ports
        all_bulbs: dict[str, str] = {}  # display_name -> "port|bulb_name"
        for port in ports:
            bulbs = bulb_options.get(port, [])
            for bulb in bulbs:
                display = f"{bulb} ({port})"
                all_bulbs[display] = f"{port}|{bulb}"

        if not all_bulbs:
            return self.async_abort(reason="no_bulbs")

        if user_input is not None:
            selected = user_input["bulb"]
            port_bulb = all_bulbs.get(selected, "")
            
            if "|" in port_bulb:
                port, bulb_name = port_bulb.split("|", 1)
                api = ESP32BulbRelaySerialApi(port)

                try:
                    await api.connect()
                    if action == "connect":
                        await api.connect_bulb(bulb_name)
                    else:
                        await api.disconnect_bulb(bulb_name)
                    await api.close()
                    
                    return self.async_create_entry(title="", data=self._config_entry.options)
                except ESP32BulbRelayApiError:
                    errors["base"] = "command_failed"
                finally:
                    try:
                        await api.close()
                    except Exception:
                        pass

        step_id = f"debug_{action}"
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required("bulb"): vol.In(list(all_bulbs.keys())),
                }
            ),
            errors=errors,
            description_placeholders={
                "action": action.capitalize(),
            },
        )
