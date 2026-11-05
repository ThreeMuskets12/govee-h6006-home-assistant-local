"""Shared fixtures: Home Assistant stubs and a virtual-time event loop.

Home Assistant is stubbed unconditionally so the tests exercise api.py, queue.py,
coordinator.py and __init__.py without the full dependency tree. The virtual clock
lets the tests use the firmware's real worst-case timings (91.5 s) in milliseconds.
"""
from __future__ import annotations

import asyncio
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

    class Platform:
        LIGHT = "light"

    _module("homeassistant")
    _module("homeassistant.config_entries", ConfigEntry=object)
    _module("homeassistant.const", Platform=Platform)
    _module("homeassistant.core", HomeAssistant=object, ServiceCall=object)
    _module("homeassistant.helpers")
    _module("homeassistant.helpers.config_validation", string=str)
    _module("homeassistant.helpers.device_registry", DeviceEntry=object)
    _module(
        "homeassistant.helpers.update_coordinator",
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
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.run_until_complete(asyncio.sleep(0))
            loop.close()

    return _run
