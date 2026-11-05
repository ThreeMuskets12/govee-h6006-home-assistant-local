"""Shared fixtures: Home Assistant stubs and a virtual-time event loop.

Home Assistant is stubbed unconditionally so the tests exercise api.py, queue.py,
coordinator.py and __init__.py without the full dependency tree. The virtual clock
lets the tests use the firmware's real worst-case timings (91.5 s) in milliseconds.
"""
from __future__ import annotations

import asyncio
import enum
import selectors
import sys
import types
from pathlib import Path
from typing import Generic, TypeVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    if "." in name:
        parent, _, child = name.rpartition(".")
        setattr(sys.modules[parent], child, module)
    return module


def _install_stubs() -> None:
    T = TypeVar("T")

    class DataUpdateCoordinator(Generic[T]):
        def __init__(self, hass, logger, name=None, update_interval=None) -> None:
            self.hass = hass
            self.data = None
            self.last_update_success = True

        async def async_config_entry_first_refresh(self) -> None:
            self.data = await self._async_update_data()

    class CoordinatorEntity(Generic[T]):
        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator
            self.added_to_coordinator = False

        async def async_added_to_hass(self) -> None:
            self.added_to_coordinator = True

    class LightEntity:
        def async_write_ha_state(self) -> None:
            pass

    class RestoredExtraData:
        def __init__(self, json_dict: dict) -> None:
            self.json_dict = json_dict

        def as_dict(self) -> dict:
            return self.json_dict

    class RestoreEntity:
        last_state = None
        last_extra_data = None

        async def async_get_last_state(self):
            return self.last_state

        async def async_get_last_extra_data(self):
            return self.last_extra_data

    class ColorMode(str, enum.Enum):
        COLOR_TEMP = "color_temp"
        RGB = "rgb"

    class Platform:
        LIGHT = "light"

    _module("homeassistant")
    _module("homeassistant.components")
    _module(
        "homeassistant.components.light",
        ATTR_BRIGHTNESS="brightness",
        ATTR_COLOR_MODE="color_mode",
        ATTR_COLOR_TEMP_KELVIN="color_temp_kelvin",
        ATTR_RGB_COLOR="rgb_color",
        ColorMode=ColorMode,
        LightEntity=LightEntity,
    )
    _module("homeassistant.config_entries", ConfigEntry=object)
    _module("homeassistant.const", Platform=Platform, STATE_OFF="off", STATE_ON="on")
    _module(
        "homeassistant.core",
        HomeAssistant=object,
        ServiceCall=object,
        callback=lambda func: func,
    )
    _module("homeassistant.helpers")
    _module("homeassistant.helpers.config_validation", string=str)
    _module("homeassistant.helpers.device_registry", DeviceEntry=object, DeviceInfo=dict)
    _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    _module(
        "homeassistant.helpers.restore_state",
        RestoredExtraData=RestoredExtraData,
        RestoreEntity=RestoreEntity,
    )
    _module(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=CoordinatorEntity,
        DataUpdateCoordinator=DataUpdateCoordinator,
        UpdateFailed=Exception,
    )
    _module("serial_asyncio", open_serial_connection=None)
    try:
        import voluptuous  # noqa: F401
    except ImportError:
        _module("voluptuous", Schema=lambda schema: schema, Required=lambda key: key)


_install_stubs()


class _VirtualSelector(selectors.SelectSelector):
    """Selector that jumps the loop's clock instead of sleeping."""

    def __init__(self) -> None:
        super().__init__()
        self.now = 0.0

    def select(self, timeout=None):
        if timeout is None:
            raise RuntimeError("deadlock: nothing is scheduled on the virtual loop")
        self.now += timeout
        return super().select(0)


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """Event loop whose time only advances when every task is idle."""

    def __init__(self) -> None:
        self._virtual_selector = _VirtualSelector()
        super().__init__(self._virtual_selector)

    def time(self) -> float:
        return self._virtual_selector.now

    def run_in_executor(self, executor, func, *args):
        """Run inline: a real thread would finish outside the virtual clock."""
        future = self.create_future()
        try:
            future.set_result(func(*args))
        except Exception as err:
            future.set_exception(err)
        return future


@pytest.fixture
def run(monkeypatch):
    """Run a coroutine on the virtual clock."""
    from custom_components.esp32_bulb_relay import queue as queue_module

    monkeypatch.setattr(
        queue_module,
        "time",
        types.SimpleNamespace(monotonic=lambda: asyncio.get_running_loop().time()),
    )

    def _run(coro):
        loop = VirtualTimeLoop()
        try:
            return loop.run_until_complete(coro)
        finally:
            # A cancelled recovery's release boots a fake board, which starts new tasks
            loop.run_until_complete(asyncio.sleep(0))
            while tasks := asyncio.all_tasks(loop):
                for task in tasks:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                loop.run_until_complete(asyncio.sleep(0))
            loop.close()

    return _run
