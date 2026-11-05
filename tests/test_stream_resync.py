"""Junk on the serial stream must be discarded, never parsed, and logged once."""
from __future__ import annotations

import asyncio
import logging

import pytest

from custom_components.esp32_bulb_relay.api import (
    ESP32BulbRelayOutOfSyncError,
    ESP32BulbRelaySerialApi,
    ESP32BulbRelayTimeoutError,
)
from custom_components.esp32_bulb_relay.const import (
    JUNK_SAMPLE_BYTES,
    RESYNC_MAX_TIME,
    RESYNC_QUIET_GAP,
)
from fake_esp32 import ROM_BANNER, FakeEsp32, attach

PORT = "/dev/fake0"
BOARD = {"Sky1": True, "Sky2": True}
EMPTY_REPLY = b'{"bulbs":[],"count":0}'  # Parsing this would empty the board in HA


def make_api(esp: FakeEsp32) -> ESP32BulbRelaySerialApi:
    api = ESP32BulbRelaySerialApi(PORT)
    attach(api, esp)
    return api


def names(bulbs: list[dict]) -> list[str]:
    return [bulb["name"] for bulb in bulbs]


def junk_warnings(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and "out of sync" in record.getMessage()
    ]


def test_backlog_over_the_clear_limit_then_silence(run, caplog):
    junk = bytes(range(256)) * 300

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        api = make_api(esp)
        esp.reader.feed_data(junk)
        return await api.get_bulbs(timeout=5)

    assert names(run(scenario())) == list(BOARD)
    warnings = junk_warnings(caplog)
    assert len(warnings) == 1
    assert repr(junk[:JUNK_SAMPLE_BYTES]) in warnings[0].getMessage()


def test_flood_that_outlasts_the_clear_then_silence(run, caplog):
    """At line rate the clear's time limit comes long before its 64 KB limit."""

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        api = make_api(esp)
        esp.start_flood(EMPTY_REPLY + b"\r\n", seconds=3.0)
        return await api.get_bulbs(timeout=5), esp

    bulbs, esp = run(scenario())
    assert names(bulbs) == list(BOARD)
    assert esp.count("/bulbs") == 1
    assert len(junk_warnings(caplog)) == 1


def test_endless_flood_fails_in_bounded_time_and_warns_once(run, caplog):
    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        api = make_api(esp)
        esp.start_flood(b"\xaa" * 64)
        loop = asyncio.get_running_loop()
        durations = []
        for _ in range(5):
            start = loop.time()
            with pytest.raises(ESP32BulbRelayTimeoutError):
                await api.get_bulbs(timeout=5)
            durations.append(loop.time() - start)
        return esp, durations

    esp, durations = run(scenario())
    assert max(durations) <= 2.0 + RESYNC_MAX_TIME + 2 * RESYNC_QUIET_GAP
    assert esp.received == [], "nothing is written into a stream that won't go quiet"
    assert len(junk_warnings(caplog)) == 1


def test_junk_only_the_resync_notices_gets_its_warning(run, caplog):
    """After a reset the episode restarts, and a slow drip passes the clear unnoticed."""

    async def drip(esp: FakeEsp32) -> None:
        while True:
            esp.reader.feed_data(b"\xaa" * 64)
            await asyncio.sleep(0.15)  # Longer than the clear waits, shorter than the resync

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        api = make_api(esp)
        api._out_of_sync = True
        dripping = asyncio.create_task(drip(esp))
        with pytest.raises(ESP32BulbRelayOutOfSyncError):
            await api.get_bulbs(timeout=5)
        dripping.cancel()

    run(scenario())
    warnings = junk_warnings(caplog)
    assert len(warnings) == 1
    assert repr(b"\xaa" * JUNK_SAMPLE_BYTES) in warnings[0].getMessage()


def test_lines_of_junk_with_pauses_are_a_flood_too(run, caplog):
    """Pauses longer than the clear waits let a command through, into a reset loop's banners."""

    async def banners(esp: FakeEsp32) -> None:
        while True:
            esp.reader.feed_data(ROM_BANNER * 8)
            await asyncio.sleep(0.15)

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        esp.hung = True
        api = make_api(esp)
        task = asyncio.create_task(banners(esp))
        with pytest.raises(ESP32BulbRelayOutOfSyncError):
            await api.get_bulbs(timeout=5)
        task.cancel()
        return api

    assert run(scenario()).out_of_sync is True
    assert len(junk_warnings(caplog)) == 1


def test_a_single_boot_banner_is_no_flood(run):
    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        esp.hung = True
        api = make_api(esp)
        asyncio.get_running_loop().call_later(0.5, esp.reader.feed_data, ROM_BANNER)
        with pytest.raises(ESP32BulbRelayTimeoutError):
            await api.get_bulbs(timeout=2)
        return api

    assert run(scenario()).out_of_sync is False


def test_a_good_reply_ends_the_episode(run, caplog):
    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        api = make_api(esp)
        esp.start_flood(b"\xaa" * 64)
        with pytest.raises(ESP32BulbRelayTimeoutError):
            await api.get_bulbs(timeout=5)
        esp.stop_flood()
        await asyncio.sleep(1)
        bulbs = await api.get_bulbs(timeout=5)
        esp.start_flood(b"\xbb" * 64)
        with pytest.raises(ESP32BulbRelayTimeoutError):
            await api.get_bulbs(timeout=5)
        return bulbs

    assert names(run(scenario())) == list(BOARD)
    warnings = junk_warnings(caplog)
    assert len(warnings) == 2
    assert repr(b"\xbb" * JUNK_SAMPLE_BYTES) in warnings[1].getMessage()


def test_the_tail_of_an_overlong_line_is_not_a_reply(run, caplog):
    """8193 bytes used to end the line, leaving the rest to parse as a fresh one."""

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        api = make_api(esp)
        esp.reply_junk = b"\xaa" * 8193 + EMPTY_REPLY + b"\r\n"
        with pytest.raises(ESP32BulbRelayTimeoutError):
            await api.get_bulbs(timeout=5)
        return await api.get_bulbs(timeout=5)

    assert names(run(scenario())) == list(BOARD)
    warnings = junk_warnings(caplog)
    assert len(warnings) == 1
    assert "line too long" in warnings[0].getMessage()
    assert repr(b"\xaa" * JUNK_SAMPLE_BYTES) in warnings[0].getMessage()


@pytest.mark.parametrize(
    "fragment",
    [EMPTY_REPLY, b'{"bu\xfflbs":[],"count":0}\r\n'],
    ids=["no line ending", "bad byte dropped to leave valid JSON"],
)
def test_partial_garbage_is_never_taken_for_a_reply(run, fragment):
    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        api = make_api(esp)
        esp.hung = True
        asyncio.get_running_loop().call_later(0.2, esp.reader.feed_data, fragment)
        with pytest.raises(ESP32BulbRelayTimeoutError):
            await api.get_bulbs(timeout=2)
        esp.hung = False
        return await api.get_bulbs(timeout=2)

    assert names(run(scenario())) == list(BOARD)


def test_a_control_command_waits_out_the_junk_too(run, caplog):
    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        api = make_api(esp)
        esp.reader.feed_data(b"\xaa" * 70000)
        return await api.turn_on("Sky1")

    assert run(scenario()) == {"success": True, "action": "on"}
    assert len(junk_warnings(caplog)) == 1
