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

# Shorter timeout for initial connection test
CONNECT_TEST_TIMEOUT = 10


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
        _LOGGER.debug("API client initialized for port %s", port)

    async def connect(self) -> None:
        """Establish serial connection."""
        if self._connected:
            _LOGGER.debug("Already connected to %s", self._port)
            return
        
        _LOGGER.info("Attempting to connect to %s at %d baud", self._port, self._baud_rate)
        
        try:
            # Set a timeout for the connection attempt itself
            async with asyncio.timeout(10):
                self._reader, self._writer = await serial_asyncio.open_serial_connection(
                    url=self._port,
                    baudrate=self._baud_rate,
                )
            
            self._connected = True
            _LOGGER.info("Serial connection established to %s", self._port)
            
            # Give ESP32 time to finish any reset/boot sequence
            _LOGGER.debug("Waiting 2s for ESP32 boot sequence on %s", self._port)
            await asyncio.sleep(2)
            
            # Clear any startup messages/garbage from buffer
            _LOGGER.debug("Clearing buffer on %s", self._port)
            await self._clear_buffer()
            _LOGGER.info("Connection to %s ready", self._port)
            
        except asyncio.TimeoutError:
            self._connected = False
            _LOGGER.error("Timeout while connecting to %s", self._port)
            raise ESP32BulbRelayConnectionError(
                f"Timeout connecting to {self._port}"
            )
        except Exception as err:
            self._connected = False
            _LOGGER.error("Failed to connect to %s: %s", self._port, err)
            raise ESP32BulbRelayConnectionError(
                f"Failed to connect to {self._port}: {err}"
            ) from err

    async def _clear_buffer(self) -> None:
        """Clear any pending data in the read buffer."""
        if self._reader is None:
            return
        
        total_cleared = 0
        try:
            # Read and discard any pending data with short timeout
            while True:
                try:
                    async with asyncio.timeout(0.2):
                        data = await self._reader.read(4096)
                        if not data:
                            break
                        total_cleared += len(data)
                        _LOGGER.debug("Cleared %d bytes from %s (total: %d)", 
                                     len(data), self._port, total_cleared)
                except asyncio.TimeoutError:
                    break
        except Exception as err:
            _LOGGER.debug("Error clearing buffer on %s: %s", self._port, err)
        
        if total_cleared > 0:
            _LOGGER.debug("Total cleared from %s: %d bytes", self._port, total_cleared)

    async def close(self) -> None:
        """Close the serial connection."""
        _LOGGER.debug("Closing connection to %s", self._port)
        await self._command_queue.stop()
        
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception as err:
                _LOGGER.debug("Error closing writer for %s: %s", self._port, err)
        
        self._reader = None
        self._writer = None
        self._connected = False
        _LOGGER.info("Disconnected from %s", self._port)

    async def _read_line_safe(self, timeout: float = 5.0) -> bytes | None:
        """Read a line with timeout, handling errors gracefully."""
        if self._reader is None:
            return None
        
        try:
            async with asyncio.timeout(timeout):
                # Read byte by byte until newline to avoid buffer issues
                line = b""
                while True:
                    byte = await self._reader.read(1)
                    if not byte:
                        if line:
                            return line
                        return None
                    if byte == b'\n':
                        return line
                    if byte == b'\r':
                        continue  # Skip carriage returns
                    line += byte
                    # Limit line length to prevent memory issues
                    if len(line) > 8192:
                        _LOGGER.warning("Line too long on %s, truncating", self._port)
                        return line
        except asyncio.TimeoutError:
            _LOGGER.debug("Read timeout on %s (got %d bytes so far)", self._port, len(line) if 'line' in dir() else 0)
            return None
        except Exception as err:
            _LOGGER.debug("Read error on %s: %s", self._port, err)
            return None

    async def _send_command(self, command: str, timeout: float | None = None) -> dict[str, Any]:
        """Send a command and wait for JSON response."""
        if timeout is None:
            timeout = self._timeout
            
        _LOGGER.debug("_send_command called: %s on %s (timeout=%s)", command, self._port, timeout)
        
        if not self._connected:
            _LOGGER.debug("Not connected, attempting to connect to %s", self._port)
            await self.connect()
        
        if self._writer is None or self._reader is None:
            raise ESP32BulbRelayConnectionError("Not connected to ESP32")
        
        async with self._lock:
            _LOGGER.debug("Acquired lock for %s", self._port)
            
            try:
                # Clear any pending data
                await self._clear_buffer()
                
                # Send command with newline
                cmd_bytes = f"{command}\n".encode('utf-8')
                _LOGGER.info("Sending to %s: %s", self._port, command)
                self._writer.write(cmd_bytes)
                await self._writer.drain()
                _LOGGER.debug("Command sent and drained on %s", self._port)
                
                # Read response with timeout
                start_time = asyncio.get_event_loop().time()
                attempts = 0
                max_attempts = 100
                
                while attempts < max_attempts:
                    # Check overall timeout
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed > timeout:
                        _LOGGER.error("Overall timeout after %.1fs on %s", elapsed, self._port)
                        raise ESP32BulbRelayConnectionError(
                            f"Timeout waiting for response from {self._port}"
                        )
                    
                    attempts += 1
                    remaining_timeout = min(5.0, timeout - elapsed)
                    
                    _LOGGER.debug("Read attempt %d on %s (%.1fs remaining)", 
                                 attempts, self._port, timeout - elapsed)
                    
                    line = await self._read_line_safe(timeout=remaining_timeout)
                    
                    if line is None:
                        _LOGGER.debug("No data received on attempt %d from %s", attempts, self._port)
                        continue
                    
                    # Try to decode as UTF-8
                    try:
                        line_str = line.decode('utf-8', errors='ignore').strip()
                    except Exception as decode_err:
                        _LOGGER.debug("Decode error on %s: %s", self._port, decode_err)
                        continue
                    
                    if not line_str:
                        continue
                    
                    # Log what we received
                    if len(line_str) > 200:
                        _LOGGER.debug("Received from %s: %s... (%d chars)", 
                                     self._port, line_str[:200], len(line_str))
                    else:
                        _LOGGER.debug("Received from %s: %s", self._port, line_str)
                    
                    # Skip lines that don't look like JSON
                    if not (line_str.startswith('{') or line_str.startswith('[')):
                        _LOGGER.debug("Skipping non-JSON line from %s", self._port)
                        continue
                    
                    # Try to parse as JSON
                    try:
                        result = json.loads(line_str)
                        _LOGGER.info("Valid JSON response from %s: %s", self._port, result)
                        return result
                    except json.JSONDecodeError as json_err:
                        _LOGGER.debug("JSON parse error on %s: %s", self._port, json_err)
                        continue
                
                _LOGGER.error("No valid JSON after %d attempts on %s", max_attempts, self._port)
                raise ESP32BulbRelayConnectionError(
                    f"No valid JSON response after {max_attempts} attempts from {self._port}"
                )
                            
            except ESP32BulbRelayConnectionError:
                raise
            except asyncio.TimeoutError:
                _LOGGER.error("Asyncio timeout on %s", self._port)
                raise ESP32BulbRelayConnectionError(
                    f"Timeout waiting for response from {self._port}"
                )
            except Exception as err:
                self._connected = False
                _LOGGER.error("Error communicating with %s: %s", self._port, err)
                raise ESP32BulbRelayApiError(
                    f"Error communicating with ESP32: {err}"
                ) from err
            finally:
                _LOGGER.debug("Released lock for %s", self._port)

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

    async def get_bulbs(self, timeout: float | None = None) -> list[dict[str, Any]]:
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
        _LOGGER.debug("get_bulbs called on %s with timeout=%s", self._port, timeout)
        result = await self._send_command(CMD_BULBS, timeout=timeout)
        
        if isinstance(result, dict) and "bulbs" in result:
            _LOGGER.debug("Found %d bulbs on %s", len(result["bulbs"]), self._port)
            return result["bulbs"]
        
        _LOGGER.warning("Unexpected response format from %s: %s", self._port, result)
        return []

    async def turn_on(self, bulb_name: str) -> dict[str, Any]:
        """Turn on a bulb."""
        command = CMD_BULB_ON.format(name=bulb_name)
        result = await self._queued_command(command)
        self._check_success(result, "on")
        return result

    async def turn_off(self, bulb_name: str) -> dict[str, Any]:
        """Turn off a bulb."""
        command = CMD_BULB_OFF.format(name=bulb_name)
        result = await self._queued_command(command)
        self._check_success(result, "off")
        return result

    async def set_brightness(self, bulb_name: str, brightness: int) -> dict[str, Any]:
        """Set bulb brightness (0-100)."""
        brightness = max(0, min(100, brightness))
        command = CMD_BULB_BRIGHTNESS.format(name=bulb_name, value=brightness)
        result = await self._queued_command(command)
        self._check_success(result, "brightness")
        return result

    async def set_rgb(self, bulb_name: str, r: int, g: int, b: int) -> dict[str, Any]:
        """Set bulb RGB color (0-255 each)."""
        r = max(0, min(255, r))
        g = max(0, min(255, g))
        b = max(0, min(255, b))
        command = CMD_BULB_RGB.format(name=bulb_name, r=r, g=g, b=b)
        result = await self._queued_command(command)
        self._check_success(result, "rgb")
        return result

    async def set_temperature(self, bulb_name: str, temperature: int) -> dict[str, Any]:
        """Set bulb white temperature (2000-9000K)."""
        temperature = max(2000, min(9000, temperature))
        command = CMD_BULB_TEMPERATURE.format(name=bulb_name, value=temperature)
        result = await self._queued_command(command)
        self._check_success(result, "temperature")
        return result

    async def connect_bulb(self, bulb_name: str) -> dict[str, Any]:
        """Connect/reconnect a bulb (debug only)."""
        command = CMD_BULB_CONNECT.format(name=bulb_name)
        result = await self._queued_command(command)
        self._check_success(result, "connect")
        return result

    async def disconnect_bulb(self, bulb_name: str) -> dict[str, Any]:
        """Disconnect a bulb (debug only)."""
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
    """List available serial ports.
    
    This runs in an executor to avoid blocking the event loop.
    """
    def _list_ports() -> list[dict[str, str]]:
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
    
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _list_ports)
