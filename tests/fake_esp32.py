"""Timing model of the ESP32 sketch behind an in-memory serial stream.

This is a model of govee_controller_serial.ino, not the firmware itself. It keeps
what matters to the integration: loop() answers one line at a time, in order, and
stalls for the durations in the sketch and NimBLE. `firmware="original"` models the
recovered sketch; `firmware="fixed"` models the working-tree sketch.
"""
from __future__ import annotations

import asyncio
import json

SERIAL_ROUND_TRIP = 0.011  # Field median, .analysis/evidence_latency.txt

CONNECT_TIMEOUT = 30.0  # NimBLEClient m_connectTimeout default; the sketch never sets it
WRITE_TIMEOUT = 3.0  # Sketch WRITE_TIMEOUT
MAX_RETRY_ATTEMPTS = 3  # Sketch MAX_RETRY_ATTEMPTS
RECONNECT_DELAY = 0.5  # delay(500) after a failed inline reconnect

# writeWithRetry() on a dead bulb in fast mode: three connects, each followed by delay(500).
DEAD_BULB_STALL = MAX_RETRY_ATTEMPTS * (CONNECT_TIMEOUT + RECONNECT_DELAY)

# BLEMonitor's cycle for a dead bulb in slow mode: lastReconnectAttempt is stamped when
# the attempt starts, and the 5 s monitor tick lands on 60.5 s after it.
SLOW_MODE_CYCLE = 60.5


class FakeEsp32:
    """One board: exposes the StreamWriter surface and feeds a real StreamReader."""

    def __init__(
        self,
        bulbs: dict[str, bool],
        firmware: str = "original",
        dead_bulb_stall: float = DEAD_BULB_STALL,
    ) -> None:
        self.bulbs = bulbs  # name -> connected
        self.firmware = firmware
        self.dead_bulb_stall = dead_bulb_stall
        self.reader = asyncio.StreamReader()
        self.received: list[str] = []
        self.executed: list[tuple[float, str]] = []
        self.monitor_holds_mutex_until = 0.0
        self.loop_stalled_until = 0.0
        self._rx: asyncio.Queue[str] = asyncio.Queue()
        self._loop_task = asyncio.create_task(self._loop())

    # StreamWriter surface used by api.py
    def write(self, data: bytes) -> None:
        for line in data.decode().splitlines():
            self.received.append(line)
            self._rx.put_nowait(line)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._loop_task.cancel()

    async def wait_closed(self) -> None:
        return None

    def count(self, line: str) -> int:
        return self.received.count(line)

    async def _loop(self) -> None:
        """loop(): strictly one line at a time, in arrival order."""
        loop = asyncio.get_running_loop()
        while True:
            line = await self._rx.get()
            await asyncio.sleep(SERIAL_ROUND_TRIP)
            if loop.time() < self.loop_stalled_until:
                await asyncio.sleep(self.loop_stalled_until - loop.time())
            reply = await self._handle(line)
            self.executed.append((loop.time(), line))
            self.reader.feed_data(f"{json.dumps(reply, separators=(',', ':'))}\r\n".encode())

    async def _handle(self, uri: str) -> dict:
        if uri in ("/bulbs", "/bulbs/"):
            return {
                "bulbs": [
                    {"id": i, "name": name, "address": f"aa:00:{i}", "connected": up}
                    for i, (name, up) in enumerate(self.bulbs.items())
                ],
                "count": len(self.bulbs),
            }
        if not uri.startswith("/bulb/"):
            return {"error": "Endpoint not found"}

        _, _, name, action, *_ = uri.split("/")
        if name not in self.bulbs:
            return {"error": "Bulb not found"}
        if await self._write(name):
            return {"success": True, "action": action}
        return {"error": "Command failed"}

    async def _write(self, name: str) -> bool:
        """writeWithRetry()."""
        if self.firmware == "fixed":
            return self.bulbs[name]
        if not self.bulbs[name]:
            await asyncio.sleep(self.dead_bulb_stall)
            return False

        loop = asyncio.get_running_loop()
        for _ in range(MAX_RETRY_ATTEMPTS):
            held_for = self.monitor_holds_mutex_until - loop.time()
            if held_for <= 0:
                return True
            if held_for <= WRITE_TIMEOUT:
                await asyncio.sleep(held_for)
                return True
            await asyncio.sleep(WRITE_TIMEOUT)
        return False


def attach(api, esp: FakeEsp32) -> None:
    """Point an API client at a fake board without opening a port."""
    api._reader = esp.reader
    api._writer = esp
    api._connected = True
