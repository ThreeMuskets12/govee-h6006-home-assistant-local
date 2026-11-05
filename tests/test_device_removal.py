"""Removing a bulb's device from Home Assistant."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.esp32_bulb_relay import (
    _async_options_updated,
    async_remove_config_entry_device,
)
from custom_components.esp32_bulb_relay.api import ESP32BulbRelaySerialApi
from custom_components.esp32_bulb_relay.const import CONF_BULBS, CONF_SERIAL_PORTS, DOMAIN
from custom_components.esp32_bulb_relay.coordinator import ESP32BulbRelayCoordinator
from fake_esp32 import FakeEsp32, attach

PORT = "/dev/fake0"


class FakeConfigEntries:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.reloads = 0

    def async_update_entry(self, entry, options) -> None:
        entry.options = options
        self.updates.append(options)

    async def async_reload(self, entry_id) -> None:
        self.reloads += 1


def device(*identifiers):
    return SimpleNamespace(name="device", identifiers=set(identifiers))


def bulb_device(name: str):
    return device((DOMAIN, f"bulb_{name}"))


async def setup(enabled: list[str], board: dict[str, bool] | None = None):
    esp = FakeEsp32({"Sky1": True, "Sky2": False} if board is None else board, firmware="fixed")
    coordinator = ESP32BulbRelayCoordinator(None, [PORT])
    api = ESP32BulbRelaySerialApi(PORT)
    attach(api, esp)
    coordinator._apis[PORT] = api
    coordinator.set_enabled_bulbs(set(enabled))
    await coordinator.async_rescan_all_ports()

    entry = SimpleNamespace(
        entry_id="entry",
        data={CONF_SERIAL_PORTS: [PORT]},
        options={CONF_BULBS: enabled, "other": 1},
    )
    hass = SimpleNamespace(
        data={DOMAIN: {entry.entry_id: coordinator}},
        config_entries=FakeConfigEntries(),
    )
    return hass, entry, coordinator, esp


@pytest.mark.parametrize("name", ["Sky1", "Sky2"])
def test_refuses_a_bulb_a_board_reports_connected_or_not(run, name):
    async def scenario():
        hass, entry, _, _ = await setup(["Sky1", "Sky2"])
        return await async_remove_config_entry_device(hass, entry, bulb_device(name)), hass, entry

    allowed, hass, entry = run(scenario())
    assert allowed is False
    assert hass.config_entries.updates == []
    assert entry.options[CONF_BULBS] == ["Sky1", "Sky2"]


def test_removes_an_orphan_that_is_not_in_the_options(run):
    async def scenario():
        hass, entry, _, _ = await setup(["Sky1"])
        return await async_remove_config_entry_device(hass, entry, bulb_device("LivingRoomLamp")), hass

    allowed, hass = run(scenario())
    assert allowed is True
    assert hass.config_entries.updates == []


def test_removed_bulb_is_dropped_from_enabled_bulbs_without_a_reload(run):
    async def scenario():
        hass, entry, coordinator, _ = await setup(["Sky1", "Gone"])
        allowed = await async_remove_config_entry_device(hass, entry, bulb_device("Gone"))
        await _async_options_updated(hass, entry)
        return allowed, hass, entry, coordinator

    allowed, hass, entry, coordinator = run(scenario())
    assert allowed is True
    assert entry.options == {CONF_BULBS: ["Sky1"], "other": 1}
    assert coordinator.get_enabled_bulbs() == {"Sky1"}
    assert hass.config_entries.reloads == 0, "a reload reopens the ports and can reset the boards"


def test_a_real_options_change_still_reloads(run):
    async def scenario():
        hass, entry, _, _ = await setup(["Sky1", "Sky2"])
        entry.options = {CONF_BULBS: ["Sky1"]}
        await _async_options_updated(hass, entry)
        return hass

    assert run(scenario()).config_entries.reloads == 1


def test_uses_the_boards_current_report_not_the_last_poll(run):
    async def scenario():
        hass, entry, _, esp = await setup(["Sky1"])
        esp.bulbs["JustPaired"] = True
        return await async_remove_config_entry_device(hass, entry, bulb_device("JustPaired"))

    assert run(scenario()) is False


def test_refuses_while_a_board_is_not_answering(run):
    async def scenario():
        hass, entry, _, esp = await setup(["Sky1"])
        esp.loop_stalled_until = 100.0
        return await async_remove_config_entry_device(hass, entry, bulb_device("Gone"))

    assert run(scenario()) is False


def test_refuses_when_the_integration_is_not_loaded(run):
    async def scenario():
        hass = SimpleNamespace(data={})
        entry = SimpleNamespace(entry_id="entry", options={})
        return await async_remove_config_entry_device(hass, entry, bulb_device("Gone"))

    assert run(scenario()) is False


def test_identifiers_from_other_integrations_are_ignored(run):
    async def scenario():
        hass, entry, _, _ = await setup(["Sky1"])
        return await async_remove_config_entry_device(
            hass, entry, device(("other_domain", "bulb_Sky1"), (DOMAIN, "bulb_Gone"))
        )

    assert run(scenario()) is True
