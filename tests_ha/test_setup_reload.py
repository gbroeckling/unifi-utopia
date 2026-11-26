"""The whole integration on a real Home Assistant: setup, an options change (which reloads it), unload.

The tests in tests/ use light stand-ins for Home Assistant and never run __init__.py. These run it on the real
one, through pytest-homeassistant-custom-component, and are skipped where that isn't installed. Run them on
their own (tests/conftest.py's stand-ins would replace the real Home Assistant in the same run):

    pip install pytest-homeassistant-custom-component
    pytest tests_ha

The controller is never contacted: the login and the coordinator's poll are replaced, and the poll answers
with one access point and no clients.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import custom_components  # noqa: E402  (pytest-homeassistant-custom-component's own package; this repo's is added)

if str(ROOT / "custom_components") not in list(custom_components.__path__):
    custom_components.__path__.insert(0, str(ROOT / "custom_components"))

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.helpers import device_registry as dr  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed  # noqa: E402

DOMAIN = "unifi_utopia"
AP = "02:00:00:00:00:a1"
RECEIVER_BT = "02:00:00:00:00:12"     # a PadSpan receiver; its Wi-Fi address is its Bluetooth one minus 2
RECEIVER_WIFI = "02:00:00:00:00:10"


def access_point(mac: str = AP) -> dict:
    return {"mac": mac, "name": "Hallway AP", "model": "U6PRO", "version": "6.6.77", "state": 1, "last_seen": 1,
            "radio_table": [{"name": "wifi0", "radio": "ng", "channel": 6, "ht": "20"}],
            "radio_table_stats": [{"name": "wifi0", "num_sta": 3, "channel": 6}]}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def controller(hass, tmp_path):
    """What the coordinator reads from the controller each poll (change it between polls), with the login,
    the poll and the scheduled downloads replaced. The config folder is a fresh one."""
    hass.config.config_dir = str(tmp_path)
    hass.config.components.update({"http", "zeroconf"})      # the manifest's dependencies, not needed here
    state = {"devices": [access_point()], "clients": []}

    async def _update(self):
        from custom_components.unifi_utopia.coordinator import UniFiUtopiaData
        return UniFiUtopiaData(clients=[{**c, "last_seen": time.time()} for c in state["clients"]],
                               devices=[dict(d) for d in state["devices"]],
                               store=self.store, suspicion={}, events=[], health={}, dpi={}, categories={})

    with ExitStack() as stack:
        for target, new in (
            ("custom_components.unifi_utopia.unifi_api.UniFiApi.login", AsyncMock()),
            ("custom_components.unifi_utopia.coordinator.UniFiUtopiaCoordinator._async_update_data", _update),
            ("custom_components.unifi_utopia.vuln.KevFeed.start", None),
            ("custom_components.unifi_utopia.vuln.KevFeed.async_load", AsyncMock()),
            ("custom_components.unifi_utopia.usage_report.ReportManager.start", None),
        ):
            stack.enter_context(patch(target, new) if new is not None else patch(target))
        yield state


class Model:
    """PadSpan's model: one receiver placed on its plan."""

    def scanner_positions_m(self):
        return {RECEIVER_BT: {"x_m": 2.0, "y_m": 3.0, "floor_id": "ground"}}

    def light_positions_m(self):
        return {}


@pytest.fixture
def padspan(hass, controller, monkeypatch):
    """PadSpan HA on the same Home Assistant, allowing Wi-Fi coverage, with one tracked device and one placed
    receiver that the controller has on Wi-Fi."""
    mod = ModuleType("custom_components.padspan_ha.licence")
    mod.hass_addon_allowed = lambda h, name: name == "wifi"
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    hass.data["padspan_ha"] = {
        "snapshot_cache": (time.monotonic(), {"objects": {"list": [{"key": "phone1", "user_label": "Phone one",
                                                                     "age_s": 1.0}]}}),
        "model": Model()}
    controller["clients"] = [{"mac": RECEIVER_WIFI, "name": "Receiver one", "signal": -61, "radio": "ng",
                              "channel": 6, "ap_mac": AP}]
    return mod


async def minutes(hass, freezer, n: int) -> None:
    """n minutes of Home Assistant's clock, a poll each (the default scan interval)."""
    for _ in range(n):
        freezer.tick(60)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def set_up(hass) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="UniFi Utopia (192.0.2.1)",
                            data={"host": "192.0.2.1", "username": "u", "password": "p"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


def polling(coordinator) -> bool:
    return bool(coordinator._listeners) or coordinator._unsub_refresh is not None


async def test_options_change_reloads_then_unloads(hass, controller, caplog):
    """Every user: Configure → Submit reloads the integration, and it is loaded again, once."""
    entry = await set_up(hass)
    assert entry.state is ConfigEntryState.LOADED
    first = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    assert polling(first)

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"scan_interval": 120})
    assert flow["type"] == "create_entry"
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED, entry.reason
    second = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    assert second is not first and second.update_interval.total_seconds() == 120
    assert not polling(first)                       # the one from before the change has stopped
    assert polling(second)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data[DOMAIN] and not polling(second)
    assert errors(caplog) == []

    assert await hass.config_entries.async_setup(entry.entry_id)      # and it can be set up again
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert errors(caplog) == []


async def test_licensed_survey_saves_while_running_and_raw_log_is_an_option(hass, padspan, hass_storage, freezer,
                                                                          caplog):
    """With Wi-Fi coverage: the readings reach .storage within 15 minutes of polls (0.4.2's save was put off
    on every poll and only written when Home Assistant stopped), and the raw log is written only once the
    option is on."""
    entry = await set_up(hass)
    survey = hass.data[DOMAIN][entry.entry_id]["wifi_survey"]
    raw_dir = hass.config.path("unifi_utopia_wifi_survey")

    await minutes(hass, freezer, 14)
    assert survey.data["fixed"][RECEIVER_WIFI]["vals"]
    assert "unifi_utopia.wifi_survey" not in hass_storage
    await minutes(hass, freezer, 2)
    saved = hass_storage["unifi_utopia.wifi_survey"]["data"]
    assert saved["fixed"][RECEIVER_WIFI]["vals"] and survey.stats["errors"] == 0
    assert not os.path.exists(raw_dir)                              # off by default

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"wifi_raw_log": True})
    assert flow["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    survey = hass.data[DOMAIN][entry.entry_id]["wifi_survey"]
    assert survey.raw_log is True
    await minutes(hass, freezer, 5)                                 # the next round of placed devices
    assert [f for f in os.listdir(raw_dir) if f.endswith(".jsonl")]

    n = len(survey.data["fixed"][RECEIVER_WIFI]["vals"])
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(hass_storage["unifi_utopia.wifi_survey"]["data"]["fixed"][RECEIVER_WIFI]["vals"]) == n
    assert errors(caplog) == []


async def test_access_point_device_is_its_own(hass, padspan):
    """With Wi-Fi coverage each access point is a device of its own: never merged into Home Assistant's UniFi
    Network integration's device for it (whose manufacturer, model and firmware 0.4.2 overwrote)."""
    unifi = MockConfigEntry(domain="unifi", title="UniFi Network")
    unifi.add_to_hass(hass)
    reg = dr.async_get(hass)
    theirs = reg.async_get_or_create(config_entry_id=unifi.entry_id, connections={(dr.CONNECTION_NETWORK_MAC, AP)},
                                     identifiers={("unifi", AP)}, name="Hallway AP", manufacturer="Ubiquiti Networks",
                                     model="U6 Pro", sw_version="6.6.77.15000")
    entry = await set_up(hass)

    theirs = reg.async_get(theirs.id)
    assert theirs.config_entries == {unifi.entry_id} and theirs.identifiers == {("unifi", AP)}
    assert (theirs.manufacturer, theirs.model, theirs.sw_version) == ("Ubiquiti Networks", "U6 Pro", "6.6.77.15000")
    ours = reg.async_get_device(identifiers={(DOMAIN, f"ap:{AP}")})
    assert ours is not None and ours.id != theirs.id and ours.connections == set()
    online = hass.states.get("binary_sensor.hallway_ap_online")
    assert online.state == "on" and "last_seen" not in online.attributes
    assert online.attributes["wifi_access_point"] is True
    assert entry.state is ConfigEntryState.LOADED


async def test_access_point_device_from_042_loses_its_mac_connection(hass, padspan):
    """0.4.2 gave its access point devices the MAC as a connection: taken off at setup, so the UniFi Network
    integration, added later, gets a device of its own instead of merging into this one."""
    entry = MockConfigEntry(domain=DOMAIN, title="UniFi Utopia (192.0.2.1)",
                            data={"host": "192.0.2.1", "username": "u", "password": "p"})
    entry.add_to_hass(hass)
    reg = dr.async_get(hass)
    old = reg.async_get_or_create(config_entry_id=entry.entry_id, connections={(dr.CONNECTION_NETWORK_MAC, AP)},
                                  identifiers={(DOMAIN, f"ap:{AP}")}, name="Hallway AP", manufacturer="Ubiquiti")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    mine = reg.async_get(old.id)
    assert mine.connections == set() and mine.identifiers == {(DOMAIN, f"ap:{AP}")}   # the same device, kept
    assert hass.states.get("binary_sensor.hallway_ap_online").state == "on"

    unifi = MockConfigEntry(domain="unifi", title="UniFi Network")
    unifi.add_to_hass(hass)
    theirs = reg.async_get_or_create(config_entry_id=unifi.entry_id, connections={(dr.CONNECTION_NETWORK_MAC, AP)},
                                     identifiers={("unifi", AP)}, manufacturer="Ubiquiti Networks", model="U6 Pro")
    assert theirs.id != mine.id and reg.async_get(mine.id).config_entries == {entry.entry_id}


async def test_replaced_access_point_device_can_be_deleted(hass, padspan, controller, freezer):
    """Delete is offered for an access point the controller no longer reports, not for one it still does."""
    from custom_components.unifi_utopia import async_remove_config_entry_device

    entry = await set_up(hass)
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, f"ap:{AP}")})
    assert await async_remove_config_entry_device(hass, entry, device) is False

    controller["devices"] = [access_point("02:00:00:00:00:b2")]       # replaced by another one
    await minutes(hass, freezer, 1)
    assert hass.states.get("binary_sensor.hallway_ap_online").state == "unavailable"
    assert await async_remove_config_entry_device(hass, entry, device) is True


async def test_readings_saved_when_home_assistant_stops(hass, padspan, hass_storage, freezer):
    """Home Assistant stops without unloading integrations: what came in since the last save is written then."""
    entry = await set_up(hass)
    await minutes(hass, freezer, 6)
    assert "unifi_utopia.wifi_survey" not in hass_storage
    n = len(hass.data[DOMAIN][entry.entry_id]["wifi_survey"].data["fixed"][RECEIVER_WIFI]["vals"])
    assert n >= 1
    await hass.async_stop()
    assert len(hass_storage["unifi_utopia.wifi_survey"]["data"]["fixed"][RECEIVER_WIFI]["vals"]) == n
