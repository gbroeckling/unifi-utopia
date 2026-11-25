"""The Wi-Fi survey runs only while PadSpan HA on the same Home Assistant allows Wi-Fi coverage.

PadSpan says so through licence.hass_addon_allowed(hass, "wifi") (PadSpan HA 0.38.300 and later): PadSpan Pro,
or its Wi-Fi coverage add-on. Not allowed, PadSpan absent, an older PadSpan without the call, or the call
failing: nothing is collected, and wifi_heatmap answers a non-error "not licensed" shape whose version is
below 1, which PadSpan's panel (_wifiProbe) and views/wifi.js (probe) read as "no survey here".
"""
from __future__ import annotations

import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from custom_components.unifi_utopia import wifi_survey as ws

LICENCE = ws.PADSPAN_LICENCE_MODULE
AP = "02:00:00:00:00:a1"
RECEIVER_BT = "02:00:00:00:00:12"     # a PadSpan receiver; its Wi-Fi address is its Bluetooth one minus 2
RECEIVER_WIFI = "02:00:00:00:00:10"
BEFORE_KEYS = {"version", "generated_at", "cell_m", "floors", "bands", "aps", "findings", "cells", "rooms", "fixed",
               "status"}


class Coordinator:
    def __init__(self) -> None:
        self.data = SimpleNamespace(
            clients=[{"mac": RECEIVER_WIFI, "name": "Receiver one", "signal": -61, "radio": "ng", "channel": 6,
                      "ap_mac": AP, "last_seen": time.time()}],
            devices=[{"mac": AP, "name": "Hallway AP", "model": "U6", "state": 1, "last_seen": time.time(),
                      "radio_table": [{"name": "wifi0", "radio": "ng", "channel": 6, "ht": "20"}]}])
        self.listeners: list = []

    def async_add_listener(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)


class Model:
    def scanner_positions_m(self):
        return {RECEIVER_BT: {"x_m": 2.0, "y_m": 3.0, "floor_id": "ground"}}

    def light_positions_m(self):
        return {}


class Connection:
    def __init__(self, admin: bool = True) -> None:
        self.user = SimpleNamespace(is_admin=admin)
        self.results: list = []
        self.errors: list = []

    def send_result(self, msg_id, result) -> None:
        self.results.append(result)

    def send_error(self, msg_id, code, message) -> None:
        self.errors.append((code, message))


class PadSpan:
    """PadSpan HA as loaded on the same Home Assistant: its data, and its licence module in sys.modules."""

    def __init__(self, hass, monkeypatch) -> None:
        self.allowed = False
        self.asked: list[str] = []
        hass.data["padspan_ha"] = {
            "snapshot_cache": (time.monotonic(), {"objects": {"list": [{"key": "phone1", "user_label": "Phone one",
                                                                         "age_s": 1.0}]}}),
            "model": Model()}
        mod = ModuleType(LICENCE)

        def hass_addon_allowed(h, name):
            self.asked.append(name)
            return self.allowed and name == "wifi"

        mod.hass_addon_allowed = hass_addon_allowed
        self.module = mod
        monkeypatch.setitem(sys.modules, LICENCE, mod)


@pytest.fixture
async def survey(hass):
    s = ws.WifiSurvey(hass, Coordinator())
    await s.async_start()
    hass.data["unifi_utopia"] = {"entry1": {"wifi_survey": s}}
    ws.register(hass)
    return s


@pytest.fixture
def padspan(hass, monkeypatch):
    return PadSpan(hass, monkeypatch)


async def poll(hass, survey) -> None:
    """One coordinator update, and whatever it started."""
    for cb in list(survey.coordinator.listeners):
        cb()
    await hass.async_block_till_done()


async def ask(hass, kind: str, admin: bool = True, prefix: str = "unifi_utopia", **fields) -> Connection:
    conn = Connection(admin)
    out = hass.ws_commands[f"{prefix}/{kind}"](hass, conn, {"id": 1, "type": f"{prefix}/{kind}", **fields})
    if out is not None:
        await out
    return conn


def heatmap_shown(answer: dict) -> bool:
    """PadSpan's own rule (panel.js _wifiProbe, views/wifi.js probe): r.version >= 1 means the survey is there."""
    return bool(answer) and isinstance(answer.get("version"), (int, float)) and answer["version"] >= 1


async def test_unlicensed_collects_nothing_and_answers_not_licensed(hass, survey, padspan):
    await poll(hass, survey)
    assert padspan.asked == ["wifi"]
    assert survey.stats["polls"] == 0 and survey.data["fixed"] == {} and not hass.tasks

    for prefix in ("unifi_utopia", "unifiblocker"):       # the old name's commands are gated the same way
        conn = await ask(hass, "wifi_heatmap", prefix=prefix)
        assert conn.errors == []
        assert conn.results == [{"version": 0, "licensed": False, "reason": "not_allowed",
                                 "message": "Wi-Fi coverage needs PadSpan Pro or PadSpan's Wi-Fi coverage add-on."}]
        assert not heatmap_shown(conn.results[0])

    conn = await ask(hass, "wifi_links")
    assert conn.errors == [] and conn.results[0]["licensed"] is False and "wifi" not in conn.results[0]

    conn = await ask(hass, "wifi_link_set", mac=RECEIVER_WIFI, padspan_id="phone1")
    assert conn.results == [] and conn.errors[0][0] == "not_licensed"
    conn = await ask(hass, "wifi_ap_set", mac=AP, floor="ground", x=1.0, y=1.0)
    assert conn.results == [] and conn.errors[0][0] == "not_licensed"
    assert survey.data["links"] == {} and survey.data["ap_pos"] == {}


async def test_licensed_collects_and_answers_as_before(hass, survey, padspan):
    padspan.allowed = True
    await poll(hass, survey)
    assert survey.stats["polls"] == 1
    assert survey.data["fixed"][RECEIVER_WIFI]["vals"][-1][1] == -61.0

    conn = await ask(hass, "wifi_heatmap")
    assert conn.errors == []
    out = conn.results[0]
    assert set(out) == BEFORE_KEYS and out["version"] == ws.API_VERSION and heatmap_shown(out)
    assert [(f["name"], f["median"], f["floor"]) for f in out["fixed"]] == [("Receiver one", -61.0, "ground")]
    assert out["aps"][0]["mac"] == AP

    guest = await ask(hass, "wifi_heatmap", admin=False)          # as before: no status for a non-administrator
    assert "status" not in guest.results[0] and heatmap_shown(guest.results[0])

    conn = await ask(hass, "wifi_links")
    assert conn.errors == [] and {"wifi", "padspan", "status"} <= set(conn.results[0])
    conn = await ask(hass, "wifi_ap_set", mac=AP, floor="ground", x=1.0, y=1.5)
    assert conn.errors == [] and conn.results[0]["ok"] is True
    assert survey.data["ap_pos"][AP] == {"floor": "ground", "x": 1.0, "y": 1.5}


@pytest.mark.parametrize("case, reason", [
    ("padspan_absent", "padspan_missing"),
    ("module_not_loaded", "padspan_too_old"),
    ("older_padspan", "padspan_too_old"),
    ("call_fails", "check_failed"),
    ("not_a_yes", "not_allowed"),
])
async def test_padspan_missing_or_old_is_not_licensed(hass, survey, padspan, monkeypatch, case, reason):
    padspan.allowed = True
    imports: list[str] = []

    class Recorder:       # any attempt to import PadSpan from here, whether or not it would succeed
        @staticmethod
        def find_spec(name, path=None, target=None):
            if name.startswith("custom_components.padspan_ha"):
                imports.append(name)
            return None

    monkeypatch.setattr(sys, "meta_path", [Recorder(), *sys.meta_path])
    if case == "padspan_absent":
        hass.data.pop("padspan_ha")
    elif case == "module_not_loaded":
        monkeypatch.delitem(sys.modules, LICENCE)
    elif case == "older_padspan":
        del padspan.module.hass_addon_allowed
    elif case == "call_fails":
        def boom(h, name):
            raise RuntimeError("settings not loaded")
        padspan.module.hass_addon_allowed = boom
    elif case == "not_a_yes":
        padspan.module.hass_addon_allowed = lambda h, name: "yes"

    assert ws.not_licensed(hass) == reason
    await poll(hass, survey)
    assert survey.stats["polls"] == 0 and survey.data["fixed"] == {}
    conn = await ask(hass, "wifi_heatmap")
    assert conn.errors == [] and conn.results[0]["reason"] == reason and not heatmap_shown(conn.results[0])
    assert imports == []                  # asked of a loaded PadSpan only: never imported from here


async def test_licence_flips_at_runtime(hass, survey, padspan, monkeypatch):
    monkeypatch.setattr(ws, "FIXED_EVERY_S", 0)       # a placed device's reading on every poll
    readings = lambda: len((survey.data["fixed"].get(RECEIVER_WIFI) or {}).get("vals") or [])  # noqa: E731

    await poll(hass, survey)
    assert readings() == 0

    padspan.allowed = True                    # bought: counts from the next poll and the next request
    await poll(hass, survey)
    assert readings() == 1
    assert heatmap_shown((await ask(hass, "wifi_heatmap")).results[0])

    padspan.allowed = False                   # lapsed
    await poll(hass, survey)
    await poll(hass, survey)
    assert readings() == 1
    lapsed = (await ask(hass, "wifi_heatmap")).results[0]
    assert lapsed["licensed"] is False and not heatmap_shown(lapsed)

    padspan.allowed = True                    # back: what was collected is still there, and collecting resumes
    back = (await ask(hass, "wifi_heatmap")).results[0]
    assert heatmap_shown(back) and len(back["fixed"]) == 1
    await poll(hass, survey)
    assert readings() == 2
    assert survey.stats["polls"] == 2
