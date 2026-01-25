"""Config flow for ESP32 Bulb Relay integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import aiohttp_client
import homeassistant.helpers.config_validation as cv

from .api import ESP32BulbRelayApi, ESP32BulbRelayApiError
from .const import (
    CONF_BULBS,
    CONF_ESP32_HOST,
    CONF_ESP32_HOSTS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class ESP32BulbRelayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ESP32 Bulb Relay."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_bulbs: list[dict[str, Any]] = []
        self._host: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step - Add ESP32 Bulb Relay."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_ESP32_HOST].strip()
            
            # Sanitize input - remove http:// or https:// if present
            host = host.removeprefix("http://").removeprefix("https://")
            # Remove trailing slash if present
            host = host.rstrip("/")
            # Remove port if included (we use default port)
            if ":" in host:
                host = host.split(":")[0]
            
            self._host = host

            # Check if already configured
            await self.async_set_unique_id(f"esp32_bulb_relay_{host}")
            self._abort_if_unique_id_configured()

            # Test connection and get bulbs
            session = aiohttp_client.async_get_clientsession(self.hass)
            api = ESP32BulbRelayApi(host, session=session)

            try:
                bulbs = await api.get_bulbs()
                if not bulbs:
                    errors["base"] = "no_bulbs"
                else:
                    self._discovered_bulbs = bulbs
                    return await self.async_step_select_bulbs()
            except ESP32BulbRelayApiError:
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ESP32_HOST): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "title": "Add ESP32 Bulb Relay",
            },
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
                title=f"ESP32 Bulb Relay ({self._host})",
                data={
                    CONF_ESP32_HOSTS: [self._host],
                },
                options={
                    CONF_BULBS: {
                        self._host: selected_bulbs,
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
        self._host_to_add: str = ""
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
        
        hosts = self._config_entry.data.get(CONF_ESP32_HOSTS, [])
        current_bulbs = dict(self._config_entry.options.get(CONF_BULBS, {}))
        
        if user_input is not None:
            # Update bulb selections for each host
            new_bulbs = {}
            for host in hosts:
                key = f"bulbs_{host.replace('.', '_')}"
                if key in user_input:
                    new_bulbs[host] = user_input[key]
                else:
                    new_bulbs[host] = current_bulbs.get(host, [])
            
            return self.async_create_entry(
                title="",
                data={
                    **self._config_entry.options,
                    CONF_BULBS: new_bulbs,
                },
            )

        # Build schema with bulbs for each host
        schema_dict = {}
        session = aiohttp_client.async_get_clientsession(self.hass)
        
        for host in hosts:
            api = ESP32BulbRelayApi(host, session=session)
            try:
                bulbs = await api.get_bulbs()
                # Extract bulb names from the response format:
                # {"id": 0, "name": "lamp", "address": "...", "connected": true}
                bulb_names = [bulb["name"] for bulb in bulbs if "name" in bulb]
                
                current_selection = current_bulbs.get(host, bulb_names)
                key = f"bulbs_{host.replace('.', '_')}"
                
                if bulb_names:
                    # Create dict for multi_select: {value: label}
                    bulb_options = {name: name for name in bulb_names}
                    schema_dict[
                        vol.Optional(key, default=current_selection)
                    ] = cv.multi_select(bulb_options)
            except ESP32BulbRelayApiError:
                _LOGGER.warning("Could not connect to %s", host)
                errors[f"bulbs_{host}"] = "cannot_connect"

        if not schema_dict:
            return self.async_abort(reason="no_hosts")

        return self.async_show_form(
            step_id="manage_bulbs",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={
                "hosts": ", ".join(hosts),
            },
        )

    async def async_step_add_esp32(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Add a new ESP32 host."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_ESP32_HOST].strip()
            
            # Sanitize input - remove http:// or https:// if present
            host = host.removeprefix("http://").removeprefix("https://")
            # Remove trailing slash if present
            host = host.rstrip("/")
            # Remove port if included (we use default port)
            if ":" in host:
                host = host.split(":")[0]
            
            self._host_to_add = host

            existing_hosts = list(self._config_entry.data.get(CONF_ESP32_HOSTS, []))
            
            if host in existing_hosts:
                errors["base"] = "already_configured"
            else:
                # Test connection
                session = aiohttp_client.async_get_clientsession(self.hass)
                api = ESP32BulbRelayApi(host, session=session)

                try:
                    bulbs = await api.get_bulbs()
                    if bulbs:
                        self._discovered_bulbs = bulbs
                        return await self.async_step_select_new_bulbs()
                    else:
                        errors["base"] = "no_bulbs"
                except ESP32BulbRelayApiError:
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="add_esp32",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ESP32_HOST): str,
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
            
            # Update config entry with new host
            existing_hosts = list(self._config_entry.data.get(CONF_ESP32_HOSTS, []))
            existing_hosts.append(self._host_to_add)
            
            # Update options with new bulbs
            current_bulbs = dict(self._config_entry.options.get(CONF_BULBS, {}))
            current_bulbs[self._host_to_add] = selected_bulbs

            # Update the config entry data
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data={
                    **self._config_entry.data,
                    CONF_ESP32_HOSTS: existing_hosts,
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
        # Bulb format: {"id": 0, "name": "lamp", "address": "...", "connected": true}
        bulb_names = [bulb["name"] for bulb in self._discovered_bulbs if "name" in bulb]
        
        # Create dict for multi_select: {value: label}
        bulb_options = {name: name for name in bulb_names}

        return self.async_show_form(
            step_id="select_new_bulbs",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BULBS, default=bulb_names): cv.multi_select(bulb_options),
                }
            ),
            description_placeholders={
                "host": self._host_to_add,
                "count": str(len(bulb_names)),
            },
        )

    async def async_step_remove_esp32(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Remove an ESP32 host."""
        hosts = list(self._config_entry.data.get(CONF_ESP32_HOSTS, []))
        
        if not hosts:
            return self.async_abort(reason="no_hosts")
        
        if len(hosts) == 1:
            return self.async_abort(reason="cannot_remove_last")

        if user_input is not None:
            host_to_remove = user_input["host_to_remove"]
            
            # Update config entry
            hosts.remove(host_to_remove)
            current_bulbs = dict(self._config_entry.options.get(CONF_BULBS, {}))
            if host_to_remove in current_bulbs:
                del current_bulbs[host_to_remove]

            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data={
                    **self._config_entry.data,
                    CONF_ESP32_HOSTS: hosts,
                },
            )

            return self.async_create_entry(
                title="",
                data={
                    **self._config_entry.options,
                    CONF_BULBS: current_bulbs,
                },
            )

        return self.async_show_form(
            step_id="remove_esp32",
            data_schema=vol.Schema(
                {
                    vol.Required("host_to_remove"): vol.In(hosts),
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
        
        hosts = self._config_entry.data.get(CONF_ESP32_HOSTS, [])
        bulb_options = self._config_entry.options.get(CONF_BULBS, {})
        
        # Build list of all bulbs with their hosts
        all_bulbs: dict[str, str] = {}  # display_name -> "host|bulb_name"
        for host in hosts:
            bulbs = bulb_options.get(host, [])
            for bulb in bulbs:
                display = f"{bulb} ({host})"
                all_bulbs[display] = f"{host}|{bulb}"

        if not all_bulbs:
            return self.async_abort(reason="no_bulbs")

        if user_input is not None:
            selected = user_input["bulb"]
            host_bulb = all_bulbs.get(selected, "")
            
            if "|" in host_bulb:
                host, bulb_name = host_bulb.split("|", 1)
                session = aiohttp_client.async_get_clientsession(self.hass)
                api = ESP32BulbRelayApi(host, session=session)

                try:
                    if action == "connect":
                        await api.connect(bulb_name)
                    else:
                        await api.disconnect(bulb_name)
                    
                    return self.async_create_entry(title="", data=self._config_entry.options)
                except ESP32BulbRelayApiError:
                    errors["base"] = "command_failed"

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
