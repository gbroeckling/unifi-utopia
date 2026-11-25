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
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import custom_components  # noqa: E402  (pytest-homeassistant-custom-component's own package; this repo's is added)

if str(ROOT / "custom_components") not in list(custom_components.__path__):
    custom_components.__path__.insert(0, str(ROOT / "custom_components"))

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

DOMAIN = "unifi_utopia"
AP = "02:00:00:00:00:a1"


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
        return UniFiUtopiaData(clients=[dict(c) for c in state["clients"]], devices=[dict(d) for d in state["devices"]],
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
