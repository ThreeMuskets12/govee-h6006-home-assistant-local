"""Serial API client for ESP32 Bulb Relay."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import serial_asyncio

from .const import (
    CMD_BULB_BRIGHTNESS,
    CMD_BULB_CONNECT,
    CMD_BULB_DISCONNECT,
    CMD_BULB_OFF,
    CMD_BULB_ON,
    CMD_BULB_RGB,
    CMD_BULB_TEMPERATURE,
    CMD_BULBS,
    DEFAULT_BAUD_RATE,
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


class ESP32BulbRelaySerialApi:
    """Serial API client for ESP32 Bulb Relay."""

    def __init__(
        self,
        port: str,
        baud_rate: int = DEFAULT_BAUD_RATE,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialize the serial API client."""
        self._port = port
        self._baud_rate = baud_rate
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._command_queue = CommandQueue()
        self._connected = False

    async def connect(self) -> None:
        """Establish serial connection."""
        if self._connected:
            return
        
        try:
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self._port,
                baudrate=self._baud_rate,
            )
            self._connected = True
            _LOGGER.info("Connected to ESP32 on %s", self._port)
            
            # Give ESP32 time to reset after connection
            await asyncio.sleep(2)
            
            # Clear any startup messages from buffer
            await self._clear_buffer()
            
        except Exception as err:
            self._connected = False
            raise ESP32BulbRelayConnectionError(
                f"Failed to connect to {self._port}: {err}"
            ) from err

    async def _clear_buffer(self) -> None:
        """Clear any pending data in the read buffer."""
        if self._reader is None:
            return
        try:
            # Read with short timeout to clear buffer
            while True:
                try:
                    async with asyncio.timeout(0.1):
                        await self._reader.readline()
                except asyncio.TimeoutError:
                    break
        except Exception:
            pass

    async def close(self) -> None:
        """Close the serial connection."""
        await self._command_queue.stop()
        
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        
        self._reader = None
        self._writer = None
        self._connected = False
        _LOGGER.info("Disconnected from ESP32 on %s", self._port)

    async def _send_command(self, command: str) -> dict[str, Any]:
        """Send a command and wait for JSON response."""
        if not self._connected:
            await self.connect()
        
        if self._writer is None or self._reader is None:
            raise ESP32BulbRelayConnectionError("Not connected to ESP32")
        
        async with self._lock:
            try:
                # Clear any pending data
                await self._clear_buffer()
                
                # Send command with newline
                cmd_bytes = f"{command}\n".encode('utf-8')
                self._writer.write(cmd_bytes)
                await self._writer.drain()
                _LOGGER.debug("Sent command: %s", command)
                
                # Read response with timeout
                async with asyncio.timeout(self._timeout):
                    # Read lines until we get valid JSON
                    while True:
                        line = await self._reader.readline()
                        line_str = line.decode('utf-8').strip()
                        
                        if not line_str:
                            continue
                        
                        _LOGGER.debug("Received: %s", line_str)
                        
                        # Try to parse as JSON
                        try:
                            result = json.loads(line_str)
                            return result
                        except json.JSONDecodeError:
                            # Not JSON, might be debug output - keep reading
                            _LOGGER.debug("Skipping non-JSON line: %s", line_str)
                            continue
                            
            except asyncio.TimeoutError as err:
                raise ESP32BulbRelayConnectionError(
                    f"Timeout waiting for response from {self._port}"
                ) from err
            except Exception as err:
                self._connected = False
                raise ESP32BulbRelayApiError(
                    f"Error communicating with ESP32: {err}"
                ) from err

    async def _queued_command(self, command: str) -> dict[str, Any]:
        """Send a rate-limited command through the queue."""
        return await self._command_queue.enqueue(
            lambda: self._send_command(command)
        )

    def _check_success(self, result: dict[str, Any], action: str) -> None:
        """Check if the command was successful."""
        if not result.get("success", False):
            raise ESP32BulbRelayCommandError(
                f"Command '{action}' failed for bulb '{result.get('bulb', 'unknown')}'"
            )

    @property
    def port(self) -> str:
        """Return the serial port."""
        return self._port

    @property
    def is_connected(self) -> bool:
        """Return connection status."""
        return self._connected

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
        result = await self._send_command(CMD_BULBS)
        
        if isinstance(result, dict) and "bulbs" in result:
            return result["bulbs"]
        
        return []

    async def turn_on(self, bulb_name: str) -> dict[str, Any]:
        """Turn on a bulb.
        
        Expected response: {"success": true, "bulb": "lamp", "action": "on"}
        """
        command = CMD_BULB_ON.format(name=bulb_name)
        result = await self._queued_command(command)
        self._check_success(result, "on")
        return result

    async def turn_off(self, bulb_name: str) -> dict[str, Any]:
        """Turn off a bulb.
        
        Expected response: {"success": true, "bulb": "lamp", "action": "off"}
        """
        command = CMD_BULB_OFF.format(name=bulb_name)
        result = await self._queued_command(command)
        self._check_success(result, "off")
        return result

    async def set_brightness(self, bulb_name: str, brightness: int) -> dict[str, Any]:
        """Set bulb brightness (0-100).
        
        Expected response: {"success": true, "bulb": "lamp", "action": "brightness", "value": VALUE}
        """
        brightness = max(0, min(100, brightness))
        command = CMD_BULB_BRIGHTNESS.format(name=bulb_name, value=brightness)
        result = await self._queued_command(command)
        self._check_success(result, "brightness")
        return result

    async def set_rgb(self, bulb_name: str, r: int, g: int, b: int) -> dict[str, Any]:
        """Set bulb RGB color (0-255 each).
        
        Expected response: {"success": true, "bulb": "lamp", "action": "rgb", "r": R, "g": G, "b": B}
        """
        r = max(0, min(255, r))
        g = max(0, min(255, g))
        b = max(0, min(255, b))
        command = CMD_BULB_RGB.format(name=bulb_name, r=r, g=g, b=b)
        result = await self._queued_command(command)
        self._check_success(result, "rgb")
        return result

    async def set_temperature(self, bulb_name: str, temperature: int) -> dict[str, Any]:
        """Set bulb white temperature (2000-9000K).
        
        Expected response: {"success": true, "bulb": "lamp", "action": "temperature", "value": VALUE}
        """
        temperature = max(2000, min(9000, temperature))
        command = CMD_BULB_TEMPERATURE.format(name=bulb_name, value=temperature)
        result = await self._queued_command(command)
        self._check_success(result, "temperature")
        return result

    async def connect_bulb(self, bulb_name: str) -> dict[str, Any]:
        """Connect/reconnect a bulb (debug only).
        
        Expected response: {"success": true, "bulb": "lamp", "action": "connect"}
        """
        command = CMD_BULB_CONNECT.format(name=bulb_name)
        result = await self._queued_command(command)
        self._check_success(result, "connect")
        return result

    async def disconnect_bulb(self, bulb_name: str) -> dict[str, Any]:
        """Disconnect a bulb (debug only).
        
        Expected response: {"success": true, "bulb": "lamp", "action": "disconnect"}
        """
        command = CMD_BULB_DISCONNECT.format(name=bulb_name)
        result = await self._queued_command(command)
        self._check_success(result, "disconnect")
        return result

    async def test_connection(self) -> bool:
        """Test if we can connect to the ESP32."""
        try:
            await self.get_bulbs()
            return True
        except ESP32BulbRelayApiError:
            return False


async def list_serial_ports() -> list[dict[str, str]]:
    """List available serial ports."""
    import serial.tools.list_ports
    
    ports = []
    for port in serial.tools.list_ports.comports():
        ports.append({
            "device": port.device,
            "description": port.description,
            "hwid": port.hwid,
            "name": f"{port.device} - {port.description}" if port.description else port.device,
        })
    
    return ports
