"""Access points as Home Assistant devices ("Online", "Wi-Fi clients") are part of Wi-Fi coverage.

They appear only while PadSpan HA on the same Home Assistant allows Wi-Fi coverage (the same gate as the survey,
wifi_survey.not_licensed). Never licensed: none is created, for any UniFi Utopia user. The licence lapses: the
ones already there stay, unavailable, never removed. It is back: they work again.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from custom_components.unifi_utopia import access_points as apm
from custom_components.unifi_utopia import wifi_survey as ws

AP1, AP2, AP3 = "02:00:00:00:00:a1", "02:00:00:00:00:a2", "02:00:00:00:00:a3"


def ap(mac: str, name: str, clients: int) -> dict:
    return {"mac": mac, "name": name, "model": "U6", "version": "6.6", "state": 1, "last_seen": 1,
            "radio_table": [{"name": "wifi0", "radio": "ng"}],
            "radio_table_stats": [{"name": "wifi0", "num_sta": clients}]}


class Coordinator:
    def __init__(self, devices: list[dict]) -> None:
        self.data = SimpleNamespace(devices=devices, clients=[])
        self.last_update_success = True
        self.listeners: list = []

    def async_add_listener(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)

    def update(self) -> None:
        for cb in list(self.listeners):
            cb()


class Platform:
    """Home Assistant's side of async_add_entities: it keeps each entity and gives it hass."""

    def __init__(self, hass) -> None:
        self.hass = hass
        self.entities: list = []

    def add(self, entities) -> None:
        for e in entities:
            e.hass = self.hass
            self.entities.append(e)


def set_up(hass, coordinator):
    """binary_sensor.py's and sensor.py's async_setup_entry, as far as the access points go."""
    entry = SimpleNamespace(entry_id="entry1", async_on_unload=lambda fn: None)
    online, clients = Platform(hass), Platform(hass)
    apm.setup_ap_entities(hass, entry, coordinator, online.add, apm.APOnlineBinarySensor)
    apm.setup_ap_entities(hass, entry, coordinator, clients.add, apm.APClientsSensor)
    return online, clients


@pytest.mark.parametrize("padspan_here", [True, False])
def test_never_licensed_creates_no_access_point_devices(hass, monkeypatch, request, padspan_here):
    if padspan_here:
        request.getfixturevalue("padspan")              # PadSpan HA there, without Pro or the add-on
    else:
        monkeypatch.delitem(sys.modules, ws.PADSPAN_LICENCE_MODULE, raising=False)
    coordinator = Coordinator([ap(AP1, "Hallway AP", 3), ap(AP2, "Office AP", 5)])
    online, clients = set_up(hass, coordinator)
    for _ in range(3):
        coordinator.update()
    assert online.entities == [] and clients.entities == []


def test_licensed_creates_them(hass, padspan):
    padspan.allowed = True
    online, clients = set_up(hass, Coordinator([ap(AP1, "Hallway AP", 3), ap(AP2, "Office AP", 5)]))
    assert [e._mac for e in online.entities] == [AP1, AP2] and [e._mac for e in clients.entities] == [AP1, AP2]
    first = online.entities[0]
    assert first.available and first.is_on is True and first.extra_state_attributes["wifi_access_point"] is True
    assert first._attr_unique_id == "entry1_ap_0200000000a1_online"
    assert first._attr_device_info["identifiers"] == {("unifi_utopia", f"ap:{AP1}")}
    assert [e.native_value for e in clients.entities] == [3, 5] and all(e.available for e in clients.entities)


def test_lapsed_then_back(hass, padspan):
    padspan.allowed = True
    coordinator = Coordinator([ap(AP1, "Hallway AP", 3)])
    online, clients = set_up(hass, coordinator)
    made = online.entities + clients.entities
    assert len(made) == 2 and all(e.available for e in made)

    padspan.allowed = False                      # lapsed: they stay, unavailable; nothing new is added
    coordinator.data.devices.append(ap(AP2, "Office AP", 5))
    coordinator.update()
    assert online.entities + clients.entities == made
    assert not any(e.available for e in made)

    padspan.allowed = True                       # back: they work again, and the new one is added
    coordinator.update()
    assert all(e.available for e in made)
    assert [e._mac for e in online.entities] == [AP1, AP2] and [e._mac for e in clients.entities] == [AP1, AP2]
    assert all(e.available for e in online.entities + clients.entities)

    hass.data.pop("padspan_ha")                  # PadSpan HA removed: unavailable too, still not removed
    coordinator.data.devices.append(ap(AP3, "Garage AP", 1))
    coordinator.update()
    assert len(online.entities) == 2 and not any(e.available for e in online.entities + clients.entities)
