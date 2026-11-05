"""Mains-powered bulbs keep their state across restarts, so the entities must too."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from custom_components.esp32_bulb_relay.api import ESP32BulbRelayApiError
from custom_components.esp32_bulb_relay.light import ColorMode, ESP32BulbRelayLight


def make_light(last_state=None, last_extra_data=None, **coordinator_attrs) -> ESP32BulbRelayLight:
    coordinator = SimpleNamespace(data=None, last_update_success=True, **coordinator_attrs)
    light = ESP32BulbRelayLight(coordinator, "Sky1")
    light.last_state = last_state
    light.last_extra_data = last_extra_data
    return light


def state(value: str, **attributes) -> SimpleNamespace:
    return SimpleNamespace(state=value, attributes=attributes)


def restored(run, last_state) -> ESP32BulbRelayLight:
    light = make_light(last_state)
    run(light.async_added_to_hass())
    return light


def test_restores_an_rgb_light(run):
    light = restored(
        run, state("on", brightness=128, rgb_color=[10, 20, 30], color_mode="rgb")
    )
    assert light._attr_is_on is True
    assert light._attr_brightness == 128
    assert light._attr_rgb_color == (10, 20, 30)
    assert light._attr_color_mode is ColorMode.RGB
    assert light.added_to_coordinator, "the coordinator listener must still be set up"


def test_restores_a_white_light(run):
    light = restored(
        run, state("on", brightness=64, color_temp_kelvin=2700, color_mode="color_temp")
    )
    assert light._attr_is_on is True
    assert light._attr_brightness == 64
    assert light._attr_color_temp_kelvin == 2700
    assert light._attr_color_mode is ColorMode.COLOR_TEMP


def test_off_state_keeps_the_defaults_for_the_attributes_ha_cleared(run):
    light = restored(
        run,
        state("off", brightness=None, rgb_color=None, color_temp_kelvin=None, color_mode=None),
    )
    assert light._attr_is_on is False
    assert light._attr_brightness == 255
    assert light._attr_rgb_color == (255, 255, 255)
    assert light._attr_color_temp_kelvin == 4000
    assert light._attr_color_mode is ColorMode.COLOR_TEMP


@pytest.mark.parametrize("last_state", [None, state("unavailable"), state("unknown")])
def test_nothing_to_restore_keeps_the_defaults(run, last_state):
    light = restored(run, last_state)
    assert light._attr_is_on is False
    assert light._attr_brightness == 255


def test_unsupported_color_mode_is_ignored(run):
    light = restored(run, state("on", color_mode="hs"))
    assert light._attr_is_on is True
    assert light._attr_color_mode is ColorMode.COLOR_TEMP


def stored(light: ESP32BulbRelayLight) -> SimpleNamespace:
    """What a restart gives back: extra data that has been through JSON."""
    data = json.loads(json.dumps(light.extra_restore_state_data.as_dict()))
    return SimpleNamespace(as_dict=lambda: data)


def lit_rgb_light(run) -> ESP32BulbRelayLight:
    async def scenario():
        async def send(*args):
            return {"success": True}

        light = make_light(async_send_command=send)
        await light.async_turn_on(brightness=128, rgb_color=(10, 20, 30))
        return light

    return run(scenario())


@pytest.mark.parametrize("state_at_shutdown", ["unavailable", "unknown"])
def test_extra_data_restores_a_light_that_was_unavailable_at_shutdown(run, state_at_shutdown):
    light = make_light(state(state_at_shutdown), stored(lit_rgb_light(run)))
    run(light.async_added_to_hass())
    assert light._attr_is_on is True
    assert light._attr_brightness == 128
    assert light._attr_rgb_color == (10, 20, 30)
    assert light._attr_color_mode is ColorMode.RGB


def test_extra_data_survives_a_reload_without_going_through_json(run):
    extra = lit_rgb_light(run).extra_restore_state_data
    light = make_light(state("unavailable"), extra)
    run(light.async_added_to_hass())
    assert light._attr_rgb_color == (10, 20, 30)


def test_extra_data_survives_restarts_while_the_light_stays_unavailable(run):
    light = lit_rgb_light(run)
    for _ in range(3):
        extra = stored(light)
        light = make_light(state("unavailable"), extra)
        run(light.async_added_to_hass())
    assert light._attr_is_on is True
    assert light._attr_brightness == 128


def test_extra_data_keeps_the_color_of_a_light_that_was_off(run):
    """HA clears these attributes on an off light, which last_state can't bring back."""

    async def scenario():
        async def send(*args):
            return {"success": True}

        lit = make_light(async_send_command=send)
        await lit.async_turn_on(brightness=128, rgb_color=(10, 20, 30))
        await lit.async_turn_off()
        off_state = state("off", brightness=None, rgb_color=None, color_mode=None)
        light = make_light(off_state, stored(lit))
        await light.async_added_to_hass()
        return light

    light = run(scenario())
    assert light._attr_is_on is False
    assert light._attr_brightness == 128
    assert light._attr_rgb_color == (10, 20, 30)
    assert light._attr_color_mode is ColorMode.RGB


@pytest.mark.parametrize("data", [{}, {"is_on": None}, {"brightness": 10}])
def test_extra_data_without_a_state_falls_back_to_last_state(run, data):
    extra = SimpleNamespace(as_dict=lambda: data)
    light = make_light(state("on", brightness=64), extra)
    run(light.async_added_to_hass())
    assert light._attr_is_on is True
    assert light._attr_brightness == 64


def test_commands_stay_optimistic_after_a_restore(run):
    async def scenario():
        async def send(*args):
            return {"success": True}

        light = make_light(state("on", brightness=128), async_send_command=send)
        await light.async_added_to_hass()
        await light.async_turn_off()
        return light

    assert run(scenario())._attr_is_on is False


def test_failed_command_leaves_the_restored_state_alone(run):
    async def scenario():
        async def send(*args):
            raise ESP32BulbRelayApiError("no answer")

        light = make_light(state("on", brightness=128), async_send_command=send)
        await light.async_added_to_hass()
        await light.async_turn_off()
        return light

    assert run(scenario())._attr_is_on is True
