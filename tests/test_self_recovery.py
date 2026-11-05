"""Troubled boards are recovered carefully, and without any manual step.

Times are virtual seconds. The polling helper matches HA's cadence: the next refresh is
scheduled UPDATE_INTERVAL after the last one finished.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import serial

from custom_components.esp32_bulb_relay import api as api_module
from custom_components.esp32_bulb_relay import async_setup_entry, async_unload_entry
from custom_components.esp32_bulb_relay.api import (
    ESP32BulbRelayConnectionError,
    ESP32BulbRelayRecoveringError,
    ESP32BulbRelaySerialApi,
)
from custom_components.esp32_bulb_relay.const import (
    CONF_BULBS,
    CONF_SERIAL_PORTS,
    DOMAIN,
    JUNK_SAMPLE_BYTES,
    MAX_MISSED_SCANS,
    POLL_TIMEOUT,
    RECOVERY_BOOT_TIMEOUT,
    RECOVERY_BOOT_WAIT,
    RECOVERY_HOLD,
    RESET_AFTER_MISSED_SCANS,
    RESET_BACKOFF_INITIAL,
    RESET_BACKOFF_MAX,
    UPDATE_INTERVAL,
)
from custom_components.esp32_bulb_relay.coordinator import ESP32BulbRelayCoordinator
from fake_esp32 import (
    BOOT_TIME,
    DEAD_BULB_STALL,
    FakeEsp32,
    FakeHass,
    FakeSerial,
    SharedSupply,
    attach,
)

PORT = "/dev/fake0"
BOARD = {"Lamp": False, "Sky1": True, "Sky2": True}
SCAN_CYCLE = UPDATE_INTERVAL + POLL_TIMEOUT + 1  # A failed scan and the wait after it
LOGGER = "custom_components.esp32_bulb_relay.coordinator"
API_LOGGER = "custom_components.esp32_bulb_relay.api"


def make_coordinator(*boards: FakeEsp32) -> ESP32BulbRelayCoordinator:
    ports = [f"/dev/fake{i}" for i in range(len(boards))]
    coordinator = ESP32BulbRelayCoordinator(FakeHass(), ports)
    for port, esp in zip(ports, boards):
        api = ESP32BulbRelaySerialApi(port)
        attach(api, esp)
        coordinator._apis[port] = api
    return coordinator


def looping_boards(count: int, gaps: bool = False) -> tuple[SharedSupply, list[FakeEsp32]]:
    """Boards on one supply, not yet powered up."""
    supply = SharedSupply(gaps=gaps)
    boards = [
        FakeEsp32({f"Sky{i}": True}, firmware="fixed", supply=supply) for i in range(count)
    ]
    return supply, boards


async def polls(coordinator: ESP32BulbRelayCoordinator, count: int) -> None:
    for _ in range(count):
        await coordinator._async_update_data()
        await asyncio.sleep(UPDATE_INTERVAL)


async def poll_until(coordinator, esp: FakeEsp32, holds: int) -> None:
    while len(esp.holds) < holds:
        await polls(coordinator, 1)


async def hang_after_a_good_poll(coordinator, esp: FakeEsp32) -> None:
    await polls(coordinator, 1)
    esp.hung = True


def warnings_of(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def assert_running(esp: FakeEsp32) -> None:
    assert (esp.serial.dtr, esp.serial.rts) == (False, False), "a run state, EN and IO0 high"
    assert esp.serial.in_reset is False


# The model reproduces the field failure


def test_supply_model_loops_together_and_boots_alone(run):
    async def scenario():
        supply, _ = looping_boards(3)
        supply.power_up()
        await asyncio.sleep(60)
        lone_supply, _ = looping_boards(1)
        lone_supply.power_up()
        await asyncio.sleep(60)
        return supply, lone_supply

    supply, lone_supply = run(scenario())
    assert supply.brownouts > 100
    assert lone_supply.brownouts == 0


def test_resetting_one_board_at_a_time_does_not_break_the_loop(run):
    """Field: pulses 6 s apart, repeated with backoff. The other two keep the loop going."""

    async def scenario():
        supply, boards = looping_boards(3)
        supply.power_up()
        for esp in boards * 3:
            esp.serial.dtr = False
            esp.serial.rts = True
            await asyncio.sleep(0.1)
            esp.serial.rts = False
            await asyncio.sleep(6)
        return boards

    assert all(esp.in_boot_spike for esp in run(scenario()))


# Coordinated recovery


@pytest.mark.parametrize("gaps", [False, True], ids=["continuous flood", "pauses in the flood"])
def test_coordinated_recovery_breaks_the_loop(run, gaps):
    async def scenario():
        supply, boards = looping_boards(3, gaps=gaps)
        coordinator = make_coordinator(*boards)
        supply.power_up()
        await coordinator._async_update_data()
        for esp in boards:
            esp.sent_while_booting.clear()  # The scan that found the loop is allowed
        await coordinator._recovery_task
        brownouts = supply.brownouts
        await polls(coordinator, 2)
        return supply, boards, coordinator, brownouts

    supply, boards, coordinator, brownouts = run(scenario())
    assert coordinator.all_ports_online
    assert supply.brownouts == brownouts, "the loop stayed broken"
    for one, other in zip(boards, boards[1:]):
        assert other.resets[-1] - one.resets[-1] >= BOOT_TIME, "the first finished booting"
    for esp in boards:
        assert len(esp.holds) == 1
        assert esp.sent_while_held == []
        assert esp.sent_while_booting == []
        assert_running(esp)


def test_every_troubled_board_is_held_for_the_hold_time_before_any_release(run):
    async def scenario():
        supply, boards = looping_boards(2)
        coordinator = make_coordinator(*boards)
        supply.power_up()
        await coordinator._async_update_data()
        await coordinator._recovery_task
        return boards

    first, second = run(scenario())
    assert first.holds == second.holds
    held_at = first.holds[0]
    assert first.resets[-1] == held_at + RECOVERY_HOLD
    assert second.resets[-1] > first.resets[-1]


def test_healthy_board_is_untouched_while_its_siblings_recover(run):
    async def scenario():
        supply, boards = looping_boards(2)
        healthy = FakeEsp32({"Sky9": True}, firmware="fixed")
        coordinator = make_coordinator(*boards, healthy)
        supply.power_up()
        online = []
        for _ in range(4):
            data = await coordinator._async_update_data()
            online.append(data["ports"]["/dev/fake2"]["online"])
            await asyncio.sleep(UPDATE_INTERVAL)
        return coordinator, healthy, online

    coordinator, healthy, online = run(scenario())
    assert coordinator.all_ports_online
    assert all(online)
    assert healthy.resets == [] and healthy.holds == []
    assert healthy.serial.changes == [], "a healthy board's control lines are never touched"
    assert coordinator.get_bulb_port("Sky9") == "/dev/fake2"


def test_board_that_does_not_come_up_goes_back_in_hold_while_the_next_boots(run, caplog):
    async def scenario():
        broken = FakeEsp32(dict(BOARD), firmware="fixed")
        broken.stays_hung = True
        good = FakeEsp32({"Sky3": True}, firmware="fixed")
        coordinator = make_coordinator(broken, good)
        broken.start_flood(b"\xaa" * 64)
        good.start_flood(b"\xaa" * 64)
        await coordinator._async_update_data()
        await coordinator._recovery_task
        return coordinator, broken, good

    coordinator, broken, good = run(scenario())
    in_hold_after = broken.holds[1] - broken.resets[0]
    assert RECOVERY_BOOT_TIMEOUT <= in_hold_after <= RECOVERY_BOOT_TIMEOUT + 2, "bounded"
    assert good.resets == [broken.holds[1]], "the next board goes once the first is held"
    assert broken.resets[1] > good.resets[0] + BOOT_TIME, "released once the good one is up"
    assert_running(broken)
    assert list(coordinator._resets) == ["/dev/fake0"]
    assert coordinator._port_online["/dev/fake1"] is True
    assert sum("/dev/fake0 did not come up" in text for text in warnings_of(caplog)) == 1


def test_boards_released_after_a_failed_pass_still_boot_apart(run):
    async def scenario():
        supply, boards = looping_boards(3)
        for esp in boards:
            esp.stays_hung = True
        coordinator = make_coordinator(*boards)
        supply.power_up()
        await coordinator._async_update_data()
        await coordinator._recovery_task
        return supply, boards

    supply, boards = run(scenario())
    last_releases = sorted(esp.resets[-1] for esp in boards)
    assert all(
        later - earlier >= RECOVERY_BOOT_WAIT
        for earlier, later in zip(last_releases, last_releases[1:])
    )
    for esp in boards:
        assert_running(esp)


def test_slow_board_gets_its_chance_when_the_pass_ends(run, caplog):
    """A dead bulb can keep setup() past the timeout, on a board with nothing wrong."""

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        esp.setup_time = RECOVERY_BOOT_TIMEOUT + 11
        coordinator = make_coordinator(esp)
        esp.start_flood(b"\xaa" * 64)
        await polls(coordinator, 4)
        return coordinator, esp

    with caplog.at_level(logging.INFO, logger=LOGGER):
        coordinator, esp = run(scenario())
    assert coordinator.all_ports_online
    assert coordinator._resets == {}
    assert len(esp.resets) == 2
    assert any("answering again" in r.getMessage() for r in caplog.records)


def test_first_probe_waits_past_the_setup_prompt(run):
    """Older builds read serial for 's' for about 6 s, and /bulbs contains an 's'."""

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        esp.setup_time = RECOVERY_BOOT_WAIT - 0.5
        api = ESP32BulbRelaySerialApi(PORT)
        attach(api, esp)
        await api.hold_in_reset()
        assert await api.release_and_wait(RECOVERY_BOOT_TIMEOUT) is True
        return esp

    esp = run(scenario())
    assert esp.sent_while_booting == []
    assert esp.received == ["/bulbs"]


def test_lines_hold_then_release_and_never_pull_io0_low(run):
    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        api = ESP32BulbRelaySerialApi(PORT)
        attach(api, esp)
        assert await api.hold_in_reset() is True
        await asyncio.sleep(0)
        held = esp.serial.in_reset, list(esp.holds), list(esp.resets)
        await asyncio.sleep(5)
        assert await api.release_and_wait(RECOVERY_BOOT_TIMEOUT) is True
        return esp, held

    esp, held = run(scenario())
    assert held == (True, [0.0], [])
    assert esp.serial.changes == [
        ("dtr", False),
        ("rts", True),
        ("dtr", False),
        ("rts", False),
    ]
    assert esp.resets == [5.0]
    assert esp.download_mode is False
    assert_running(esp)

    dtr, rts = True, True
    for line, value in esp.serial.changes:
        dtr, rts = (value, rts) if line == "dtr" else (dtr, value)
        assert not (dtr and not rts), "that state pulls IO0 low"


def test_control_lines_are_driven_off_the_event_loop(monkeypatch):
    """On a real loop and a real executor: pyserial's ioctls can block."""
    monkeypatch.setattr(api_module, "RECOVERY_BOOT_WAIT", 0)

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        esp.setup_time = 0
        api = ESP32BulbRelaySerialApi(PORT)
        attach(api, esp)
        await api.hold_in_reset()
        await api.release_and_wait(5)
        esp.close()
        return esp.serial.threads

    threads = asyncio.run(scenario())
    assert threads and threading.get_ident() not in threads


def test_port_stays_closed_to_everything_else_during_a_recovery(run):
    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        coordinator = make_coordinator(esp)
        await polls(coordinator, 1)
        api = coordinator.get_api(PORT)
        api._out_of_sync = True
        missed = coordinator._missed_scans[PORT] = 1  # The scan that found the flood
        coordinator._async_start_recovery()
        received = len(esp.received)
        loop = asyncio.get_running_loop()
        results = []

        for pause in (1, 5):  # In the hold, then while it boots
            await asyncio.sleep(pause)
            start = loop.time()
            with pytest.raises(ESP32BulbRelayRecoveringError):
                await api.turn_on("Sky1")
            with pytest.raises(ESP32BulbRelayRecoveringError):
                await coordinator.async_send_command("Sky1", "turn_off")
            with pytest.raises(ESP32BulbRelayRecoveringError):
                await api.connect()
            await coordinator.async_rescan_all_ports()
            results.append(loop.time() - start)

        assert len(esp.received) == received, "nothing reached the port"
        assert coordinator._missed_scans[PORT] == missed, "a skipped scan isn't a missed one"
        await coordinator._recovery_task
        return await api.turn_on("Sky1"), results

    reply, durations = run(scenario())
    assert reply == {"success": True, "action": "on"}
    assert max(durations) <= 1, "commands fail fast instead of waiting out the recovery"


# Never left in reset


@pytest.mark.parametrize("stop", ["cancel", "unload"])
@pytest.mark.parametrize(
    "after",
    [
        RECOVERY_HOLD / 2,  # All held
        RECOVERY_HOLD + 1,  # The first board booting, the others held
        RECOVERY_HOLD + RECOVERY_BOOT_WAIT + 2,  # The second board booting
    ],
)
def test_nothing_stays_in_reset_when_a_recovery_stops(run, stop, after):
    async def scenario():
        supply, boards = looping_boards(3)
        coordinator = make_coordinator(*boards)
        apis = list(coordinator._apis.values())
        supply.power_up()
        await coordinator._async_update_data()
        await asyncio.sleep(after)
        assert any(esp.serial.in_reset for esp in boards)
        task = coordinator._recovery_task

        if stop == "cancel":
            task.cancel()
            await asyncio.wait({task})
        else:
            await coordinator.async_shutdown()
        await asyncio.sleep(0)
        return boards, apis, task

    boards, apis, task = run(scenario())
    assert task.cancelled()
    for esp in boards:
        assert_running(esp)
    assert not any(api.recovering for api in apis)


def test_unload_during_setup_of_a_held_short_lived_port_closes_it(run, monkeypatch):
    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        esp.serial.is_open = False
        api = ESP32BulbRelaySerialApi(PORT)
        attach(api, esp)
        held_through = FakeSerial(esp)
        monkeypatch.setattr(serial, "Serial", lambda device, baud_rate: held_through)
        await api.hold_in_reset()
        assert held_through.in_reset
        await api.close()
        return held_through

    held_through = run(scenario())
    assert held_through.in_reset is False
    assert held_through.is_open is False


def test_recovery_cannot_start_once_shutting_down(run):
    """An in-flight poll can finish after the unload began."""

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        esp.start_flood(b"\xaa" * 64)
        coordinator = make_coordinator(esp)
        poll = asyncio.create_task(coordinator._async_update_data())
        await asyncio.sleep(1)
        api = coordinator.get_api(PORT)
        await coordinator.async_shutdown()
        await poll
        await asyncio.sleep(RECOVERY_HOLD)
        return esp, api

    esp, api = run(scenario())
    assert esp.holds == []


def test_cancelling_a_hold_on_its_way_in_still_releases(monkeypatch):
    """The line job can't be recalled, so a release must not overtake it in the executor."""
    real_assert = api_module._assert_hold

    def slow_assert(serial_port):
        time.sleep(0.05)
        return real_assert(serial_port)

    monkeypatch.setattr(api_module, "_assert_hold", slow_assert)

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        api = ESP32BulbRelaySerialApi(PORT)
        attach(api, esp)
        hold = asyncio.create_task(api.hold_in_reset())
        await asyncio.sleep(0.01)
        hold.cancel()
        await asyncio.wait({hold})
        await api.release_hold()
        esp.close()
        return esp

    esp = asyncio.run(scenario())
    assert_running(esp)


# When to start


def test_unresponsive_board_gets_exactly_one_recovery(run):
    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        coordinator = make_coordinator(esp)
        await hang_after_a_good_poll(coordinator, esp)
        await polls(coordinator, RESET_AFTER_MISSED_SCANS - 1)
        before = list(esp.resets)
        await polls(coordinator, 3)
        return coordinator, esp, before

    coordinator, esp, before = run(scenario())
    assert before == []
    assert len(esp.holds) == 1
    assert len(esp.resets) == 1
    assert coordinator.all_ports_online
    assert coordinator.get_bulb_port("Sky1") == PORT
    assert coordinator._resets == {}


def test_transient_blip_does_not_recover(run):
    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        coordinator = make_coordinator(esp)
        await hang_after_a_good_poll(coordinator, esp)
        await polls(coordinator, RESET_AFTER_MISSED_SCANS - 1)
        esp.hung = False
        await polls(coordinator, 2)
        return coordinator, esp

    coordinator, esp = run(scenario())
    assert esp.holds == []
    assert coordinator._missed_scans[PORT] == 0


def test_reset_threshold_is_past_the_longest_legitimate_stall():
    assert RESET_AFTER_MISSED_SCANS > MAX_MISSED_SCANS
    assert (RESET_AFTER_MISSED_SCANS - 1) * SCAN_CYCLE > DEAD_BULB_STALL


@pytest.mark.parametrize("stall_starts_before_poll_by", [0.1, 10.0, UPDATE_INTERVAL - 0.1])
def test_board_busy_with_a_dead_bulb_is_not_recovered(run, stall_starts_before_poll_by):
    """The original firmware's 91.5 s stall looks like a hang but ends by itself."""

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="original")
        coordinator = make_coordinator(esp)
        await coordinator._async_update_data()
        await asyncio.sleep(UPDATE_INTERVAL - stall_starts_before_poll_by)
        command = asyncio.create_task(coordinator.async_send_command("Lamp", "turn_on"))
        await asyncio.sleep(stall_starts_before_poll_by)
        await polls(coordinator, 6)
        command.exception()
        return coordinator, esp

    coordinator, esp = run(scenario())
    assert esp.holds == []
    assert coordinator.all_ports_online


def test_board_still_in_setup_after_power_up_is_left_alone(run, monkeypatch):
    """setup() can take 90+ s, and silence at startup isn't a flood."""

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        esp.hung = True

        async def open_port(**kwargs):
            return esp.reader, esp

        monkeypatch.setattr(api_module.serial_asyncio, "open_serial_connection", open_port)
        coordinator = ESP32BulbRelayCoordinator(FakeHass(), [PORT])
        api = ESP32BulbRelaySerialApi(PORT)
        coordinator._apis[PORT] = api
        await api.connect()
        await coordinator.async_rescan_all_ports()
        asyncio.get_running_loop().call_later(95, setattr, esp, "hung", False)
        await polls(coordinator, 5)
        return coordinator, esp

    coordinator, esp = run(scenario())
    assert esp.holds == []
    assert coordinator.all_ports_online


async def set_up_integration(boards: list[FakeEsp32], monkeypatch):
    ports = [f"/dev/fake{i}" for i in range(len(boards))]
    by_port = dict(zip(ports, boards))

    async def open_port(url, baudrate):
        return by_port[url].reader, by_port[url]

    monkeypatch.setattr(api_module.serial_asyncio, "open_serial_connection", open_port)
    hass = MagicMock()
    hass.data = {}
    hass.async_create_background_task = FakeHass().async_create_background_task
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    entry = MagicMock(
        entry_id="entry",
        data={CONF_SERIAL_PORTS: ports},
        options={CONF_BULBS: [name for esp in boards for name in esp.bulbs]},
    )
    assert await async_setup_entry(hass, entry) is True
    return hass, entry, hass.data[DOMAIN]["entry"]


def test_flood_at_startup_goes_straight_to_the_sequence(run, monkeypatch):
    async def scenario():
        supply, boards = looping_boards(3)
        supply.power_up()
        hass, entry, coordinator = await set_up_integration(boards, monkeypatch)
        missed = max(coordinator._missed_scans.values())
        task = coordinator._recovery_task
        assert task is not None and not task.done(), "setup doesn't wait for the recovery"
        await task
        return coordinator, boards, missed

    coordinator, boards, missed = run(scenario())
    assert missed < RESET_AFTER_MISSED_SCANS
    assert coordinator.all_ports_online
    assert all(len(esp.holds) == 1 for esp in boards)


def test_one_flooding_port_at_startup_starts_it_and_leaves_the_rest(run, monkeypatch):
    async def scenario():
        flooding = FakeEsp32({"Sky1": True}, firmware="fixed")
        flooding.start_flood(b"\xaa" * 64)
        healthy = FakeEsp32({"Sky2": True}, firmware="fixed")
        hass, entry, coordinator = await set_up_integration([flooding, healthy], monkeypatch)
        await coordinator._recovery_task
        return coordinator, flooding, healthy

    coordinator, flooding, healthy = run(scenario())
    assert len(flooding.holds) == 1
    assert healthy.serial.changes == []
    assert coordinator.all_ports_online


def test_unloading_during_a_startup_recovery_releases_every_board(run, monkeypatch):
    async def scenario():
        supply, boards = looping_boards(3)
        supply.power_up()
        hass, entry, coordinator = await set_up_integration(boards, monkeypatch)
        await asyncio.sleep(RECOVERY_HOLD / 2)
        assert all(esp.serial.in_reset for esp in boards)
        assert await async_unload_entry(hass, entry) is True
        return boards

    for esp in run(scenario()):
        assert_running(esp)


def test_flood_at_runtime_needs_no_missed_scans_or_silence(run):
    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        coordinator = make_coordinator(esp)
        await coordinator._async_update_data()
        await asyncio.sleep(1)
        esp.start_flood(b"\xaa" * 64)
        await coordinator._async_update_data()
        return coordinator, esp

    coordinator, esp = run(scenario())
    assert coordinator._missed_scans[PORT] == 1
    assert coordinator._recovery_task is not None


def test_concurrent_rescans_start_one_recovery(run):
    """A poll and a rescan_ports service call can overlap."""

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        coordinator = make_coordinator(esp)
        await hang_after_a_good_poll(coordinator, esp)
        await polls(coordinator, RESET_AFTER_MISSED_SCANS - 1)
        await asyncio.gather(
            coordinator.async_rescan_all_ports(), coordinator.async_rescan_all_ports()
        )
        await coordinator._recovery_task
        return esp

    assert len(run(scenario()).holds) == 1


def test_board_that_answered_while_the_hold_waited_is_left_alone(run):
    """A slow command holds the lock; its reply proves the board is alive."""

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        coordinator = make_coordinator(esp)
        coordinator._missed_scans[PORT] = RESET_AFTER_MISSED_SCANS
        esp.loop_stalled_until = 10.0
        command = asyncio.create_task(coordinator._apis[PORT].turn_on("Sky1"))
        await asyncio.sleep(1)
        await coordinator._async_recover([PORT])
        return coordinator, esp, await command

    coordinator, esp, reply = run(scenario())
    assert reply == {"success": True, "action": "on"}
    assert esp.holds == []
    assert coordinator._resets == {}, "a skipped recovery doesn't consume the backoff"
    assert coordinator.get_api(PORT).recovering is False


# Backoff


def test_backoff_doubles_up_to_the_cap(run):
    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        esp.stays_hung = True
        coordinator = make_coordinator(esp)
        await hang_after_a_good_poll(coordinator, esp)
        await poll_until(coordinator, esp, 16)
        return esp

    esp = run(scenario())
    starts = esp.holds[::2]  # Each pass holds it, then again once it doesn't come up
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
    expected = [min(RESET_BACKOFF_INITIAL * 2**i, RESET_BACKOFF_MAX) for i in range(len(gaps))]
    print(f"\nrecovery gaps {[round(gap) for gap in gaps]}, backoff {expected}")
    assert expected[:3] == [90, 180, 360]
    assert expected[-1] == RESET_BACKOFF_MAX
    pass_time = RECOVERY_HOLD + RECOVERY_BOOT_TIMEOUT
    for gap, backoff in zip(gaps, expected):
        assert backoff + pass_time <= gap <= backoff + pass_time + SCAN_CYCLE + 5


def test_due_board_brings_a_sibling_in_backoff_along(run):
    """Holding one and leaving the other to loop could never break a shared loop."""

    async def scenario():
        waiting = FakeEsp32(dict(BOARD), firmware="fixed")
        due = FakeEsp32({"Sky3": True}, firmware="fixed")
        coordinator = make_coordinator(waiting, due)
        await polls(coordinator, 1)
        waiting.start_flood(b"\xaa" * 64)
        waiting.flood_survives_reset = True
        await poll_until(coordinator, waiting, 2)
        assert "/dev/fake0" in coordinator._resets
        await coordinator._recovery_task
        due.start_flood(b"\xaa" * 64)
        await coordinator._async_update_data()
        await asyncio.sleep(0)
        return waiting, due

    waiting, due = run(scenario())
    assert waiting.holds[-1] == due.holds[-1]


def test_recovery_resets_the_backoff(run, caplog):
    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        esp.stays_hung = True
        coordinator = make_coordinator(esp)
        await hang_after_a_good_poll(coordinator, esp)
        await poll_until(coordinator, esp, 6)  # Backoff is now 6 min
        await coordinator._recovery_task
        assert coordinator._resets[PORT][0] == 3

        esp.hung = False
        await polls(coordinator, 3)
        assert coordinator._resets == {}

        hung_at = asyncio.get_running_loop().time()
        esp.hung = True
        await poll_until(coordinator, esp, 7)
        return esp, hung_at

    with caplog.at_level(logging.INFO, logger=LOGGER):
        esp, hung_at = run(scenario())

    assert esp.holds[6] - hung_at < RESET_BACKOFF_INITIAL + RESET_AFTER_MISSED_SCANS * SCAN_CYCLE
    messages = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == LOGGER]
    assert sum("answering again" in text and level == logging.INFO for level, text in messages) == 1


# Logging


def test_a_recovery_logs_its_start_each_board_and_each_failure(run, caplog):
    async def scenario():
        supply, boards = looping_boards(3)
        boards[1].stays_hung = True
        coordinator = make_coordinator(*boards)
        supply.power_up()
        await coordinator._async_update_data()
        await coordinator._recovery_task

    with caplog.at_level(logging.INFO, logger=LOGGER):
        run(scenario())

    records = [r for r in caplog.records if r.name == LOGGER]
    started = [r for r in records if "Coordinated recovery" in r.getMessage()]
    assert len(started) == 1
    assert started[0].levelno == logging.WARNING
    for port in ("/dev/fake0", "/dev/fake1", "/dev/fake2"):
        assert port in started[0].getMessage()

    answering = [r.getMessage() for r in records if "released and answering" in r.getMessage()]
    assert [text.split()[2] for text in answering] == ["/dev/fake0", "/dev/fake2"]
    assert all(r.levelno == logging.INFO for r in records if "released and" in r.getMessage())

    failed = [r for r in records if "did not come up" in r.getMessage()]
    assert len(failed) == 1
    assert failed[0].levelno == logging.WARNING
    assert "/dev/fake1" in failed[0].getMessage()


def test_outage_logs_its_failed_scan_and_timeout_once_per_port(run, caplog):
    async def scenario():
        one = FakeEsp32({"Sky1": True}, firmware="fixed")
        other = FakeEsp32({"Sky2": True}, firmware="fixed")
        coordinator = make_coordinator(one, other)
        await polls(coordinator, 1)
        one.hung = other.hung = True
        await polls(coordinator, 3)
        one.hung = other.hung = False
        await polls(coordinator, 1)
        one.hung = True
        await polls(coordinator, 2)

    with caplog.at_level(logging.DEBUG, logger=API_LOGGER), caplog.at_level(
        logging.DEBUG, logger=LOGGER
    ):
        run(scenario())

    def count(text: str, port: str, level: int) -> int:
        return sum(
            text in r.getMessage() and port in r.getMessage() and r.levelno == level
            for r in caplog.records
        )

    # One for each outage of /dev/fake0, and one for the single outage of /dev/fake1
    assert count("Failed to scan port", "/dev/fake0", logging.WARNING) == 2
    assert count("Failed to scan port", "/dev/fake1", logging.WARNING) == 1
    assert count("Overall timeout", "/dev/fake0", logging.ERROR) == 2
    assert count("Overall timeout", "/dev/fake1", logging.ERROR) == 1
    # The rest are still there at DEBUG
    assert count("Failed to scan port", "/dev/fake0", logging.DEBUG) == 3
    assert count("Overall timeout", "/dev/fake0", logging.DEBUG) == 3


def test_flooding_board_warns_for_the_episode_and_the_recovery_only(run, caplog):
    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        coordinator = make_coordinator(esp)
        await polls(coordinator, 1)
        esp.start_flood(b"\xaa" * 64)
        await poll_until(coordinator, esp, 1)
        await polls(coordinator, 1)
        return coordinator, esp

    coordinator, esp = run(scenario())
    assert len(esp.holds) == 1
    assert coordinator.all_ports_online
    warnings = warnings_of(caplog)
    assert len(warnings) == 2, "one for the episode, one for the recovery, none per scan"
    assert "out of sync" in warnings[0] and repr(b"\xaa" * JUNK_SAMPLE_BYTES) in warnings[0]
    assert "Coordinated recovery" in warnings[1]


def test_flood_that_survives_a_recovery_warns_with_the_backoff_not_the_polls(run, caplog):
    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        coordinator = make_coordinator(esp)
        await polls(coordinator, 1)
        esp.flood_survives_reset = True
        esp.start_flood(b"\xaa" * 64)
        await poll_until(coordinator, esp, 6)
        await coordinator._recovery_task
        return esp

    esp = run(scenario())
    assert len(esp.holds) == 6
    warnings = warnings_of(caplog)
    assert "out of sync" in warnings[0]
    assert [
        ("Coordinated recovery" in text, "did not come up" in text) for text in warnings[1:]
    ] == [(True, False), (False, True)] * 3


def test_reply_lost_in_junk_is_not_a_second_warning(run, caplog):
    """An overlong line fails the scan from the reply path rather than the clear."""

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        coordinator = make_coordinator(esp)
        esp.reply_junk = b"\xaa" * 8193
        await polls(coordinator, 2)
        return coordinator, esp

    coordinator, esp = run(scenario())
    assert coordinator.all_ports_online
    assert esp.holds == [], "the line went quiet again, so this was no flood"
    warnings = warnings_of(caplog)
    assert len(warnings) == 1
    assert "line too long" in warnings[0]


# Ports that have gone away


def test_dead_transport_holds_through_its_own_port(run, monkeypatch):
    """After a USB error the transport has no serial object left to drive."""

    async def scenario():
        dead = FakeEsp32(dict(BOARD), firmware="fixed")
        dead.serial.is_open = False
        api = ESP32BulbRelaySerialApi(PORT)
        attach(api, dead)
        fresh = FakeEsp32(dict(BOARD), firmware="fixed")
        own_ports: list[FakeSerial] = []

        def open_serial(device, baud_rate):
            assert device == PORT
            own_ports.append(FakeSerial(fresh))
            return own_ports[-1]

        async def open_port(**kwargs):
            return fresh.reader, fresh

        monkeypatch.setattr(serial, "Serial", open_serial)
        monkeypatch.setattr(api_module.serial_asyncio, "open_serial_connection", open_port)

        await api.hold_in_reset()
        held = own_ports[0].in_reset
        assert await api.release_and_wait(RECOVERY_BOOT_TIMEOUT) is True
        result = await asyncio.wait_for(api.turn_on("Sky1"), timeout=20)
        return dead, fresh, own_ports, held, result

    dead, fresh, own_ports, held, result = run(scenario())
    assert held is True
    assert result == {"success": True, "action": "on"}
    assert len(own_ports) == 1
    assert own_ports[0].changes == [
        ("dtr", False),
        ("rts", True),
        ("dtr", False),
        ("rts", False),
    ]
    assert own_ports[0].is_open is False
    assert dead.serial.changes == []
    assert len(fresh.resets) == 1
    assert fresh.sent_while_booting == []


def test_port_that_cannot_be_opened_backs_off_without_breaking_the_scan(run, monkeypatch, caplog):
    async def scenario():
        gone = FakeEsp32(dict(BOARD), firmware="fixed")
        gone.serial.is_open = False
        gone.hung = True
        healthy = FakeEsp32({"Sky3": True}, firmware="fixed")
        coordinator = make_coordinator(gone, healthy)
        attempts: list[float] = []
        loop = asyncio.get_running_loop()

        def open_serial(device, baud_rate):
            attempts.append(loop.time())
            raise serial.SerialException("no such device")

        async def open_port(**kwargs):
            raise serial.SerialException("no such device")

        monkeypatch.setattr(serial, "Serial", open_serial)
        monkeypatch.setattr(api_module.serial_asyncio, "open_serial_connection", open_port)
        await polls(coordinator, RESET_AFTER_MISSED_SCANS + 6)
        return coordinator, attempts

    coordinator, attempts = run(scenario())
    assert len(attempts) >= 2
    assert attempts[1] - attempts[0] >= RESET_BACKOFF_INITIAL
    assert coordinator._port_online["/dev/fake1"] is True
    assert coordinator.get_api("/dev/fake0").recovering is False
    assert any("Could not hold /dev/fake0" in text for text in warnings_of(caplog))


def test_failed_hold_of_one_board_does_not_stop_the_others(run, monkeypatch):
    async def scenario():
        gone = FakeEsp32(dict(BOARD), firmware="fixed")
        gone.serial.is_open = False
        good = FakeEsp32({"Sky3": True}, firmware="fixed")
        coordinator = make_coordinator(gone, good)
        gone.start_flood(b"\xaa" * 64)
        good.start_flood(b"\xaa" * 64)

        def open_serial(device, baud_rate):
            raise serial.SerialException("no such device")

        monkeypatch.setattr(serial, "Serial", open_serial)
        await coordinator._async_update_data()
        await coordinator._recovery_task
        return coordinator, good

    coordinator, good = run(scenario())
    assert len(good.holds) == 1
    assert coordinator._port_online["/dev/fake1"] is True
    assert list(coordinator._resets) == ["/dev/fake0"]


def test_hold_error_surfaces_as_an_api_error(run, monkeypatch):
    async def scenario():
        api = ESP32BulbRelaySerialApi(PORT)

        def open_serial(device, baud_rate):
            raise serial.SerialException("no such device")

        monkeypatch.setattr(serial, "Serial", open_serial)
        try:
            await api.hold_in_reset()
        finally:
            assert api.recovering is False

    with pytest.raises(ESP32BulbRelayConnectionError):
        run(scenario())


def test_normal_open_leaves_the_control_lines_at_pyserial_defaults(run, monkeypatch):
    """Both lines asserted is a run state, and hardware confirmed it doesn't reset."""

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        opened: list[dict] = []

        async def open_port(**kwargs):
            opened.append(kwargs)
            return esp.reader, esp

        monkeypatch.setattr(api_module.serial_asyncio, "open_serial_connection", open_port)
        api = ESP32BulbRelaySerialApi(PORT)
        await api.connect()
        return esp, opened

    esp, opened = run(scenario())
    assert opened == [{"url": PORT, "baudrate": 115200}]
    assert esp.serial.changes == []
    assert esp.serial.in_reset is False
    assert (serial.Serial().dtr, serial.Serial().rts) == (True, True)
