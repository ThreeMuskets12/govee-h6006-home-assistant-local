"""Timing model of the ESP32 sketch behind an in-memory serial stream.

This is a model of govee_controller_serial.ino, not the firmware itself. It keeps
what matters to the integration: loop() answers one line at a time, in order, and
stalls for the durations in the sketch and NimBLE. `firmware="original"` models the
recovered sketch; `firmware="fixed"` models the working-tree sketch.
"""
from __future__ import annotations

import asyncio
import json
import threading

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

LINE_RATE = 11520  # Bytes per second at 115200 8N1
BOOT_TIME = 3.0  # Writes this soon after a reset are recorded as a failure
ROM_BANNER = b"ets Jul 29 2019 12:21:57\r\nrst:0x1 (POWERON_RESET),boot:0x13\r\n"

BOOT_SPIKE = 1.0  # How long a boot draws enough current to sag the rail
LOOP_PERIOD = 1 / 6  # Field: about 18 ROM banners in 3 s per board
LOOP_JUNK = b"\x0c|\x8c\x00p\x00"  # Field: the same bytes on all three ports


class FakeHass:
    """The part of HomeAssistant the coordinator needs."""

    def async_create_background_task(self, target, name, eager_start=True) -> asyncio.Task:
        return asyncio.create_task(target, name=name)


class SharedSupply:
    """A marginal 5 V rail: overlapping boots brown each other out, and a lone boot survives.

    A board running or held in reset doesn't count. Boards powered up together therefore
    loop until no two of them are booting at once, which resetting them one at a time
    can't achieve while the others keep the loop going. With gaps=True the line has
    pauses between banners, too short for a quiet line but long enough to pass the clear.
    """

    def __init__(self, gaps: bool = False) -> None:
        self.gaps = gaps
        self.boards: list[FakeEsp32] = []
        self.brownouts = 0
        self._rail_task: asyncio.Task | None = None

    def power_up(self) -> None:
        """All boards start together, as they do when the host comes up."""
        for board in self.boards:
            board.boot()
        self._rail_task = asyncio.create_task(self._rail())

    async def _rail(self) -> None:
        while True:
            await asyncio.sleep(LOOP_PERIOD)
            booting = [board for board in self.boards if board.in_boot_spike]
            if len(booting) >= 2:
                self.brownouts += 1
                for board in booting:
                    board.brown_out()


class FakeSerial:
    """The board's control lines, behind the DevKit's two-transistor auto-reset circuit.

    EN is low only while RTS is asserted and DTR is not; IO0 is low only while DTR is
    asserted and RTS is not. Starts as pyserial's defaults leave a port: both asserted.
    """

    def __init__(self, board: FakeEsp32) -> None:
        self.board = board
        self.is_open = True
        self.changes: list[tuple[str, bool]] = []
        self.threads: set[int] = set()
        self._dtr = True
        self._rts = True

    def close(self) -> None:
        self.is_open = False

    @property
    def in_reset(self) -> bool:
        return self._rts and not self._dtr

    @property
    def dtr(self) -> bool:
        return self._dtr

    @dtr.setter
    def dtr(self, value: bool) -> None:
        self._set("dtr", value)

    @property
    def rts(self) -> bool:
        return self._rts

    @rts.setter
    def rts(self, value: bool) -> None:
        self._set("rts", value)

    def _set(self, line: str, value: bool) -> None:
        was_in_reset = self.in_reset
        setattr(self, f"_{line}", value)
        self.changes.append((line, value))
        self.threads.add(threading.get_ident())
        if not was_in_reset and self.in_reset:
            self.board.en_pulled_low()
        if was_in_reset and not self.in_reset:
            self.board.en_released(io0_low=self._dtr and not self._rts)


class FakeEsp32:
    """One board: exposes the StreamWriter surface and feeds a real StreamReader."""

    def __init__(
        self,
        bulbs: dict[str, bool],
        firmware: str = "original",
        dead_bulb_stall: float = DEAD_BULB_STALL,
        supply: SharedSupply | None = None,
    ) -> None:
        self.bulbs = bulbs  # name -> connected
        self.firmware = firmware
        self.dead_bulb_stall = dead_bulb_stall
        self.reader = asyncio.StreamReader()
        self.received: list[str] = []
        self.executed: list[tuple[float, str]] = []
        self.monitor_holds_mutex_until = 0.0
        self.loop_stalled_until = 0.0
        self.hung = False  # Reads its input but answers nothing, until reset
        self.stays_hung = False  # A reset doesn't help
        self.reply_junk = b""  # Sent once, straight before the next reply
        self.flood_survives_reset = False
        self.serial = FakeSerial(self)
        self.holds: list[float] = []  # When EN went low
        self.resets: list[float] = []  # When a boot started
        self.download_mode = False
        self.setup_time = BOOT_TIME  # From a boot until loop() runs
        self.booting_until = 0.0
        self.sent_while_booting: list[str] = []
        self.sent_while_held: list[str] = []
        self.supply = supply
        if supply is not None:
            supply.boards.append(self)
        self._spike_until = 0.0
        self._loop_ref = asyncio.get_running_loop()
        self._flood_task: asyncio.Task | None = None
        self._flood_junk = b""
        self._rx: asyncio.Queue[str] = asyncio.Queue()
        self._loop_task = asyncio.create_task(self._loop())

    # StreamWriter surface used by api.py
    def write(self, data: bytes) -> None:
        for line in data.decode().splitlines():
            self.received.append(line)
            if self.serial.in_reset:
                self.sent_while_held.append(line)
            elif self._loop_ref.time() < self.booting_until:
                self.sent_while_booting.append(line)
            self._rx.put_nowait(line)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.stop_flood()
        self._loop_task.cancel()

    async def wait_closed(self) -> None:
        return None

    def get_extra_info(self, name: str, default=None):
        return self.serial if name == "serial" else default

    def count(self, line: str) -> int:
        return self.received.count(line)

    def start_flood(self, junk: bytes, seconds: float = float("inf")) -> None:
        """Send junk at line rate, with no pause longer than one 10 ms chunk."""
        self._flood_junk = junk
        self._flood_task = asyncio.create_task(self._flood(junk, seconds))

    def stop_flood(self) -> None:
        if self._flood_task is not None:
            self._flood_task.cancel()

    async def _flood(self, junk: bytes, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        chunk = (junk * (LINE_RATE // 100 // len(junk) + 1))[: LINE_RATE // 100]
        while loop.time() < end:
            self.reader.feed_data(chunk)
            await asyncio.sleep(0.01)

    def en_pulled_low(self) -> None:
        """EN fell. Called from whichever thread drove the line."""
        self._loop_ref.call_soon_threadsafe(self._halt)

    def en_released(self, io0_low: bool) -> None:
        """EN rose. Called from whichever thread drove the line."""
        self._loop_ref.call_soon_threadsafe(self.boot, io0_low)

    @property
    def in_boot_spike(self) -> bool:
        return not self.serial.in_reset and self._loop_ref.time() < self._spike_until

    def _halt(self) -> None:
        """A board in reset runs nothing and sends nothing."""
        self.holds.append(self._loop_ref.time())
        self._spike_until = 0.0
        self.stop_flood()
        self._loop_task.cancel()

    def boot(self, io0_low: bool = False) -> None:
        now = self._loop_ref.time()
        self.resets.append(now)
        self.download_mode = io0_low
        self.booting_until = self.loop_stalled_until = now + self.setup_time
        self._spike_until = now + BOOT_SPIKE
        self.hung = self.stays_hung
        self.stop_flood()
        if self.flood_survives_reset:
            self.start_flood(self._flood_junk)
        self._rx = asyncio.Queue()
        self._loop_task.cancel()
        self._loop_task = self._loop_ref.create_task(self._loop())
        self.reader.feed_data(ROM_BANNER)

    def brown_out(self) -> None:
        """The rail sagged during boot: start again, with junk from the dying UART."""
        self.reader.feed_data(LOOP_JUNK * 64)
        self.boot()
        if not self.supply.gaps:
            self.start_flood(LOOP_JUNK, seconds=2 * LOOP_PERIOD)

    async def _loop(self) -> None:
        """loop(): strictly one line at a time, in arrival order."""
        loop = asyncio.get_running_loop()
        while True:
            line = await self._rx.get()
            await asyncio.sleep(SERIAL_ROUND_TRIP)
            if self.hung:
                continue
            if loop.time() < self.loop_stalled_until:
                await asyncio.sleep(self.loop_stalled_until - loop.time())
            reply = await self._handle(line)
            self.executed.append((loop.time(), line))
            reply_bytes = f"{json.dumps(reply, separators=(',', ':'))}\r\n".encode()
            self.reader.feed_data(self.reply_junk + reply_bytes)
            self.reply_junk = b""

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
