"""Serial API client for ESP32 Bulb Relay."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

try:
    import serial_asyncio_fast as serial_asyncio  # HA 2026.10+ no longer ships pyserial-asyncio
except ImportError:  # pragma: no cover
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
    COMMAND_TIMEOUT,
    DEFAULT_BAUD_RATE,
    DEFAULT_TIMEOUT,
    JUNK_LINES_MAX_BYTES,
    JUNK_SAMPLE_BYTES,
    POLL_TIMEOUT,
    RECOVERY_BOOT_WAIT,
    RECOVERY_RETRY_GAP,
    RESYNC_MAX_BYTES,
    RESYNC_MAX_TIME,
    RESYNC_QUIET_GAP,
)
from .queue import CommandQueue

_LOGGER = logging.getLogger(__name__)

# Shorter timeout for initial connection test
CONNECT_TEST_TIMEOUT = 10


class ESP32BulbRelayApiError(Exception):
    """Exception for API errors."""


class ESP32BulbRelayConnectionError(ESP32BulbRelayApiError):
    """Exception for connection errors."""


class ESP32BulbRelayTimeoutError(ESP32BulbRelayConnectionError):
    """The ESP32 did not answer in time; it may still act on the command later."""


class ESP32BulbRelayOutOfSyncError(ESP32BulbRelayTimeoutError):
    """Junk on the stream got in the way. This episode has had its warning already."""


class ESP32BulbRelayRecoveringError(ESP32BulbRelayTimeoutError):
    """The board is held in reset or booting, so nothing may be sent to it yet."""


class ESP32BulbRelayCommandError(ESP32BulbRelayApiError):
    """Exception for command failures."""


class ESP32BulbRelayBulbNotFoundError(ESP32BulbRelayCommandError):
    """This ESP32 does not have the bulb, so it may have moved to another port."""


def _assert_hold(serial_port: Any) -> Any:
    """Pull EN low through the DevKit auto-reset circuit. Blocks; call from an executor.

    EN is low only with RTS asserted and DTR not; IO0 is low only with DTR asserted and
    RTS not. DTR goes first, so the lines never pass through the state that pulls IO0 low.
    """
    serial_port.dtr = False  # IO0 high
    serial_port.rts = True  # EN low
    return serial_port


def _hold_through_new_port(device: str, baud_rate: int) -> Any:
    """Hold a board in reset through its own port. Blocks; call from an executor."""
    import serial

    # Opening with pyserial's defaults asserts both lines, which does not reset the board
    serial_port = serial.Serial(device, baud_rate)
    try:
        return _assert_hold(serial_port)
    except OSError:
        serial_port.close()
        raise


def _release_hold(serial_port: Any, close: bool) -> None:
    """Let EN rise, so the sketch boots. Blocks; call from an executor.

    Both lines stay deasserted afterwards: that is a run state, and raising them one at
    a time would pull EN or IO0 low.
    """
    try:
        serial_port.dtr = False  # IO0 high
        serial_port.rts = False  # EN high: the board boots
    finally:
        if close:
            serial_port.close()


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
        # A timed-out command's reply may still arrive. The firmware has no request
        # IDs, but it answers in order, so a /bulbs reply proves the stream is in sync.
        self._resync_needed = False
        self._out_of_sync = False  # Junk seen; discard input until it goes quiet
        self._junk_logged = False  # This episode of junk has had its one warning
        self._no_reply_logged = False  # This outage has had its one error
        self._last_heard: float | None = None  # Last reply, or port open if none since
        self._recovering = False  # Held or booting: nothing else may use the port
        self._hold: asyncio.Future | None = None  # Resolves to the port holding EN low
        _LOGGER.debug("API client initialized for port %s", port)

    async def connect(self) -> None:
        """Establish serial connection."""
        self._raise_if_recovering()
        async with self._lock:
            await self._connect()

    async def _connect(self) -> None:
        """Open the port. The API lock must be held."""
        if self._connected:
            _LOGGER.debug("Already connected to %s", self._port)
            return
        
        _LOGGER.info("Attempting to connect to %s at %d baud", self._port, self._baud_rate)
        
        try:
            # Wrap the ENTIRE connection sequence in a timeout
            # This includes serial open, boot wait, and buffer clear
            async with asyncio.timeout(15):
                self._reader, self._writer = await serial_asyncio.open_serial_connection(
                    url=self._port,
                    baudrate=self._baud_rate,
                )
                
                self._connected = True
                _LOGGER.info("Serial connection established to %s", self._port)
                
                # Give ESP32 time to finish any reset/boot sequence
                # Reduced from 2s since we also clear buffer
                _LOGGER.debug("Waiting 1s for ESP32 boot sequence on %s", self._port)
                await asyncio.sleep(1)
                
                # Clear any startup messages/garbage from buffer
                await self._clear_buffer()
                self._last_heard = asyncio.get_running_loop().time()
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
        """Clear any pending data in the read buffer.
        
        This has strict time limits to prevent blocking HA startup.
        """
        if self._reader is None:
            return
        
        total_cleared = 0
        sample = b""
        start_time = asyncio.get_event_loop().time()
        max_time = 2.0  # Maximum 2 seconds total for buffer clearing
        max_bytes = 65536  # Maximum 64KB to clear

        _LOGGER.debug("Clearing buffer on %s", self._port)
        
        try:
            while True:
                # Check overall time limit
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > max_time:
                    _LOGGER.debug(
                        "Buffer clear time limit (%.1fs) reached on %s, cleared %d bytes",
                        max_time, self._port, total_cleared
                    )
                    # At 115200 baud a flood reaches this before the bytes limit
                    self._mark_out_of_sync(f"no pause in {max_time:.0f}s of clearing", sample)
                    break

                # Check bytes limit
                if total_cleared > max_bytes:
                    self._mark_out_of_sync(f"over {max_bytes} bytes to clear", sample)
                    break

                try:
                    # Use wait_for which is more reliable than timeout context
                    data = await asyncio.wait_for(
                        self._reader.read(1024),  # Smaller chunks
                        timeout=0.1  # Very short timeout
                    )
                    if not data:
                        _LOGGER.debug("No more data to clear on %s", self._port)
                        break
                    total_cleared += len(data)
                    if len(sample) < JUNK_SAMPLE_BYTES:
                        sample += data
                except asyncio.TimeoutError:
                    # No data available - buffer is clear
                    _LOGGER.debug("Buffer clear complete on %s (timeout, %d bytes cleared)", 
                                 self._port, total_cleared)
                    break
                except Exception as read_err:
                    _LOGGER.debug("Read error during buffer clear on %s: %s", self._port, read_err)
                    break
                    
        except Exception as err:
            _LOGGER.debug("Error clearing buffer on %s: %s", self._port, err)
        
        if total_cleared > 0:
            _LOGGER.info("Cleared %d bytes from buffer on %s", total_cleared, self._port)

    def _mark_out_of_sync(self, reason: str, junk: bytes) -> None:
        """Note that the stream can't be trusted, warning once per episode."""
        self._out_of_sync = True
        if self._junk_logged:
            _LOGGER.debug("Stream from %s still out of sync: %s", self._port, reason)
            return
        self._junk_logged = True
        _LOGGER.warning(
            "Serial stream from %s is out of sync (%s), discarding until it goes quiet. "
            "First bytes: %r",
            self._port, reason, junk[:JUNK_SAMPLE_BYTES]
        )

    async def _resync_stream(self) -> None:
        """Discard input until a quiet gap, within the time and bytes limits."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RESYNC_MAX_TIME
        discarded = 0
        sample = b""

        while loop.time() < deadline and discarded < RESYNC_MAX_BYTES:
            try:
                data = await asyncio.wait_for(
                    self._reader.read(4096),
                    timeout=RESYNC_QUIET_GAP
                )
            except asyncio.TimeoutError:
                self._out_of_sync = False
                _LOGGER.debug("Stream from %s quiet after %d bytes", self._port, discarded)
                return
            if not data:
                break
            discarded += len(data)
            if len(sample) < JUNK_SAMPLE_BYTES:
                sample += data

        # After a reset the episode is new, and junk too slow for the clear starts here
        self._mark_out_of_sync(f"no pause in {discarded} bytes", sample)
        raise ESP32BulbRelayOutOfSyncError(
            f"{self._port} still out of sync after discarding {discarded} bytes"
        )

    async def close(self) -> None:
        """Close the serial connection."""
        _LOGGER.debug("Closing connection to %s", self._port)

        await self.release_hold()

        try:
            await self._command_queue.stop()
        except Exception as err:
            _LOGGER.debug("Error stopping command queue for %s: %s", self._port, err)

        await self._close_transport()
        _LOGGER.info("Disconnected from %s", self._port)

    async def _close_transport(self) -> None:
        """Close the port but leave the command queue running."""
        if self._writer:
            try:
                self._writer.close()
                # Add timeout to wait_closed to prevent hanging
                await asyncio.wait_for(
                    self._writer.wait_closed(),
                    timeout=2.0
                )
            except asyncio.TimeoutError:
                _LOGGER.debug("Timeout waiting for writer to close on %s", self._port)
            except Exception as err:
                _LOGGER.debug("Error closing writer for %s: %s", self._port, err)
        
        self._reader = None
        self._writer = None
        self._connected = False

    def _raise_if_recovering(self) -> None:
        """Fail fast: a held board can't answer, and a booting one must not be disturbed."""
        if self._recovering:
            raise ESP32BulbRelayRecoveringError(f"{self._port} is being recovered")

    def silent_for(self, seconds: float) -> bool:
        """Return whether nothing has been heard from the board for this long."""
        return (
            self._last_heard is None
            or asyncio.get_running_loop().time() - self._last_heard >= seconds
        )

    async def hold_in_reset(self, min_silence: float = 0) -> bool:
        """Hold EN low, so the board draws no boot current, until it is released.

        Every other use of the port fails fast until release_and_wait() succeeds or
        release_hold() is called, and one of those must follow. Returns False, leaving the
        board alone, if it was heard in the last min_silence seconds.
        """
        loop = asyncio.get_running_loop()

        # Set before queueing for the lock, which is FIFO, so nothing can follow this in
        self._recovering = True

        async with self._lock:
            if not self.silent_for(min_silence):
                _LOGGER.debug("Not holding %s: it answered recently", self._port)
                self._recovering = False
                return False

            serial_port = self._writer.get_extra_info("serial") if self._writer else None
            if serial_port is not None and serial_port.is_open:
                self._hold = loop.run_in_executor(None, _assert_hold, serial_port)
            else:
                await self._close_transport()
                self._hold = loop.run_in_executor(
                    None, _hold_through_new_port, self._port, self._baud_rate
                )

            # If this is cancelled the lines may yet change, so the release waits for the job
            try:
                await asyncio.shield(self._hold)
            except OSError as err:  # Includes serial.SerialException
                self._hold = None
                self._recovering = False
                raise ESP32BulbRelayConnectionError(
                    f"Could not hold {self._port} in reset: {err}"
                ) from err
            return True

    async def _let_go(self) -> None:
        """Let EN rise if it is held, and see that through even if cancelled."""
        job, self._hold = self._hold, None
        if job is None:
            return

        async def release() -> None:
            serial_port = await job
            transport = self._writer.get_extra_info("serial") if self._writer else None
            await asyncio.get_running_loop().run_in_executor(
                None, _release_hold, serial_port, serial_port is not transport
            )

        release_task = asyncio.ensure_future(release())
        await asyncio.shield(release_task)

    async def release_hold(self) -> None:
        """End the recovery of this board, letting it run. Safe to call at any time."""
        try:
            await self._let_go()
        except OSError as err:
            _LOGGER.debug("Error releasing %s: %s", self._port, err)
        finally:
            self._recovering = False

    async def release_and_wait(self, timeout: float) -> bool:
        """Release the held board and wait for a quiet line and an answer to /bulbs.

        The API lock is held throughout, so nothing else reaches the booting board. If this
        returns False the board is running but unproven, and the recovery is not over:
        hold it again, or end it with release_hold().
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        async with self._lock:
            try:
                await self._let_go()
            except OSError as err:
                _LOGGER.debug("Could not release %s: %s", self._port, err)
                return False

            # A reset drops whatever the board was working on
            self._resync_needed = True

            await asyncio.sleep(RECOVERY_BOOT_WAIT)

            while loop.time() < deadline:
                try:
                    if not self._connected:
                        await self._connect()
                    await self._resync_stream()
                    await self._exchange(CMD_BULBS, min(POLL_TIMEOUT, deadline - loop.time()))
                    self._recovering = False
                    return True
                except ESP32BulbRelayConnectionError as err:
                    _LOGGER.debug("%s not up yet: %s", self._port, err)
                except OSError as err:  # The transport died with the board
                    _LOGGER.debug("%s not up yet: %s", self._port, err)
                    await self._close_transport()
                await asyncio.sleep(RECOVERY_RETRY_GAP)

        return False

    async def _read_line_safe(self, timeout: float = 5.0) -> bytes | None:
        """Read a newline-terminated line, or None if there isn't one in time.

        Uses individual timeouts per read operation to prevent any single
        read from blocking indefinitely. Anything unterminated is discarded, because
        it may be a fragment of junk.
        """
        if self._reader is None:
            return None
        
        line = b""
        start_time = asyncio.get_event_loop().time()
        
        try:
            while True:
                # Check overall timeout
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > timeout:
                    _LOGGER.debug("Read timeout on %s after %.1fs (got %d bytes)",
                                 self._port, elapsed, len(line))
                    return None

                # Remaining time for this read
                remaining = max(0.1, timeout - elapsed)
                
                try:
                    # Read one byte with timeout
                    byte = await asyncio.wait_for(
                        self._reader.read(1),
                        timeout=min(remaining, 0.5)  # Max 0.5s per byte
                    )
                except asyncio.TimeoutError:
                    return None

                if not byte:
                    return None

                if byte == b'\n':
                    return line
                if byte == b'\r':
                    continue  # Skip carriage returns
                    
                line += byte
                
                # Limit line length to prevent memory issues
                if len(line) > 8192:
                    self._mark_out_of_sync("line too long", line)
                    return None

        except Exception as err:
            _LOGGER.debug("Read error on %s: %s", self._port, err)
            return None

    async def _send_command(self, command: str, timeout: float | None = None) -> dict[str, Any]:
        """Send a command and wait for JSON response."""
        if timeout is None:
            timeout = self._timeout
            
        _LOGGER.debug("_send_command called: %s on %s (timeout=%s)", command, self._port, timeout)

        self._raise_if_recovering()

        async with self._lock:
            _LOGGER.debug("Acquired lock for %s", self._port)

            if not self._connected:
                _LOGGER.debug("Not connected, attempting to connect to %s", self._port)
                await self._connect()

            if self._writer is None or self._reader is None:
                raise ESP32BulbRelayConnectionError("Not connected to ESP32")

            try:
                # Clear any pending data
                await self._clear_buffer()

                # Nothing is written into a stream that hasn't gone quiet
                if self._out_of_sync:
                    await self._resync_stream()

                # Don't queue a command behind one the ESP32 hasn't finished: it would
                # run late, and its reply couldn't be told from the earlier one's.
                if self._resync_needed and command != CMD_BULBS:
                    await self._exchange(CMD_BULBS, POLL_TIMEOUT)
                
                return await self._exchange(command, timeout)
                            
            except ESP32BulbRelayConnectionError:
                self._resync_needed = True
                raise
            except asyncio.TimeoutError:
                _LOGGER.error("Asyncio timeout on %s", self._port)
                self._resync_needed = True
                raise ESP32BulbRelayTimeoutError(
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

    async def _exchange(self, command: str, timeout: float) -> dict[str, Any]:
        """Write one command and read its reply. The API lock must be held."""
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
        junk_bytes = 0

        while attempts < max_attempts:
            # Check overall timeout
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                # A silent board fails every scan, so an outage gets one error
                quiet = self._no_reply_logged or self._recovering
                self._no_reply_logged = True
                _LOGGER.log(
                    logging.DEBUG if quiet else logging.ERROR,
                    "Overall timeout after %.1fs on %s", elapsed, self._port
                )
                raise ESP32BulbRelayTimeoutError(
                    f"Timeout waiting for response from {self._port}"
                )
            
            attempts += 1
            remaining_timeout = min(5.0, timeout - elapsed)
            
            _LOGGER.debug("Read attempt %d on %s (%.1fs remaining)", 
                         attempts, self._port, timeout - elapsed)
            
            line = await self._read_line_safe(timeout=remaining_timeout)

            # The reply, if any, is lost in the junk. The board may still act on the command.
            if self._out_of_sync:
                await self._resync_stream()
                raise ESP32BulbRelayOutOfSyncError(
                    f"Reply from {self._port} lost in junk on the stream"
                )

            if line is None:
                _LOGGER.debug("No data received on attempt %d from %s", attempts, self._port)
                continue

            junk_bytes += len(line)
            if junk_bytes > JUNK_LINES_MAX_BYTES:
                self._mark_out_of_sync(f"{junk_bytes} bytes of lines but no reply", line)
                continue

            # Try to decode as UTF-8. Dropping bad bytes could turn junk into a valid reply.
            try:
                line_str = line.decode('utf-8').strip()
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
            except json.JSONDecodeError as json_err:
                _LOGGER.debug("JSON parse error on %s: %s", self._port, json_err)
                continue

            # Even a late reply proves the board is alive and the stream is readable
            self._last_heard = asyncio.get_event_loop().time()
            self._junk_logged = False
            self._no_reply_logged = False
            junk_bytes = 0

            # A late reply to a timed-out command can't answer this one
            is_bulb_list = isinstance(result, dict) and "bulbs" in result
            if is_bulb_list != (command == CMD_BULBS):
                _LOGGER.debug("Skipping late reply from %s: %s", self._port, result)
                continue
            
            if is_bulb_list:
                self._resync_needed = False
            _LOGGER.info("Valid JSON response from %s: %s", self._port, result)
            return result
        
        # The timeout comes first unless lines keep arriving, and a reset loop's are short
        self._mark_out_of_sync(f"{max_attempts} attempts but no reply", line or b"")
        await self._resync_stream()
        raise ESP32BulbRelayOutOfSyncError(
            f"No valid JSON response after {max_attempts} attempts from {self._port}"
        )

    async def _queued_command(self, command: str) -> dict[str, Any]:
        """Send a rate-limited command through the queue.
        
        Uses COMMAND_TIMEOUT since bulb commands may involve BLE reconnection.
        """
        return await self._command_queue.enqueue(
            lambda: self._send_command(command, timeout=COMMAND_TIMEOUT)
        )

    def _check_success(self, result: dict[str, Any], action: str) -> None:
        """Check if the command was successful."""
        if result.get("error") == "Bulb not found":
            raise ESP32BulbRelayBulbNotFoundError(
                f"Command '{action}' failed: bulb not found on {self._port}"
            )
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
    def out_of_sync(self) -> bool:
        """Return True if the stream never went quiet: a flood, not a busy board."""
        return self._out_of_sync

    @property
    def recovering(self) -> bool:
        """Return True while the board is held in reset or booting from a recovery."""
        return self._recovering

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
