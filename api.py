"""API client for ESP32 Bulb Relay."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .const import (
    API_BULB_BRIGHTNESS,
    API_BULB_CONNECT,
    API_BULB_DISCONNECT,
    API_BULB_OFF,
    API_BULB_ON,
    API_BULB_RGB,
    API_BULB_TEMPERATURE,
    API_BULBS,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
)
from .queue import CommandQueue

_LOGGER = logging.getLogger(__name__)


class ESP32BulbRelayApiError(Exception):
    """Exception for API errors."""


class ESP32BulbRelayConnectionError(ESP32BulbRelayApiError):
    """Exception for connection errors."""


class ESP32BulbRelayCommandError(ESP32BulbRelayApiError):
    """Exception for command failures."""


class ESP32BulbRelayApi:
    """API client for ESP32 Bulb Relay."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        session: aiohttp.ClientSession | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialize the API client."""
        self._host = host
        self._port = port
        self._session = session
        self._timeout = timeout
        self._base_url = f"http://{host}:{port}"
        self._owns_session = False
        self._command_queue = CommandQueue()

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the aiohttp session."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        """Close the session and stop the queue."""
        await self._command_queue.stop()
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None

    async def _request(self, endpoint: str) -> dict[str, Any]:
        """Make a request to the ESP32 and return JSON response."""
        session = await self._get_session()
        url = f"{self._base_url}{endpoint}"

        try:
            async with asyncio.timeout(self._timeout):
                async with session.get(url) as response:
                    if response.status != 200:
                        raise ESP32BulbRelayApiError(
                            f"API request failed with status {response.status}"
                        )
                    
                    return await response.json()
        except asyncio.TimeoutError as err:
            raise ESP32BulbRelayConnectionError(
                f"Timeout connecting to {self._host}"
            ) from err
        except aiohttp.ClientError as err:
            raise ESP32BulbRelayConnectionError(
                f"Error connecting to {self._host}: {err}"
            ) from err
        except Exception as err:
            raise ESP32BulbRelayApiError(
                f"Error parsing response from {self._host}: {err}"
            ) from err

    async def _queued_request(self, endpoint: str) -> dict[str, Any]:
        """Make a rate-limited request through the command queue."""
        return await self._command_queue.enqueue(
            lambda: self._request(endpoint)
        )

    def _check_success(self, result: dict[str, Any], action: str) -> None:
        """Check if the command was successful."""
        if not result.get("success", False):
            raise ESP32BulbRelayCommandError(
                f"Command '{action}' failed for bulb '{result.get('bulb', 'unknown')}'"
            )

    @property
    def host(self) -> str:
        """Return the host."""
        return self._host

    @property
    def pending_commands(self) -> int:
        """Return the number of pending commands in the queue."""
        return self._command_queue.pending_count

    async def get_bulbs(self) -> list[dict[str, Any]]:
        """Get list of all bulbs from the ESP32.
        
        Expected response format:
        {
            "bulbs": [
                {"id": 0, "name": "lamp", "address": "d0:c9:07:81:56:b9", "connected": true}
            ],
            "count": 1
        }
        
        Returns list of bulb dicts with keys: id, name, address, connected
        
        Note: This is NOT rate-limited as it's used for status polling.
        """
        result = await self._request(API_BULBS)
        
        if isinstance(result, dict) and "bulbs" in result:
            return result["bulbs"]
        
        return []

    async def turn_on(self, bulb_name: str) -> dict[str, Any]:
        """Turn on a bulb.
        
        Expected response: {"success": true, "bulb": "lamp", "action": "on"}
        """
        endpoint = API_BULB_ON.format(name=bulb_name)
        result = await self._queued_request(endpoint)
        self._check_success(result, "on")
        return result

    async def turn_off(self, bulb_name: str) -> dict[str, Any]:
        """Turn off a bulb.
        
        Expected response: {"success": true, "bulb": "lamp", "action": "off"}
        """
        endpoint = API_BULB_OFF.format(name=bulb_name)
        result = await self._queued_request(endpoint)
        self._check_success(result, "off")
        return result

    async def set_brightness(self, bulb_name: str, brightness: int) -> dict[str, Any]:
        """Set bulb brightness (0-100).
        
        Expected response: {"success": true, "bulb": "lamp", "action": "brightness", "value": VALUE}
        """
        brightness = max(0, min(100, brightness))
        endpoint = API_BULB_BRIGHTNESS.format(name=bulb_name, value=brightness)
        result = await self._queued_request(endpoint)
        self._check_success(result, "brightness")
        return result

    async def set_rgb(self, bulb_name: str, r: int, g: int, b: int) -> dict[str, Any]:
        """Set bulb RGB color (0-255 each).
        
        Expected response: {"success": true, "bulb": "lamp", "action": "rgb", "r": R, "g": G, "b": B}
        """
        r = max(0, min(255, r))
        g = max(0, min(255, g))
        b = max(0, min(255, b))
        endpoint = API_BULB_RGB.format(name=bulb_name, r=r, g=g, b=b)
        result = await self._queued_request(endpoint)
        self._check_success(result, "rgb")
        return result

    async def set_temperature(self, bulb_name: str, temperature: int) -> dict[str, Any]:
        """Set bulb white temperature (2000-9000K).
        
        Expected response: {"success": true, "bulb": "lamp", "action": "temperature", "value": VALUE}
        """
        temperature = max(2000, min(9000, temperature))
        endpoint = API_BULB_TEMPERATURE.format(name=bulb_name, value=temperature)
        result = await self._queued_request(endpoint)
        self._check_success(result, "temperature")
        return result

    async def connect(self, bulb_name: str) -> dict[str, Any]:
        """Connect/reconnect a bulb (debug only).
        
        Expected response: {"success": true, "bulb": "lamp", "action": "connect"}
        """
        endpoint = API_BULB_CONNECT.format(name=bulb_name)
        result = await self._queued_request(endpoint)
        self._check_success(result, "connect")
        return result

    async def disconnect(self, bulb_name: str) -> dict[str, Any]:
        """Disconnect a bulb (debug only).
        
        Expected response: {"success": true, "bulb": "lamp", "action": "disconnect"}
        """
        endpoint = API_BULB_DISCONNECT.format(name=bulb_name)
        result = await self._queued_request(endpoint)
        self._check_success(result, "disconnect")
        return result

    async def test_connection(self) -> bool:
        """Test if we can connect to the ESP32."""
        try:
            await self.get_bulbs()
            return True
        except ESP32BulbRelayApiError:
            return False
