"""Each UniFi access point as a Home Assistant device: "Online" and "Wi-Fi clients".

So an access point can be placed in PadSpan HA's Mapping like any other device (its "Online" sensor
carries wifi_access_point: true, which PadSpan's Atlas reads as a Wi-Fi access point), and so Home
Assistant can say when one goes down. Read from the controller's device list; nothing is written to it.

Part of Wi-Fi coverage: only while PadSpan HA on this Home Assistant allows it
(wifi_survey.not_licensed, asked on every poll). Never licensed, none is added. When the licence lapses, the
ones already there stay and show unavailable (never removed); when it is back they work again.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .wifi_survey import not_licensed


def ap_identifier(mac: str) -> tuple[str, str]:
    """The device registry identifier of an access point's device."""
    return (DOMAIN, f"ap:{mac.lower()}")


def access_points(coordinator) -> dict[str, dict[str, Any]]:
    """{mac: the controller's record} for every device with radios (access points, consoles with Wi-Fi)."""
    out = {}
    for d in getattr(coordinator.data, "devices", None) or []:
        if isinstance(d, dict) and d.get("radio_table") and d.get("mac"):
            out[str(d["mac"]).lower()] = d
    return out


def drop_old_connections(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """0.4.2 gave each access point's device its MAC as a connection, so Home Assistant's UniFi Network
    integration, added later, would merge its device into it. Taken off the devices only this integration has
    (nothing else about them changes); one 0.4.2 already merged with another integration's device stays as it is."""
    reg = dr.async_get(hass)
    for dev in dr.async_entries_for_config_entry(reg, entry.entry_id):
        if (dev.connections and set(dev.config_entries) == {entry.entry_id}
                and any(i[0] == DOMAIN and i[1].startswith("ap:") for i in dev.identifiers)):
            reg.async_update_device(dev.id, new_connections=set())


class _APEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, mac: str, key: str) -> None:
        super().__init__(coordinator)
        self._mac = mac
        self._attr_unique_id = f"{entry.entry_id}_ap_{mac.replace(':', '')}_{key}"
        d = access_points(coordinator).get(mac) or {}
        # Identifiers only: the access point's MAC as a connection would merge this device with Home Assistant's
        # own UniFi Network integration's device for it, and overwrite that one's manufacturer, model and firmware.
        self._attr_device_info = DeviceInfo(
            identifiers={ap_identifier(mac)},
            name=str(d.get("name") or d.get("model") or mac),
            manufacturer="Ubiquiti",
            model=d.get("model"),
            sw_version=d.get("version"),
        )

    @property
    def _ap(self) -> dict[str, Any] | None:
        return access_points(self.coordinator).get(self._mac)

    @property
    def available(self) -> bool:
        return super().available and self._ap is not None and not_licensed(self.hass) is None


class APOnlineBinarySensor(_APEntity, BinarySensorEntity):
    """On while the controller has the access point connected."""

    _attr_name = "Online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, entry: ConfigEntry, mac: str) -> None:
        super().__init__(coordinator, entry, mac, "online")

    @property
    def is_on(self) -> bool | None:
        ap = self._ap
        return None if ap is None else ap.get("state") == 1

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ap = self._ap or {}
        # The marker PadSpan HA's Atlas reads: place this one in Mapping as a Wi-Fi access point. Nothing that
        # changes on every poll (the controller's last_seen did), or the recorder keeps a new row each time.
        return {"wifi_access_point": True, "model": ap.get("model")}


class APClientsSensor(_APEntity, SensorEntity):
    """How many Wi-Fi clients the access point has, over all its radios."""

    _attr_name = "Wi-Fi clients"
    _attr_icon = "mdi:access-point-network"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry: ConfigEntry, mac: str) -> None:
        super().__init__(coordinator, entry, mac, "clients")

    @property
    def native_value(self) -> int | None:
        ap = self._ap
        if ap is None:
            return None
        stats = [s for s in ap.get("radio_table_stats") or [] if isinstance(s, dict)]
        if stats:
            return sum(int(s.get("num_sta") or 0) for s in stats)
        n = ap.get("num_sta")
        return int(n) if isinstance(n, (int, float)) else None


def setup_ap_entities(hass: HomeAssistant, entry: ConfigEntry, coordinator, async_add_entities: AddEntitiesCallback,
                      make) -> None:
    """Add one entity per access point now, and for each new one the controller reports later, while PadSpan
    allows Wi-Fi coverage (asked on every update, so they are added when a licence arrives)."""
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        if not_licensed(hass):
            return
        new = [m for m in access_points(coordinator) if m not in known]
        if new:
            known.update(new)
            async_add_entities([make(coordinator, entry, m) for m in new])

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))
