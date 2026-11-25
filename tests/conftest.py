"""Test setup: light stand-ins for the parts of Home Assistant the Wi-Fi survey uses.

No Home Assistant install is needed. The `homeassistant` modules wifi_survey.py and access_points.py
import are stand-ins registered before any test imports them, and the integration's package is
registered bare, so its __init__.py (the whole integration's setup) is not run: wifi_survey.py and
its own imports are what is tested.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _module(name: str, **attrs) -> ModuleType:
    mod = ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    parent, _, child = name.rpartition(".")
    if parent in sys.modules:
        setattr(sys.modules[parent], child, mod)
    return mod


def _callback(fn):
    return fn


def _websocket_command(schema):
    def deco(fn):
        fn.ws_type = next(v for k, v in schema.items() if str(k) == "type")
        return fn
    return deco


def _register_command(hass, handler) -> None:
    hass.ws_commands[handler.ws_type] = handler


class _Registry:
    def async_get(self, *_a, **_kw):
        return None

    def async_get_device(self, *_a, **_kw):
        return None


class Store:
    """Home Assistant's Store, in memory."""

    def __init__(self, hass, version, key) -> None:
        self.key = key
        self.saved = None

    async def async_load(self):
        return None

    async def async_save(self, data) -> None:
        self.saved = data

    def async_delay_save(self, data_func, delay=0) -> None:
        self.saved = data_func()


class _Entity:
    def __init__(self, *args, **kwargs) -> None:
        pass


class CoordinatorEntity(_Entity):
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator


_module("homeassistant")
_module("homeassistant.core", HomeAssistant=object, callback=_callback)
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.components")
_module("homeassistant.components.websocket_api", websocket_command=_websocket_command,
        require_admin=lambda fn: fn, async_response=lambda fn: fn, async_register_command=_register_command)
_module("homeassistant.components.binary_sensor", BinarySensorEntity=_Entity,
        BinarySensorDeviceClass=SimpleNamespace(CONNECTIVITY="connectivity"))
_module("homeassistant.components.sensor", SensorEntity=_Entity, SensorStateClass=SimpleNamespace(MEASUREMENT="measurement"))
_module("homeassistant.helpers")
_module("homeassistant.helpers.device_registry", async_get=lambda hass: _Registry(), CONNECTION_NETWORK_MAC="mac",
        DeviceInfo=dict)
_module("homeassistant.helpers.entity_registry", async_get=lambda hass: _Registry(),
        async_entries_for_device=lambda reg, device_id: [])
_module("homeassistant.helpers.storage", Store=Store)
_module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
_module("homeassistant.helpers.update_coordinator", CoordinatorEntity=CoordinatorEntity)

# The integration's package, bare: its submodules import from here as they would in Home Assistant.
_module("custom_components").__path__ = [str(ROOT / "custom_components")]
_module("custom_components.unifi_utopia").__path__ = [str(ROOT / "custom_components" / "unifi_utopia")]


class FakeHass:
    """What the survey asks of Home Assistant: its data, config folder, states and task scheduling."""

    def __init__(self, config_dir: Path) -> None:
        self.data: dict = {}
        self.config = SimpleNamespace(path=lambda *p: str(config_dir.joinpath(*p)))
        self.states = SimpleNamespace(get=lambda entity_id: None)
        self.tasks: list[asyncio.Task] = []
        self.ws_commands: dict = {}

    def async_create_task(self, coro, name=None):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)

    async def async_block_till_done(self) -> None:
        while self.tasks:
            await self.tasks.pop(0)


@pytest.fixture
def hass(tmp_path: Path) -> FakeHass:
    return FakeHass(tmp_path)
