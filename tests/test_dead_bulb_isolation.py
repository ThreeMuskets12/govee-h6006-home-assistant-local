"""A dead bulb must not degrade its siblings on the same board.

Latencies are virtual seconds and print with `pytest -s`.
"""
from __future__ import annotations

import asyncio

import pytest

from custom_components.esp32_bulb_relay.api import (
    ESP32BulbRelayApiError,
    ESP32BulbRelayCommandError,
    ESP32BulbRelaySerialApi,
)
from custom_components.esp32_bulb_relay.const import MIN_COMMAND_INTERVAL
from custom_components.esp32_bulb_relay.coordinator import ESP32BulbRelayCoordinator
from fake_esp32 import SLOW_MODE_CYCLE, FakeEsp32, attach

PORT = "/dev/fake0"
BOARD = {"Lamp": False, "Sky1": True, "Sky2": True}


def make_api(esp: FakeEsp32, port: str = PORT) -> ESP32BulbRelaySerialApi:
    api = ESP32BulbRelaySerialApi(port)
    attach(api, esp)
    return api


def make_coordinator(*boards: FakeEsp32) -> ESP32BulbRelayCoordinator:
    ports = [f"/dev/fake{i}" for i in range(len(boards))]
    coordinator = ESP32BulbRelayCoordinator(None, ports)
    for port, esp in zip(ports, boards):
        coordinator._apis[port] = make_api(esp, port)
    return coordinator


async def timed(coro) -> tuple[float, object]:
    """Return (seconds, result or the raised API error)."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    try:
        result = await coro
    except ESP32BulbRelayApiError as err:
        result = err
    return loop.time() - start, result


async def idle_rate_limit() -> None:
    await asyncio.sleep(MIN_COMMAND_INTERVAL)


def test_sibling_behind_dead_bulb_command_original_firmware(run):
    """Old firmware stalls loop() for 91.5 s. Siblings fail fast and are not left queued."""

    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="original")
        coordinator = make_coordinator(esp)
        await coordinator.async_rescan_all_ports()
        dead = asyncio.create_task(timed(coordinator.async_send_command("Lamp", "turn_on")))
        await asyncio.sleep(0.1)
        sibling = await timed(coordinator.async_send_command("Sky1", "turn_on"))
        dead = await dead
        return esp, dead, sibling

    esp, dead, sibling = run(scenario())
    print(f"\noriginal firmware: dead bulb {dead[0]:.1f}s, sibling {sibling[0]:.1f}s -> {sibling[1]!r}")
    assert isinstance(sibling[1], ESP32BulbRelayApiError)
    assert sibling[0] < 21.5
    assert esp.count("/bulb/Sky1/on") == 0, "command written into a stalled board runs much later"
    assert esp.count("/bulb/Lamp/on") == 1, "a timed-out command is still queued on the board"


def test_sibling_behind_dead_bulb_command_fixed_firmware(run):
    async def scenario():
        esp = FakeEsp32(dict(BOARD), firmware="fixed")
        coordinator = make_coordinator(esp)
        await coordinator.async_rescan_all_ports()
        await idle_rate_limit()
        dead = asyncio.create_task(timed(coordinator.async_send_command("Lamp", "turn_on")))
        await asyncio.sleep(0.01)
        sibling = await timed(coordinator.async_send_command("Sky1", "turn_on"))
        dead = await dead
        return dead, sibling

    dead, sibling = run(scenario())
    print(f"\nfixed firmware: dead bulb {dead[0]:.2f}s, sibling {sibling[0]:.2f}s -> {sibling[1]!r}")
    assert isinstance(dead[1], ESP32BulbRelayCommandError)
    assert dead[0] < 0.5
    assert sibling[1] == {"success": True, "action": "on"}
    assert sibling[0] < 1.0


def test_late_reply_is_not_taken_for_the_next_command(run):
    """A reply that arrives after the timeout belongs to the timed-out command."""

    async def scenario():
        esp = FakeEsp32(dict(BOARD), dead_bulb_stall=20.0)
        api = make_api(esp)
        timed_out = await timed(api.turn_on("Lamp"))
        sibling = await timed(api.turn_on("Sky1"))
        await idle_rate_limit()
        esp.bulbs["Sky2"] = False
        esp.dead_bulb_stall = 0
        next_command = await timed(api.turn_on("Sky2"))
        return timed_out, sibling, next_command

    timed_out, sibling, next_command = run(scenario())
    print(f"\ntimed out at {timed_out[0]:.1f}s; next sibling {sibling[0]:.1f}s -> {sibling[1]!r}")
    assert sibling[1] == {"success": True, "action": "on"}
    assert isinstance(next_command[1], ESP32BulbRelayCommandError)


def test_late_control_reply_is_not_taken_for_a_poll(run):
    """A stale {"error": ...} read as the /bulbs reply used to empty the whole board."""

    async def scenario():
        esp = FakeEsp32(dict(BOARD), dead_bulb_stall=17.0)
        api = make_api(esp)
        await timed(api.turn_on("Lamp"))
        return await api.get_bulbs(timeout=5)

    bulbs = run(scenario())
    assert [bulb["name"] for bulb in bulbs] == list(BOARD)


def test_late_poll_reply_is_not_taken_for_a_command(run):
    async def scenario():
        esp = FakeEsp32(dict(BOARD))
        api = make_api(esp)
        esp.loop_stalled_until = 5.5
        with pytest.raises(ESP32BulbRelayApiError):
            await api.get_bulbs(timeout=5)
        await idle_rate_limit()
        return await api.turn_on("Sky1")

    assert run(scenario()) == {"success": True, "action": "on"}


def test_board_that_never_answers_a_timed_out_command_recovers(run):
    """A reset board drops the pending command; the next /bulbs reply resynchronizes."""

    async def scenario():
        esp = FakeEsp32(dict(BOARD))
        api = make_api(esp)
        api._resync_needed = True
        return await api.turn_on("Sky1")

    assert run(scenario()) == {"success": True, "action": "on"}


def test_command_failure_does_not_rescan_or_retry(run):
    """The firmware already retried; a failure says nothing about which port owns the bulb."""

    async def scenario():
        board = FakeEsp32(dict(BOARD), firmware="fixed")
        other = FakeEsp32({"Sky3": True}, firmware="fixed")
        coordinator = make_coordinator(board, other)
        await coordinator.async_rescan_all_ports()
        await idle_rate_limit()
        polls_before = board.count("/bulbs"), other.count("/bulbs")
        result = await timed(coordinator.async_send_command("Lamp", "turn_on"))
        polls = board.count("/bulbs") - polls_before[0], other.count("/bulbs") - polls_before[1]
        return board, result, polls

    board, result, polls = run(scenario())
    print(f"\nfast failure: {result[0]:.2f}s, extra polls per port {polls}")
    assert isinstance(result[1], ESP32BulbRelayCommandError)
    assert polls == (0, 0)
    assert board.count("/bulb/Lamp/on") == 1


def test_healthy_bulb_gets_one_retry_without_a_rescan(run):
    """The original firmware fails healthy bulbs while it reconnects a dead sibling."""

    async def scenario():
        board = FakeEsp32(dict(BOARD), firmware="original")
        other = FakeEsp32({"Sky3": True}, firmware="original")
        coordinator = make_coordinator(board, other)
        await coordinator.async_rescan_all_ports()
        await idle_rate_limit()
        board.monitor_holds_mutex_until = asyncio.get_running_loop().time() + 12.0
        result = await timed(coordinator.async_send_command("Sky1", "turn_on"))
        return board, other, result

    board, other, result = run(scenario())
    assert result[1] == {"success": True, "action": "on"}
    assert board.count("/bulb/Sky1/on") == 2
    assert (board.count("/bulbs"), other.count("/bulbs")) == (1, 1)


def test_timeout_does_not_rescan_or_retry(run):
    async def scenario():
        board = FakeEsp32(dict(BOARD), firmware="original")
        other = FakeEsp32({"Sky3": True}, firmware="original")
        coordinator = make_coordinator(board, other)
        await coordinator.async_rescan_all_ports()
        await idle_rate_limit()
        result = await timed(coordinator.async_send_command("Lamp", "turn_on"))
        return board, other, result

    board, other, result = run(scenario())
    print(f"\ntimeout path: {result[0]:.1f}s, /bulbs on other port {other.count('/bulbs')}")
    assert result[0] < 16
    assert other.count("/bulbs") == 1
    assert board.count("/bulb/Lamp/on") == 1


def test_bulb_moved_to_another_port_is_still_found(run):
    """`Bulb not found` is the one failure a rescan can fix."""

    async def scenario():
        old_home = FakeEsp32({"Sky1": True}, firmware="fixed")
        new_home = FakeEsp32({"Sky2": True}, firmware="fixed")
        coordinator = make_coordinator(old_home, new_home)
        await coordinator.async_rescan_all_ports()
        await idle_rate_limit()
        del old_home.bulbs["Sky1"]
        new_home.bulbs["Sky1"] = True
        return coordinator, await coordinator.async_send_command("Sky1", "turn_on")

    coordinator, result = run(scenario())
    assert result == {"success": True, "action": "on"}
    assert coordinator.get_bulb_port("Sky1") == "/dev/fake1"


def test_stalled_poll_keeps_siblings_routed_and_available(run):
    """A missed poll means the board is busy, not that its bulbs are gone."""

    async def scenario():
        esp = FakeEsp32(dict(BOARD))
        coordinator = make_coordinator(esp)
        await coordinator._async_update_data()
        esp.loop_stalled_until = 100.0
        data = await coordinator._async_update_data()
        return coordinator, data

    coordinator, data = run(scenario())
    assert data["ports"][PORT]["online"] is False
    assert set(data["bulbs"]) == set(BOARD)
    assert data["bulbs"]["Sky1"]["connected"] is True
    assert coordinator.get_bulb_port("Sky1") == PORT


def test_port_that_stays_offline_eventually_drops_its_bulbs(run):
    async def scenario():
        esp = FakeEsp32(dict(BOARD))
        coordinator = make_coordinator(esp)
        await coordinator.async_rescan_all_ports()
        esp.loop_stalled_until = 1000.0
        survived = []
        for _ in range(5):
            await coordinator.async_rescan_all_ports()
            survived.append(coordinator.get_bulb_port("Sky1") is not None)
        return survived

    assert run(scenario()) == [True, True, True, False, False]


def test_fresh_report_from_another_port_beats_the_cached_entry(run):
    async def scenario():
        stalled = FakeEsp32({"Sky1": True})
        other = FakeEsp32({"Sky2": True})
        coordinator = make_coordinator(stalled, other)
        await coordinator.async_rescan_all_ports()
        stalled.loop_stalled_until = 100.0
        other.bulbs["Sky1"] = True
        await coordinator.async_rescan_all_ports()
        return coordinator

    assert run(scenario()).get_bulb_port("Sky1") == "/dev/fake1"


@pytest.mark.parametrize(
    ("firmware", "dead_bulbs"), [("original", 1), ("original", 2), ("fixed", 1)]
)
def test_sibling_latency_across_the_reconnect_cycle(run, firmware, dead_bulbs):
    """Sweep a sibling command across one slow-mode cycle, as HA sees it.

    The dead bulb is unavailable in HA, so no command targets it; this is the steady
    state for a bulb on a wall switch. The monitor holds bleMutex for one 30 s connect
    per dead bulb in each 60.5 s cycle (original firmware).
    """

    async def sample(offset: float) -> tuple[float, object]:
        esp = FakeEsp32(dict(BOARD), firmware=firmware)
        coordinator = make_coordinator(esp)
        await coordinator.async_rescan_all_ports()
        await idle_rate_limit()
        hold = min(30.0 * dead_bulbs - offset, SLOW_MODE_CYCLE - offset)
        esp.monitor_holds_mutex_until = asyncio.get_running_loop().time() + hold
        return await timed(coordinator.async_send_command("Sky1", "turn_on"))

    results = [run(sample(offset)) for offset in [i * 0.5 for i in range(int(SLOW_MODE_CYCLE * 2))]]
    latencies = sorted(seconds for seconds, _ in results)
    failed = sum(isinstance(result, ESP32BulbRelayApiError) for _, result in results)
    slow = sum(seconds > 1.0 for seconds in latencies)
    print(
        f"\n{firmware} firmware, {dead_bulbs} dead: n={len(results)} failed={failed / len(results):.0%} "
        f">1s={slow / len(results):.0%} median={latencies[len(latencies) // 2]:.2f}s max={latencies[-1]:.2f}s"
    )
    if firmware == "fixed":
        assert failed == 0
        assert latencies[-1] < 1.0
