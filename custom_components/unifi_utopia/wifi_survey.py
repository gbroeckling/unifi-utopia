"""Wi-Fi coverage survey: where in the house each Wi-Fi signal was measured.

UniFi says how strongly each access point hears each wireless client. PadSpan HA says where its tracked
devices are. Joined, they give a coverage map from everyday life, with no walk-round survey:

- Phones (any PadSpan-tracked device linked to a Wi-Fi client) give a reading only while they have
  stayed put for about a minute, so a reading belongs to one spot and not to a walk.
- Fixed devices PadSpan has placed on its plans (its Bluetooth receivers, its positioned lights and
  devices) give a reading at a known spot every few minutes: the 2.4 GHz picture of the smart home.

Readings are kept per floor, band and access point in 1.5 m cells and per room, for 30 days, with
the newest 240 per key. PadSpan's panel asks `unifi_utopia/wifi_heatmap` and draws them; when this
integration is absent the command doesn't exist and PadSpan shows nothing.

Everything stays in Home Assistant. Nothing here writes to UniFi or to PadSpan.

Test build (2026-11-22): PadSpan positions are read from its in-memory snapshot (no request, no
cost); a documented PadSpan call replaces that if this goes further.

The robot vacuum (2026-11-24, the owner's "Yes, build it"): PadSpan's room location runs stand the robot at
spots about 1.5 m apart and keep the robot's own Wi-Fi at each (what the robot hears, from Valetudo). PadSpan
hands them out through vacuum_run.robot_wifi_snapshot(hass), with the robot's IP. Here the robot is found
among UniFi's clients by that IP each poll, and each of its readings is joined with what the access point
heard from it nearest in time. The answer carries them apart from everything else ("robot"): the robot
reads at about 0.1 m off the floor and almost certainly on 2.4 GHz only, and its own dBm and the access
point's side are different scales, so neither is mixed into the cells, rooms, devices or findings.

Readings taken while the robot cleans (PadSpan from 2026-11-25, the owner's choice on its card, off by
default): PadSpan marks each reading "kind" "stop" (a room location run's stand) or "moving" (on the move,
about every 5 s), with a "weight" (1.0 a stop, 0.1 a moving one); a reading without them is a stop. Both are
passed through as they are. A moving reading joins UniFi's side only with a sample within ROBOT_MOVING_JOIN_S
of its own time: the robot drives on, and UniFi is polled about once a minute, so most have no access point
side, and that is as it should be.

Unlocked by PadSpan's licence (0.4.2, the owner's decision): Wi-Fi coverage is PadSpan Pro's, or PadSpan's Wi-Fi
coverage add-on's. The survey collects, and its commands answer with readings, only while PadSpan HA on this
Home Assistant says so (licence.hass_addon_allowed(hass, "wifi"), PadSpan HA 0.38.300 and later). PadSpan
absent, an older one without the call, or the call failing: not licensed. Asked on every poll and every
request (a read of PadSpan's settings in memory, nothing fetched), so a licence that arrives or lapses counts
from the next one. Not licensed, wifi_heatmap answers {"version": 0, "licensed": false, ...}: PadSpan reads
version 1 or more as "the survey is there", so its Wi-Fi tab stays away. What was collected stays stored.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import os
import re
import sys
import time
from bisect import bisect_left, bisect_right
from collections import deque
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

API_VERSION = 1
STORE_KEY = "unifi_utopia.wifi_survey"
CELL_M = 1.5               # grid cell size
KEEP_S = 30 * 86400        # readings older than this are dropped
KEEP_N = 240               # newest readings kept per cell / room / device
STILL_MIN_S = 50           # a phone counts as still when it was within STILL_M ...
STILL_MAX_S = 200          # ... of where it was this long ago (one or two polls back)
STILL_M = 1.5
FRESH_POS_S = 45           # PadSpan must have heard the device this recently
FRESH_WIFI_S = 150         # UniFi must have seen the client this recently
FIXED_EVERY_S = 290        # fixed devices: one reading about every five minutes
SAVE_EVERY_S = 15 * 60     # the readings are saved this often (and when it stops)
RAW_KEEP_DAYS = 7          # raw log files (an option, off by default) kept this many days
HEATMAP_KEPT = 16          # answers kept until the next poll, one per filter asked for
BANDS = {"ng": "2.4", "na": "5", "6e": "6"}
PADSPAN = "padspan_ha"
POS_KEYS = ("x_m", "y_m", "floor_id", "room", "_stale")   # what PadSpan's presence coordinator adds
# A phone PadSpan has stopped hearing is still where it last sat when its Wi-Fi hasn't changed:
CARRY_S = 1800             # for this long after its last still reading at most,
CARRY_DB = 4               # with the same access point and band, and its signal within this of then
# Findings
WEAK_DBM = -80             # below this Wi-Fi drops out
RECEIVER_WEAK_DBM = -75    # a PadSpan receiver this weak loses reports
TX_EXCESS_DB = 15          # heard this much worse than it hears, beyond its access point's usual: weak transmitter
TX_BASELINE_DB = 5         # the usual gap when an access point has too few other devices to tell
DEV_POOR_DBM = -70         # the device hears its access point this badly or worse
WLED_TIMEOUT_S = 4
# What the access points themselves show
AP_DOWN_S = 600            # an access point not seen this long is down
FOREIGN_DBM = -60          # another network this strong on one of your channels is worth naming
FOREIGN_FRESH_S = 7200     # ... when heard within this long (stale entries left out)
LOUD_DB = 6                # louder than your other access points' median by this much on 2.4 GHz
HOG_DBM = -80              # airtime: a client this weak or weaker,
HOG_ATTEMPTS = 100000      # ... with this much sent to it,
HOG_RATE_MBPS = 12         # ... at this rate or slower,
HOG_RETRY_PCT = 40         # ... or with this share retried
ISP_NAME = re.compile(r"^(TELUS|Shaw|Rogers|Bell|Fizz|Videotron|Cogeco|Xfinity|Spectrum|ATT|Verizon|Fios|CenturyLink|"
                      r"BT-|BTHub|Sky|Virgin|Vodafone|Telstra|Optus|Ziggo|KPN|FRITZ!Box|Livebox|SFR|Freebox|"
                      r"Telekom|o2-)", re.I)         # internet providers' modem defaults (not router brands)
NEAR_M = 3.0               # neighbours for "on a far access point"
GAP_DB = 15                # ... that hear another access point at least this much better
FAR_MIN_DBM = -70          # ... and only when the device itself is this weak or weaker (fair or worse)
DROP_SHARE = 0.6           # seen in fewer than this share of the last day's checks: dropping off
RECENT_S = 86400           # a device's "now" is its last day (or since it last changed access point)
PHONE_DAYS = 7             # phone time on weak Wi-Fi, over this many days
PHONE_MIN_S = 30 * 60      # ... from this much time in one room
# The robot vacuum's readings (PadSpan's room location runs)
PADSPAN_RUN_MODULE = "custom_components.padspan_ha.vacuum_run"   # robot_wifi_snapshot(hass), version 1
ROBOT_JOIN_S = 45          # a robot reading takes the access point's sample nearest in time, this close or none
ROBOT_STAND_MID_S = 10     # ... nearest the middle of the robot's stand: PadSpan reads at arrival, it stands ~20 s
ROBOT_STOP_SLACK_S = 3     # a sample this close outside the stop's own start/end still counts (UniFi's report lags)
ROBOT_MOVING_JOIN_S = 5    # a reading taken on the move: the access point's sample this close in time, or none
ROBOT_RING_N = 1440        # the robot's access point samples kept in memory: a day at the 60 s poll
ROBOT_FREQ = {"2.4ghz": "2.4", "5ghz": "5"}   # Valetudo's frequency as a band
# PadSpan's licence: Wi-Fi coverage is its add-on "wifi" (PadSpan Pro includes it)
PADSPAN_LICENCE_MODULE = "custom_components.padspan_ha.licence"   # hass_addon_allowed(hass, name), 0.38.300+
PADSPAN_ADDON = "wifi"
NOT_LICENSED_WORDS = {
    "padspan_missing": "Wi-Fi coverage is shown in PadSpan HA, which is not running on this Home Assistant.",
    "padspan_too_old": "Wi-Fi coverage needs PadSpan HA 0.38.300 or later.",
    "not_allowed": "Wi-Fi coverage needs PadSpan Pro or PadSpan's Wi-Fi coverage add-on.",
    "check_failed": "PadSpan HA could not say whether Wi-Fi coverage is allowed.",
}


def not_licensed(hass: HomeAssistant) -> str | None:
    """None while PadSpan HA on this Home Assistant allows Wi-Fi coverage, else why not (NOT_LICENSED_WORDS).
    Asked only of a PadSpan that is loaded (its licence module is imported with it); never imported here."""
    if PADSPAN not in hass.data:
        return "padspan_missing"
    fn = getattr(sys.modules.get(PADSPAN_LICENCE_MODULE), "hass_addon_allowed", None)
    if not callable(fn):
        return "padspan_too_old"
    try:
        ok = fn(hass, PADSPAN_ADDON)
    except Exception:  # noqa: BLE001 - PadSpan's trouble: not licensed this time, asked again next time
        _LOGGER.debug("Wi-Fi survey: PadSpan's licence not readable", exc_info=True)
        return "check_failed"
    return None if ok is True else "not_allowed"


def not_licensed_answer(reason: str) -> dict[str, Any]:
    """What the commands answer while not licensed: no readings, and why."""
    return {"version": 0, "licensed": False, "reason": reason, "message": NOT_LICENSED_WORDS[reason]}


def norm_name(s: Any) -> str:
    """'Pixel-8-Pro' and 'Pixel 8 Pro' are the same name."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def mac_plus(mac: str, n: int) -> str:
    v = (int(mac.replace(":", "").replace("-", ""), 16) + n) & 0xFFFFFFFFFFFF
    return ":".join(f"{(v >> (8 * i)) & 0xFF:02x}" for i in reversed(range(6)))


def _q(vals: list[float], p: float) -> float:
    s = sorted(vals)
    if not s:
        return float("nan")
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarise(vals: list[list[float]]) -> dict[str, Any]:
    """[[t, dBm], ...] -> median, quartiles, count, time span."""
    v = [x[1] for x in vals]
    return {"median": round(_q(v, 0.5), 1), "p25": round(_q(v, 0.25), 1), "p75": round(_q(v, 0.75), 1),
            "n": len(v), "first": int(min(x[0] for x in vals)), "last": int(max(x[0] for x in vals))}


class WifiSurvey:
    """One per config entry. Fed by the coordinator; answers the websocket commands."""

    def __init__(self, hass: HomeAssistant, coordinator, raw_log: bool = False) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.store = Store(hass, 1, STORE_KEY)
        self.raw_log = raw_log                   # the option "keep a raw log of each reading"
        self.data: dict[str, Any] = {"links": {}, "excluded": [], "cells": {}, "rooms": {}, "fixed": {},
                                     "aps": {}, "ap_pos": {}, "phone_time": {}, "rounds": []}
        self._track: dict[str, deque] = {}       # wifi mac -> recent (t, x, y, floor, room)
        self._anchor: dict[str, dict] = {}       # wifi mac -> its last still reading (where it sat)
        self._last_poll: dict[str, float] = {}   # wifi mac -> when it was last counted for phone time
        self._pt_day = ""
        self._last_fixed = 0.0
        # The robot vacuum as UniFi's access point hears it, one sample per poll while PadSpan knows its IP:
        # (t, ap mac, signal = the access point's side in dBm, band, tx_rate, rx_rate). In memory only, so
        # after a restart the readings from before have no access point side.
        self._robot_ring: deque = deque(maxlen=ROBOT_RING_N)
        self._unsub = None
        self._unsub_stop = None
        self._last_save = 0.0
        self._upkeep_day = ""
        self._answers: dict[tuple, dict[str, Any]] = {}   # heatmap() per filter, until the next poll
        self._faulty = False                     # the last poll failed (warned once, until one goes through)
        self.stats = {"polls": 0, "phone_kept": 0, "phone_carried": 0, "phone_moving": 0, "phone_stale": 0, "fixed_kept": 0,
                      "no_padspan": 0, "errors": 0, "last_error": "", "started": time.time()}
        self.raw_dir = hass.config.path("unifi_utopia_wifi_survey")

    async def async_start(self) -> None:
        stored = await self.store.async_load()
        if isinstance(stored, dict):
            for k in self.data:
                if isinstance(stored.get(k), type(self.data[k])):
                    self.data[k] = stored[k]
        self._last_save = time.time()
        self._unsub = self.coordinator.async_add_listener(self._on_update)
        # Home Assistant stops without unloading the integration: saved then too.
        self._unsub_stop = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_ha_stop)
        await self.hass.async_add_executor_job(self._prune_raw)

    async def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._unsub_stop:
            self._unsub_stop()
            self._unsub_stop = None
        await self._save()

    async def _on_ha_stop(self, _event) -> None:
        self._unsub_stop = None          # it has fired: nothing left to remove
        await self._save()

    async def _save(self) -> None:
        self._last_save = time.time()
        if any(self.data.values()):      # never licensed, never collected: no file of its own
            await self.store.async_save(self.data)

    @callback
    def _on_update(self) -> None:
        self._answers.clear()            # a new poll: heatmap() works its answer out again
        if not_licensed(self.hass):      # PadSpan doesn't allow Wi-Fi coverage (now): nothing collected this poll
            return
        self.hass.async_create_task(self._collect(), "unifi_utopia wifi survey")

    # ── inputs ────────────────────────────────────────────────────────────────

    def _padspan(self) -> tuple[list[dict], Any]:
        """PadSpan's latest snapshot objects and its model store, or ([], None) when PadSpan is absent."""
        dom = self.hass.data.get(PADSPAN) or {}
        cached = dom.get("snapshot_cache")
        objs: list[dict] = []
        if isinstance(cached, tuple) and len(cached) == 2 and isinstance(cached[1], dict):
            # The copy itself has an age: PadSpan rebuilds it on each of its polls, so it is seconds old,
            # but a paused PadSpan leaves an old one. Each device's age is counted from now, not from then.
            built = cached[0] if isinstance(cached[0], (int, float)) else 0.0
            copy_age = max(0.0, time.monotonic() - built)
            o = cached[1].get("objects")
            lst = o.get("list") if isinstance(o, dict) else o
            # The copy has each device's raw room but not its spot: PadSpan keeps the spot (x_m, y_m,
            # floor_id) and the smoothed room in its presence coordinator and lays them over a copy only
            # when a screen asks. They are laid over here the same way.
            pc = dom.get("presence_coordinator")
            placed = getattr(pc, "data", None)
            placed = placed if isinstance(placed, dict) else {}
            if isinstance(lst, list):
                for x in lst:
                    if isinstance(x, dict):
                        a = x.get("age_s")
                        obj = {**x, "age_s": (a + copy_age) if isinstance(a, (int, float)) else None}
                        p = placed.get(x.get("key"))
                        if isinstance(p, dict):
                            for k in POS_KEYS:
                                if p.get(k) is not None:
                                    obj[k] = p[k]
                        objs.append(obj)
        return objs, dom.get("model")

    def _robot_wifi(self) -> dict[str, Any] | None:
        """PadSpan's robot Wi-Fi readings (vacuum_run.robot_wifi_snapshot, version 1), or None: PadSpan not
        loaded, an older PadSpan without the call, its vacuum store not loaded yet, or a shape this doesn't
        know. Asked only of a PadSpan that is loaded (its module is imported then); never imported here."""
        if PADSPAN not in self.hass.data:
            return None
        fn = getattr(sys.modules.get(PADSPAN_RUN_MODULE), "robot_wifi_snapshot", None)
        if not callable(fn):
            return None
        try:
            snap = fn(self.hass)
        except Exception:  # noqa: BLE001 - PadSpan's trouble is not the survey's: no robot this time
            _LOGGER.debug("Wi-Fi survey: PadSpan's robot readings not readable", exc_info=True)
            return None
        if not isinstance(snap, dict) or snap.get("version") != 1 or not isinstance(snap.get("readings"), list):
            return None
        return snap

    def _robot_sample(self, now: float, wl: dict[str, dict]) -> None:
        """The robot as its access point hears it now, into the ring, when PadSpan knows the robot's IP.

        One sample per coordinator update, so the join is only as fine as the poll: every scan_interval
        (DEFAULT_SCAN_INTERVAL 60 s, 10 s at the least in the options). PadSpan reads the robot's Wi-Fi as
        it arrives at a spot and keeps it there about 20 s (35 s when too few receivers heard its tag), so at
        60 s a sample lies within 30 s of every reading (ROBOT_JOIN_S leaves room for a late poll), but only
        about one in three is taken while the robot still stands there: the others are from while it drove,
        up to about one spot (1.5 m) away. UniFi's signal is also the access point's latest report, itself a
        few seconds old. The access point's side is "around then", not "at exactly that spot"."""
        snap = self._robot_wifi()
        ip = str((snap or {}).get("robot_ip") or "")
        if not ip:
            return
        c = next((c for c in wl.values() if str(c.get("ip") or "") == ip), None)
        if c is None:
            return
        radio = str(c.get("radio") or "")
        self._robot_ring.append((now, str(c.get("ap_mac") or "").lower(), float(c["signal"]),
                                 BANDS.get(radio, radio or None), c.get("tx_rate"), c.get("rx_rate")))

    def _bssid_aps(self) -> dict[str, str]:
        """Each access point radio's BSSID -> that access point's mac (stat/device vap_table)."""
        out: dict[str, str] = {}
        for d in getattr(self.coordinator.data, "devices", None) or []:
            if not isinstance(d, dict) or not d.get("mac"):
                continue
            for v in d.get("vap_table") or []:
                if isinstance(v, dict) and v.get("bssid"):
                    out[str(v["bssid"]).lower()] = str(d["mac"]).lower()
        return out

    def robot(self, now: float, floor: str | None = None, band: str | None = None,
              ap: str | None = None) -> dict[str, Any] | None:
        """The robot vacuum's floor-level layer, or None when PadSpan has no robot readings to give.

        Each reading where the robot stood: its own signal (own: what a device at that spot hears) and,
        joined by time, the access point's side (ap_side: what the access point hears from the robot, the
        scale the cells and devices use); never one for the other. Positions are PadSpan's floor metres and
        floor ids, as the cells' and devices' are. The access point is the one the robot names by its BSSID,
        else the one UniFi had it on; the access point's side is left out when the two disagree (it roamed in
        between: another link). Filtered as the rest of the answer is (floor, band, one access point), and
        kept KEEP_S like the rest. A reading without a signal or a spot is left out: nothing to draw. Each
        carries PadSpan's kind ("stop" or "moving") and weight as given (a stop at 1.0 when PadSpan says neither)."""
        snap = self._robot_wifi()
        readings = (snap or {}).get("readings") or []
        if not readings:
            return None
        ring = list(self._robot_ring)
        times = [s[0] for s in ring]
        by_bssid = self._bssid_aps()
        names = {**(self.data.get("aps") or {}), **self._ap_names()}
        want_ap = (ap or "*").lower()
        out = []
        for r in readings:
            if not isinstance(r, dict):
                continue
            ts, sig, x, y = r.get("ts"), r.get("signal"), r.get("x_m"), r.get("y_m")
            if not all(isinstance(v, (int, float)) for v in (ts, sig, x, y)) or ts < now - KEEP_S:
                continue
            moving = r.get("kind") == "moving"
            wt = r.get("weight")
            weight = float(wt) if isinstance(wt, (int, float)) and math.isfinite(wt) and wt >= 0 else 1.0
            t0, t1 = r.get("ts_start"), r.get("ts_end")
            if moving:
                # Taken on the move (PadSpan from 2026-11-25): a sample only this close in time, else none.
                mid = ts
                lo, hi = bisect_left(times, ts - ROBOT_MOVING_JOIN_S), bisect_right(times, ts + ROBOT_MOVING_JOIN_S)
                near = ring[lo:hi]
            elif isinstance(t0, (int, float)) and isinstance(t1, (int, float)) and t1 >= t0:
                # The stop's own start and end (PadSpan from 2026-11-24): only a sample taken while it stood there,
                # the one nearest the stop's middle; one taken while it drove is another spot's link.
                mid = (t0 + t1) / 2
                lo, hi = bisect_left(times, t0 - ROBOT_STOP_SLACK_S), bisect_left(times, t1 + ROBOT_STOP_SLACK_S)
                near = ring[lo:hi]
            else:
                mid = ts + ROBOT_STAND_MID_S      # an older PadSpan: nearest the stop's likely middle, this close or none
                i = bisect_left(times, mid)
                near = [ring[k] for k in (i - 1, i) if 0 <= k < len(ring) and abs(ring[k][0] - mid) <= ROBOT_JOIN_S]
            sample = min(near, key=lambda s: abs(s[0] - mid)) if near else None
            ap_mac = by_bssid.get(str(r.get("bssid") or "").lower()) or (sample[1] if sample else "") or None
            ap_side = sample[2] if sample and sample[1] == ap_mac else None
            # UniFi's band first: Valetudo says "2.4ghz" whatever the radio (LinuxWifiConfigurationCapability, 2026.10.0).
            bd = (sample[3] if sample else None) or ROBOT_FREQ.get(str(r.get("frequency") or ""))
            fl = r.get("floor_id") or None
            if (floor is not None and fl != floor) or (band is not None and bd != band) \
                    or (want_ap != "*" and ap_mac != want_ap):
                continue
            out.append({"x": x, "y": y, "floor": fl, "band": bd, "own": round(float(sig), 1),
                        "ap": names.get(ap_mac, ap_mac) if ap_mac else None,
                        "ap_side": round(ap_side, 1) if ap_side is not None else None,
                        "ts": int(ts), "run": r.get("run_id"),
                        "kind": "moving" if moving else "stop", "weight": weight})
        return {"version": 1, "readings": out}

    @staticmethod
    def _obj_id(o: dict) -> str:
        return str(o.get("padspan_id") or o.get("key") or "")

    def _wireless(self) -> dict[str, dict]:
        cl = getattr(self.coordinator.data, "clients", None) or []
        return {str(c.get("mac", "")).lower(): c for c in cl
                if isinstance(c, dict) and c.get("mac") and not c.get("is_wired") and c.get("signal") is not None}

    def _ap_names(self) -> dict[str, str]:
        out = {}
        for d in getattr(self.coordinator.data, "devices", None) or []:
            # Every UniFi device by name, not only those typed "uap": a gateway or console with its own
            # Wi-Fi serves clients too (a client's ap_mac is that device's mac).
            if isinstance(d, dict) and d.get("mac"):
                out[str(d["mac"]).lower()] = str(d.get("name") or d.get("model") or d["mac"])
        return out

    def links(self, objs: list[dict], wl: dict[str, dict]) -> dict[str, dict]:
        """Wi-Fi mac -> PadSpan object, by hand where set, else by the same name on both sides."""
        by_id = {self._obj_id(o): o for o in objs if self._obj_id(o)}
        labelled = {}
        for o in objs:
            n = norm_name(o.get("user_label"))
            if n:
                labelled.setdefault(n, []).append(o)
        out = {}
        excluded = set(self.data.get("excluded") or [])
        for mac, c in wl.items():
            if mac in excluded:
                continue
            manual = (self.data.get("links") or {}).get(mac)
            if manual and manual.get("ps") in by_id:
                out[mac] = by_id[manual["ps"]]
                continue
            for name in (c.get("name"), c.get("hostname")):
                cand = labelled.get(norm_name(name)) or []
                if len(cand) == 1:
                    out[mac] = cand[0]
                    break
        return out

    def _fixed_points(self, model, wl: dict[str, dict]) -> dict[str, dict]:
        """Wi-Fi mac -> where PadSpan has that device placed (its receivers, its positioned lights/devices)."""
        out: dict[str, dict] = {}
        if model is None:
            return out
        try:
            sp = model.scanner_positions_m() or {}
        except Exception:  # noqa: BLE001 - an older PadSpan without it: no receivers, said in the status
            sp = {}
        for src, p in sp.items():
            if not isinstance(p, dict) or p.get("x_m") is None:
                continue
            for n in (-2,):       # an ESP32's Wi-Fi address is its Bluetooth address minus 2
                m = mac_plus(str(src).lower(), n)
                if m in wl and m not in out:
                    out[m] = {"x": p["x_m"], "y": p["y_m"], "floor": p.get("floor_id"), "kind": "receiver"}
        try:
            lp = model.light_positions_m() or {}
        except Exception:  # noqa: BLE001
            lp = {}
        ereg, dreg = er.async_get(self.hass), dr.async_get(self.hass)
        for eid, p in lp.items():
            if not isinstance(p, dict) or p.get("x_m") is None:
                continue
            ent = ereg.async_get(eid)
            dev = dreg.async_get(ent.device_id) if ent and ent.device_id else None
            for t, v in (dev.connections if dev else ()):
                m = str(v).lower()
                if t == dr.CONNECTION_NETWORK_MAC and m in wl and m not in out:
                    out[m] = {"x": p["x_m"], "y": p["y_m"], "floor": p.get("floor_id"), "kind": "device",
                              "entity": eid}
        return out

    # ── collecting ────────────────────────────────────────────────────────────

    def _add(self, table: str, key: str, t: float, v: float) -> None:
        arr = self.data[table].setdefault(key, [])
        arr.append([int(t), round(float(v), 1)])
        cut = t - KEEP_S
        while arr and (arr[0][0] < cut or len(arr) > KEEP_N):
            arr.pop(0)

    async def _collect(self) -> None:
        try:
            await self._collect_inner()
            self._faulty = False
        except Exception as err:  # noqa: BLE001 - counted and shown in the status; never breaks the poll
            self.stats["errors"] += 1
            self.stats["last_error"] = f"{type(err).__name__}: {err}"[:300]
            if self._faulty:      # still failing: in the log once is enough (the status counts every one)
                _LOGGER.debug("Wi-Fi survey: a poll could not be used: %s", err)
            else:
                self._faulty = True
                _LOGGER.warning("Wi-Fi survey: a poll could not be used: %s", err, exc_info=True)
        finally:
            self._answers.clear()

    def _drop_stale(self, now: float) -> None:
        """The cells and rooms, and the placed devices, whose newest reading is older than KEEP_S. _add trims only
        the key it adds to, so one nothing adds to any more (a device moved, an access point replaced) would stay."""
        cut = now - KEEP_S
        for table in ("cells", "rooms"):
            t = self.data.get(table) or {}
            for k in [k for k, v in t.items() if not v or v[-1][0] < cut]:
                del t[k]
        fixed = self.data.get("fixed") or {}
        for m in [m for m, f in fixed.items() if not f.get("vals") or f["vals"][-1][0] < cut]:
            del fixed[m]

    async def _collect_inner(self) -> None:
        now = time.time()
        self.stats["polls"] += 1
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if day != self._upkeep_day:      # once a day (and on the first poll)
            self._upkeep_day = day
            self._drop_stale(now)
            await self.hass.async_add_executor_job(self._prune_raw)
        wl = self._wireless()
        self._robot_sample(now, wl)
        objs, model = self._padspan()
        if not objs:
            self.stats["no_padspan"] += 1
            return
        aps = self._ap_names()
        raw: list[dict] = []
        for mac, o in self.links(objs, wl).items():
            c = wl[mac]
            band = BANDS.get(str(c.get("radio") or ""), str(c.get("radio") or "?"))
            ap = str(c.get("ap_mac") or "").lower()
            age = o.get("age_s")
            seen = c.get("last_seen")
            x, y, fl, room = o.get("x_m"), o.get("y_m"), o.get("floor_id"), o.get("room")
            rec = {"t": int(now), "kind": "phone", "label": o.get("user_label"), "wifi": mac, "ap": ap,
                   "band": band, "ch": c.get("channel"), "sig": c.get("signal"), "sat": c.get("satisfaction"),
                   "seen_ago": round(now - seen) if isinstance(seen, (int, float)) else None,
                   "room": room, "floor": fl, "x": x, "y": y, "pos_age": age}
            wifi_fresh = isinstance(seen, (int, float)) and now - seen <= FRESH_WIFI_S
            fresh = (isinstance(age, (int, float)) and age <= FRESH_POS_S and wifi_fresh
                     and room and fl and not o.get("_stale"))
            sig = float(c["signal"])
            tr = self._track.setdefault(mac, deque(maxlen=8))
            still = carried = False
            if fresh:
                for (t0, x0, y0, f0, r0) in tr:
                    if STILL_MIN_S <= now - t0 <= STILL_MAX_S and f0 == fl and r0 == room:
                        if x is None or x0 is None or math.hypot(x - x0, y - y0) <= STILL_M:
                            still = True
                tr.append((now, x, y, fl, room))
                if still:
                    self._anchor[mac] = {"t": now, "floor": fl, "room": room, "x": x, "y": y,
                                         "ap": ap, "band": band, "sig": sig}
            else:
                # PadSpan has lost it, but its Wi-Fi says it hasn't moved: same access point and band as
                # when it last sat still, and a signal within a few dB of then. It is still there.
                a = self._anchor.get(mac)
                if (a and wifi_fresh and now - a["t"] <= CARRY_S and ap == a["ap"] and band == a["band"]
                        and abs(sig - a["sig"]) <= CARRY_DB):
                    carried = still = True
                    fl, room, x, y = a["floor"], a["room"], a["x"], a["y"]
            rec["still"] = still
            rec["carried"] = carried
            raw.append(rec)
            if (fresh or carried) and room and fl:
                self._phone_time(now, mac, o.get("user_label"), fl, room, band, ap, sig)
            if not fresh and not carried:
                self.stats["phone_stale"] += 1
                continue
            if not still:
                self.stats["phone_moving"] += 1
                continue
            self.stats["phone_carried" if carried else "phone_kept"] += 1
            self._add("rooms", f"{fl}|{band}|{ap}|{room}", now, sig)
            self._add("rooms", f"{fl}|{band}|*|{room}", now, sig)
            if x is not None and y is not None:
                cx, cy = math.floor(x / CELL_M), math.floor(y / CELL_M)
                self._add("cells", f"{fl}|{band}|{ap}|{cx}|{cy}", now, sig)
                self._add("cells", f"{fl}|{band}|*|{cx}|{cy}", now, sig)
        if now - self._last_fixed >= FIXED_EVERY_S:
            self._last_fixed = now
            self.data["rounds"] = [t for t in self.data.get("rounds") or [] if t >= now - 2 * RECENT_S] + [int(now)]
            pts = {m: p for m, p in self._fixed_points(model, wl).items()
                   if isinstance(wl[m].get("last_seen"), (int, float)) and now - wl[m]["last_seen"] <= FRESH_WIFI_S}
            heard = dict(zip(pts, await asyncio.gather(*(self._device_hears(m, wl[m]) for m in pts))))
            for mac, p in pts.items():
                c = wl[mac]
                band = BANDS.get(str(c.get("radio") or ""), str(c.get("radio") or "?"))
                ap = str(c.get("ap_mac") or "").lower()
                f = self.data["fixed"].setdefault(mac, {"vals": []})
                if f.get("ap") and f["ap"] != ap:
                    # It moved to another access point: kept, so the change shows before and after.
                    before = self._recent(f, now) or [v[1] for v in f["vals"][-12:]]
                    f["changes"] = (f.get("changes") or [])[-4:] + [
                        {"t": int(now), "from": f["ap"], "to": ap, "before": round(_q(before, 0.5), 1) if before else None}]
                f.update({"name": c.get("name") or c.get("hostname") or mac, "x": p["x"], "y": p["y"],
                          "floor": p["floor"], "kind": p["kind"], "band": band, "ap": ap})
                f["vals"].append([int(now), float(c["signal"])])
                f["vals"] = [v for v in f["vals"] if v[0] >= now - KEEP_S][-KEEP_N:]
                hv, src = heard.get(mac) or (None, None)
                if hv is not None:
                    f["dev_vals"] = [v for v in f.get("dev_vals") or [] if v[0] >= now - KEEP_S][-(KEEP_N - 1):] + [[int(now), hv]]
                    f["dev_src"] = src
                self.stats["fixed_kept"] += 1
                raw.append({"t": int(now), "kind": "fixed", "name": f["name"], "wifi": mac, "ap": ap, "band": band,
                            "ch": c.get("channel"), "sig": c.get("signal"), "hears": hv, "hears_from": src,
                            "x": p["x"], "y": p["y"], "floor": p["floor"]})
        # Only devices that have had Wi-Fi clients on them: switches and gateways without Wi-Fi stay out of
        # the list, and an access point stays in it while nobody happens to be on it.
        used = {str(c.get("ap_mac") or "").lower() for c in wl.values()}
        kept = {m: aps.get(m, n) for m, n in (self.data.get("aps") or {}).items()}
        self.data["aps"] = {**kept, **{m: n for m, n in aps.items() if m in used}}
        if now - self._last_save >= SAVE_EVERY_S:
            await self._save()
        if raw and self.raw_log:
            await self.hass.async_add_executor_job(self._write_raw, raw)

    def _write_raw(self, rows: list[dict]) -> None:
        os.makedirs(self.raw_dir, exist_ok=True)
        path = os.path.join(self.raw_dir, time.strftime("%Y-%m-%d", time.localtime()) + ".jsonl")
        with open(path, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")

    def _prune_raw(self) -> None:
        if not os.path.isdir(self.raw_dir):
            return
        cut = time.time() - RAW_KEEP_DAYS * 86400
        for name in os.listdir(self.raw_dir):
            p = os.path.join(self.raw_dir, name)
            if name.endswith(".jsonl") and os.path.getmtime(p) < cut:
                os.remove(p)

    def _phone_time(self, now: float, mac: str, label: Any, fl: str, room: str, band: str, ap: str, sig: float) -> None:
        """Time each phone spends in each room, and how much of it on weak Wi-Fi (seconds, per day)."""
        last = self._last_poll.get(mac)
        self._last_poll[mac] = now
        dt = min(now - last, 120.0) if last else 60.0
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        pt = self.data.setdefault("phone_time", {})
        k = f"{day}|{str(label or mac).replace('|', '/')[:60]}|{fl}|{str(room).replace('|', '/')}|{band}|{ap}"
        row = pt.setdefault(k, [0.0, 0.0, 0.0])         # seconds, seconds weak, signal x seconds
        row[0] = round(row[0] + dt, 1)
        row[1] = round(row[1] + (dt if sig <= WEAK_DBM else 0.0), 1)
        row[2] = round(row[2] + sig * dt, 1)
        if self._pt_day != day:
            self._pt_day = day
            cut = time.strftime("%Y-%m-%d", time.localtime(now - (PHONE_DAYS + 1) * 86400))
            for key in [key for key in pt if key.split("|", 1)[0] < cut]:
                pt.pop(key, None)

    @staticmethod
    def _recent(f: dict, now: float, key: str = "vals") -> list[float]:
        """A fixed device's readings (its access point's side, or with key="dev_vals" its own) since it last
        changed access point, at most the last day."""
        since = max([ch["t"] for ch in f.get("changes") or []] + [now - RECENT_S])
        return [v[1] for v in f.get(key) or [] if v[0] >= since]

    async def _device_hears(self, mac: str, c: dict) -> tuple[float | None, str | None]:
        """How well the device itself hears its access point (dBm), and where that came from: an enabled
        Wi-Fi signal sensor on its Home Assistant device, else a WLED's own /json/info. (None, None) when
        it doesn't say. Never UniFi's own figures: those are the access point's side."""
        try:
            dev = dr.async_get(self.hass).async_get_device(connections={(dr.CONNECTION_NETWORK_MAC, mac)})
            ents = er.async_entries_for_device(er.async_get(self.hass), dev.id) if dev is not None else []
            for ent in ents:
                if ent.platform in ("unifi", "unifi_utopia", "hacs"):
                    continue
                st = self.hass.states.get(ent.entity_id)
                if st is None or st.attributes.get("device_class") != "signal_strength" \
                        or st.attributes.get("unit_of_measurement") != "dBm":
                    continue
                try:
                    v = float(st.state)
                except (TypeError, ValueError):
                    continue
                if -100 <= v < 0:
                    return v, "ha"
            wled = (any(ent.platform == "wled" for ent in ents)
                    or str(getattr(dev, "manufacturer", "") or "").upper() == "WLED"
                    or str(c.get("hostname") or "").lower().startswith("wled"))
            if wled and c.get("ip"):
                v = await self._wled_rssi(str(c["ip"]))
                if v is not None:
                    return v, "wled"
        except Exception:  # noqa: BLE001 - the device's side is a bonus; never breaks the round
            _LOGGER.debug("Wi-Fi survey: no device side for %s", mac, exc_info=True)
        return None, None

    async def _wled_rssi(self, ip: str) -> float | None:
        """A WLED's own reading of its access point, from its info (read-only, local addresses only)."""
        try:
            if not ipaddress.ip_address(ip).is_private:
                return None
        except ValueError:
            return None
        import aiohttp  # noqa: PLC0415
        from homeassistant.helpers.aiohttp_client import async_get_clientsession  # noqa: PLC0415
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(f"http://{ip}/json/info", timeout=aiohttp.ClientTimeout(total=WLED_TIMEOUT_S)) as r:
                if r.status != 200:
                    return None
                info = await r.json(content_type=None)
            v = float(((info or {}).get("wifi") or {}).get("rssi"))
        except Exception:  # noqa: BLE001 - off, busy or not a WLED after all
            return None
        return v if -100 <= v < 0 else None

    def _ap_entities(self, macs) -> dict[str, list[str]]:
        """{access point mac: the entity ids of its own Home Assistant device (access_points.py)}."""
        out: dict[str, list[str]] = {}
        try:
            from .access_points import ap_identifier  # noqa: PLC0415
            dreg, ereg = dr.async_get(self.hass), er.async_get(self.hass)
            for m in macs:
                dev = dreg.async_get_device(identifiers={ap_identifier(m)})
                if dev is not None:
                    out[m] = sorted(e.entity_id for e in er.async_entries_for_device(ereg, dev.id))
        except Exception:  # noqa: BLE001 - placement is a bonus; the estimate still stands
            _LOGGER.debug("Wi-Fi survey: access point devices not readable", exc_info=True)
        return out

    def ap_positions(self) -> list[dict]:
        """Each access point where it is placed in PadSpan's Mapping (its "Online" sensor, placed like any
        device), else where it was put on the Wi-Fi screen before, else estimated from the placed devices
        that hear it best. Every device with radios is listed, connected or not, so each can be placed."""
        from .access_points import access_points  # noqa: PLC0415
        now = time.time()
        aps = dict(self.data.get("aps") or {})
        for m, d in access_points(self.coordinator).items() if getattr(self.coordinator, "data", None) else []:
            aps.setdefault(m, str(d.get("name") or d.get("model") or m))
        placed = self.data.get("ap_pos") or {}
        model = (self.hass.data.get(PADSPAN) or {}).get("model")
        try:
            lp = model.light_positions_m() if model is not None else {}
        except Exception:  # noqa: BLE001 - an older PadSpan: no placements
            lp = {}
        ents = self._ap_entities(aps)
        out = []
        for m, name in sorted(aps.items(), key=lambda kv: kv[1]):
            eids = ents.get(m) or []
            e = {"mac": m, "name": name, "floor": None, "x": None, "y": None, "placed": None,
                 "entity_id": next((x for x in eids if x.startswith("binary_sensor.")), None)}
            hit = next((lp[x] for x in eids if isinstance(lp.get(x), dict) and lp[x].get("x_m") is not None), None)
            if hit:
                e.update({"floor": hit.get("floor_id"), "x": hit.get("x_m"), "y": hit.get("y_m"), "placed": "padspan"})
            elif m in placed:
                e.update({"floor": placed[m].get("floor"), "x": placed[m].get("x"), "y": placed[m].get("y"), "placed": "you"})
            else:
                devs = []
                for f in (self.data.get("fixed") or {}).values():
                    r = self._recent(f, now)
                    if f.get("ap") == m and r and f.get("floor") and f.get("x") is not None:
                        devs.append((_q(r, 0.5), f))
                if devs:
                    devs.sort(key=lambda d: -d[0])
                    fl = devs[0][1]["floor"]
                    top = [d for d in devs if d[1]["floor"] == fl][:3]
                    w = [10 ** (d[0] / 10) for d in top]
                    e.update({"floor": fl, "x": round(sum(wi * d[1]["x"] for wi, d in zip(w, top)) / sum(w), 2),
                              "y": round(sum(wi * d[1]["y"] for wi, d in zip(w, top)) / sum(w), 2), "placed": "estimated"})
            out.append(e)
        return out

    async def set_ap_position(self, mac: str, floor: str | None, x: float | None, y: float | None) -> None:
        pos = self.data.setdefault("ap_pos", {})
        if floor is None or x is None or y is None:
            pos.pop(mac.lower(), None)                # back to the estimate
        else:
            pos[mac.lower()] = {"floor": floor, "x": round(float(x), 2), "y": round(float(y), 2)}
        self._answers.clear()
        await self.store.async_save(self.data)

    def _ap_radios(self) -> list[dict]:
        """Each of your access points' radios, as set and as measured (UniFi's stat/device)."""
        out = []
        for d in getattr(self.coordinator.data, "devices", None) or []:
            if not isinstance(d, dict) or not d.get("radio_table"):
                continue
            stats = {s.get("name"): s for s in d.get("radio_table_stats") or [] if isinstance(s, dict)}
            for r in d["radio_table"]:
                if not isinstance(r, dict):
                    continue
                s = stats.get(r.get("name")) or {}
                ch = s.get("channel") or r.get("channel")
                out.append({"ap": str(d.get("mac", "")).lower(), "ap_name": str(d.get("name") or d.get("model") or d.get("mac")),
                            "state": d.get("state"), "radio": r.get("radio"),
                            "channel": int(ch) if isinstance(ch, (int, float)) or str(ch).isdigit() else None,
                            "width": str(r.get("ht") or ""), "tx_power": s.get("tx_power"), "load": s.get("cu_total"),
                            "self_tx": s.get("cu_self_tx")})
        return out

    def network_findings(self, now: float, usual_gap: dict[str, float]) -> list[dict]:
        """What the access points themselves show: one that's down, two sharing a 2.4 GHz channel while one
        of 1/6/11 is free, strong other networks on your channels, double-width 2.4 GHz channels, one much
        louder on 2.4 GHz than the rest, and the clients taking up the most airtime. Reads only."""
        devices = getattr(self.coordinator.data, "devices", None) or []
        clients = getattr(self.coordinator.data, "clients", None) or []
        rogue = getattr(self.coordinator, "_rogue", None) or []
        pos = {a["mac"]: a for a in self.ap_positions()}
        names = {str(d.get("mac", "")).lower(): str(d.get("name") or d.get("model") or d.get("mac"))
                 for d in devices if isinstance(d, dict)}

        def at(mac: str, band: str = "2.4") -> dict:
            p = pos.get(mac) or {}
            return {"floor": p.get("floor"), "x": p.get("x"), "y": p.get("y"), "band": band}

        def ago(s: float) -> str:
            return f"{s / 86400:.0f} days" if s >= 2 * 86400 else (f"{s / 3600:.0f} hours" if s >= 3600 else f"{s / 60:.0f} minutes")

        def overlaps(a: int, b: int, wide: bool) -> bool:
            return abs(a - b) < (9 if wide else 5)        # 20 MHz 2.4 GHz channels overlap within 4; 40 MHz further

        out: list[dict] = []
        # An access point that's down.
        for d in devices:
            if (not isinstance(d, dict) or not d.get("radio_table") or d.get("disabled") or d.get("adopted") is False
                    or d.get("state") == 1):
                continue
            seen = d.get("last_seen")
            if not isinstance(seen, (int, float)) or now - seen < AP_DOWN_S:
                continue
            mac = str(d.get("mac", "")).lower()
            out.append({"id": f"ap_down:{mac}", "kind": "ap_down", "level": "bad",
                        "title": f"{names.get(mac)} is offline",
                        "detail": f"UniFi last saw it {ago(now - seen)} ago ({time.strftime('%a %b %d %H:%M', time.localtime(seen))}). "
                                  f"While it's down, devices near it use access points further away.",
                        "advice": "If that isn't on purpose, check its power and its cable first.", **at(mac)})

        radios = [r for r in self._ap_radios() if r["state"] == 1 and r["channel"] is not None]
        ng = [r for r in radios if r["radio"] == "ng"]
        mine_ssids = {str(c.get("essid")) for c in clients if isinstance(c, dict) and c.get("essid") and not c.get("is_wired")}
        fresh = [g for g in rogue if isinstance(g, dict) and isinstance(g.get("last_seen"), (int, float))
                 and -120 <= now - g["last_seen"] <= FOREIGN_FRESH_S and isinstance(g.get("channel"), int)
                 and isinstance(g.get("signal"), (int, float)) and str(g.get("essid") or "") not in mine_ssids]

        def crowd(ch: int) -> int:
            return len({g.get("bssid") for g in fresh if g["channel"] <= 14 and g["signal"] >= -70
                        and overlaps(g["channel"], ch, (g.get("bw") or 20) >= 40)})

        # Two of yours on one 2.4 GHz channel while one of 1, 6 and 11 is free.
        free = [c for c in (1, 6, 11) if not any(overlaps(c, r["channel"], False) for r in ng)]
        by_ch: dict[int, list] = {}
        for r in ng:
            by_ch.setdefault(r["channel"], []).append(r)
        for ch, rs in sorted(by_ch.items()):
            if len(rs) < 2 or not free:
                continue
            best = min(free, key=crowd)
            loads = "; ".join(f"{r['ap_name']} is {r['load']}% busy, {r['self_tx']}% of it its own sending"
                              for r in rs if isinstance(r["load"], (int, float)))
            n = crowd(best)
            out.append({"id": f"shared_channel:{ch}", "kind": "shared_channel", "level": "warn",
                        "title": f"{' and '.join(r['ap_name'] for r in rs)} share 2.4 GHz channel {ch}",
                        "detail": (loads + ". " if loads else "") + f"Channel {best} has none of your access points"
                                  + (f" and {n} other network{'s' if n != 1 else ''} at -70 dBm or stronger." if n else
                                     " and no other strong network."),
                        "advice": f"Moving one of them to channel {best} gives each a channel of its own.", **at(rs[0]["ap"])})

        # Strong other networks on your channels.
        strongest: dict[str, dict] = {}
        for g in fresh:
            ch, sig = g["channel"], g["signal"]
            if sig < FOREIGN_DBM:
                continue
            wide = (g.get("bw") or 20) >= 40
            on = [r for r in radios if (ch <= 14) == (r["radio"] == "ng")
                  and (overlaps(ch, r["channel"], wide) if ch <= 14 else ch == r["channel"])]
            if not on:
                continue
            key = f"{g.get('essid') or g.get('bssid')}|{'2.4' if ch <= 14 else '5'}"
            if key not in strongest or sig > strongest[key]["signal"]:
                strongest[key] = {**g, "_on": on}
        for g in sorted(strongest.values(), key=lambda g: -g["signal"])[:5]:
            essid = str(g.get("essid") or "")
            ch, band = g["channel"], ("2.4" if g["channel"] <= 14 else "5")
            hint = ""
            if essid and any(s.lower() in essid.lower() for s in mine_ssids if len(s) >= 4):
                hint = " Its name looks like one of yours: an old router or access point still on?"
            elif ISP_NAME.match(essid):
                hint = " Its name looks like an internet provider's modem: if it's yours, its Wi-Fi is on."
            wide = band == "2.4" and (g.get("bw") or 20) >= 40
            by_mine: dict[int, set] = {}
            for r in g["_on"]:
                by_mine.setdefault(r["channel"], set()).add(r["ap_name"])
            yours = "; ".join(f"channel {c}: {' and '.join(sorted(n))}" for c, n in sorted(by_mine.items(), key=lambda kv: kv[0] != ch))
            out.append({"id": f"foreign:{essid or g.get('bssid')}|{band}", "kind": "foreign", "level": "warn",
                        "title": (f"{essid or 'A hidden network'} is on your channel {ch}" if ch in by_mine
                                  else f"{essid or 'A hidden network'} overlaps your channel {min(by_mine, key=lambda c: abs(c - ch))}"),
                        "detail": f"{names.get(str(g.get('ap_mac', '')).lower(), 'Your access point')} hears it at "
                                  f"{round(g['signal'])} dBm on channel {ch}"
                                  + (", double width (40 MHz), so it spreads over more of the band" if wide else "")
                                  + f". Yours that it overlaps: {yours}.{hint}",
                        "advice": "If it's yours and not needed, turning its Wi-Fi off frees the channel for your devices.",
                        **at(g["_on"][0]["ap"], band)})

        # Double-width channels on your own 2.4 GHz radios.
        for r in ng:
            if "40" in r["width"]:
                out.append({"id": f"wide_24:{r['ap']}", "kind": "wide_24", "level": "warn",
                            "title": f"{r['ap_name']} uses a double-width channel on 2.4 GHz",
                            "detail": f"40 MHz on channel {r['channel']}: 2.4 GHz has room for three channels that "
                                      f"don't overlap (1, 6, 11) at 20 MHz, and one 40 MHz channel covers most of the band.",
                            "advice": "Setting its 2.4 GHz channel width to 20 MHz leaves room for your other access points.",
                            **at(r["ap"])})

        # One of yours much louder on 2.4 GHz than the rest.
        pw = [r for r in ng if isinstance(r["tx_power"], (int, float))]
        if len(pw) >= 2:
            for r in pw:
                rest = [o for o in pw if o is not r]
                if r["tx_power"] - _q([o["tx_power"] for o in rest], 0.5) < LOUD_DB:
                    continue
                gap = usual_gap.get(r["ap"])
                ev = (f" The devices on it that report their own reading hear it about {round(gap)} dB better than it "
                      f"hears them." if gap is not None and gap > 0 else "")
                out.append({"id": f"loud_ap:{r['ap']}", "kind": "loud_ap", "level": "warn",
                            "title": f"{r['ap_name']} is louder on 2.4 GHz than your other access points",
                            "detail": f"It transmits at {r['tx_power']} dBm; "
                                      + ", ".join(f"{o['ap_name']} at {o['tx_power']}" for o in rest) + "." + ev
                                      + " Devices join the access point they hear best, so they join it from further "
                                        "away than it hears them back.",
                            "advice": "Setting its 2.4 GHz transmit power to medium evens that out; anything only it "
                                      "reaches gets a little weaker.",
                            **at(r["ap"])})

        # The clients taking up the most airtime: weak, slow and retried, with a lot to send.
        fixed = self.data.get("fixed") or {}
        hogs = []
        for c in clients:
            if not isinstance(c, dict) or c.get("is_wired"):
                continue
            sig, att, ret = c.get("signal"), c.get("wifi_tx_attempts") or 0, c.get("tx_retries") or 0
            rates = [v / 1000 for v in (c.get("tx_rate"), c.get("rx_rate")) if isinstance(v, (int, float)) and v > 0]
            if not isinstance(sig, (int, float)) or sig > HOG_DBM or att < HOG_ATTEMPTS or not rates:
                continue
            pct = 100 * ret / att if att else 0
            if min(rates) <= HOG_RATE_MBPS or pct >= HOG_RETRY_PCT:
                hogs.append((att, c, pct))
        for att, c, pct in sorted(hogs, key=lambda h: -h[0])[:3]:
            mac = str(c.get("mac", "")).lower()
            ap = str(c.get("ap_mac") or "").lower()
            name = c.get("name") or c.get("hostname") or mac
            f = fixed.get(mac)
            where = ({"floor": f.get("floor"), "x": f.get("x"), "y": f.get("y"), "band": f.get("band")} if f
                     else at(ap, BANDS.get(str(c.get("radio") or ""), "2.4")))
            rx, tx = (c.get("rx_rate") or 0) / 1000, (c.get("tx_rate") or 0) / 1000
            out.append({"id": f"airtime:{mac}", "kind": "airtime", "level": "warn",
                        "title": f"{name} takes up a lot of channel {c.get('channel')}'s airtime",
                        "detail": f"{names.get(ap, 'Its access point')} hears it at {round(c['signal'])} dBm; it sends at "
                                  f"about {round(rx)} Mbps and the access point at {round(tx)} Mbps, and {round(pct)}% of the "
                                  f"access point's sends to it are retried. Slow, repeated sending takes more of the channel "
                                  f"than its data needs, from everything else on channel {c.get('channel')}.",
                        "advice": "Moving it, or an access point, nearer to the other speeds it up and gives the airtime "
                                  "back; if it has a network port, a cable does it best.",
                        **where})
        return out

    def findings(self) -> list[dict]:
        """What is worth doing something about, most urgent first, each with what to do.

        UniFi says how well an access point hears a device. The device itself, where it says (an enabled
        Wi-Fi signal sensor in Home Assistant, or a WLED's own info), says how well it hears the access
        point. A radio path loses the same both ways, so the two differ only by the two transmitters: a
        device that hears its access point well while the access point hears it badly has a weak
        transmitter, not a bad spot (a box, an antenna or the place would weaken both ways). Without the
        device's side a weak reading can be either, and the findings say so instead of guessing."""
        now = time.time()
        names = dict(self.data.get("aps") or {})

        def apn(m: Any) -> str:
            return str(names.get(m, m))

        def where(f: dict) -> dict:
            return {"floor": f.get("floor"), "x": f.get("x"), "y": f.get("y"), "band": f.get("band")}

        out: list[dict] = []
        rounds = [t for t in self.data.get("rounds") or [] if t >= now - RECENT_S]
        devs = []
        for mac, f in (self.data.get("fixed") or {}).items():
            r = self._recent(f, now)
            day = [v for v in f.get("vals") or [] if v[0] >= now - RECENT_S]
            if not r and not day:
                continue
            med = _q(r, 0.5) if r else None
            hears = None
            # Each side read in the same round. When the device reports its own side, everything said about it
            # uses only the rounds that have both (never an older UniFi reading against a newer own one).
            since = max([ch["t"] for ch in f.get("changes") or []] + [now - RECENT_S])
            ap_at = {v[0]: v[1] for v in f.get("vals") or [] if v[0] >= since}
            both = [(ap_at[v[0]], v[1]) for v in f.get("dev_vals") or [] if v[0] in ap_at]
            if both:
                med = _q([a for a, _d in both], 0.5)
                hears = _q([d for _a, d in both], 0.5)
            devs.append({"mac": mac, "f": f, "med": med, "hears": hears,
                         "gap": _q([d - a for a, d in both], 0.5) if both else None,
                         "seen": len(day), "first": min(v[0] for v in f.get("vals") or [[now, 0]])})

        def usual_gap(d: dict) -> float:
            """The gap on this access point for its other devices: its transmitter against an ordinary one."""
            others = [e["gap"] for e in devs if e is not d and e["gap"] is not None and e["f"].get("ap") == d["f"].get("ap")]
            return _q(others, 0.5) if len(others) >= 2 else TX_BASELINE_DB

        gaps_by_ap: dict[str, list[float]] = {}
        for d in devs:
            if d["gap"] is not None:
                gaps_by_ap.setdefault(str(d["f"].get("ap")), []).append(d["gap"])
        usual_by_ap = {a: _q(g, 0.5) for a, g in gaps_by_ap.items() if len(g) >= 2}
        ask = ("Whether its spot or its own transmitter is to blame shows in what it hears itself: turn on its "
               "Wi-Fi signal sensor in Home Assistant so this list can tell the two apart.")
        flagged: set[str] = set()

        # A weak transmitter: it hears its access point well, the access point hears it badly.
        for d in devs:
            if d["gap"] is None or d["med"] > FAR_MIN_DBM:
                continue
            excess = d["gap"] - usual_gap(d)
            if excess < TX_EXCESS_DB:
                continue
            f = d["f"]
            flagged.add(d["mac"])
            tail = " WLED's backup and restore carries its settings across." if f.get("dev_src") == "wled" else ""
            out.append({"id": f"weak_tx:{f.get('name')}", "kind": "weak_tx", "level": "bad" if d["med"] <= WEAK_DBM else "warn",
                        "title": f"{f.get('name')}'s transmitter is weak",
                        "detail": f"It hears {apn(f.get('ap'))} at {round(d['hears'])} dBm, but {apn(f.get('ap'))} hears it at "
                                  f"{round(d['med'])} dBm: about {round(excess)} dB less than the other devices on that access "
                                  f"point send. A radio path loses the same both ways, so this is its transmitter, not its spot.",
                        "advice": "Try another power supply first; if that doesn't help, the board." + tail,
                        **where(f)})

        # Keeps dropping off Wi-Fi: on it for too few of the last day's checks (once watched a whole day).
        if len(rounds) >= 48:
            for d in devs:
                if d["mac"] in flagged or d["first"] > now - RECENT_S or d["seen"] >= DROP_SHARE * len(rounds):
                    continue
                f = d["f"]
                flagged.add(d["mac"])
                tail = f", heard at {round(d['med'])} dBm by {apn(f.get('ap'))}" if d["med"] is not None else ""
                tail += f"; it hears {apn(f.get('ap'))} at {round(d['hears'])} dBm." if d["hears"] is not None else "."
                out.append({"id": f"dropping:{f.get('name')}", "kind": "dropping", "level": "bad",
                            "title": f"{f.get('name')} keeps dropping off Wi-Fi",
                            "detail": f"It was on Wi-Fi for {d['seen']} of the last day's {len(rounds)} checks{tail}",
                            "advice": ("Both ways are weak, so it is at the edge of reach: moving it, or an access point, "
                                       "nearer to the other is the fix.") if d["hears"] is not None and d["hears"] <= DEV_POOR_DBM
                            else ("It hears its access point well, so look at the device: its power supply first."
                                  if d["hears"] is not None else
                                  "That is the edge of reach, or a transmitter or power supply failing. " + ask),
                            **where(f)})

        # On a far access point: a neighbour within a few metres is heard much better by another one.
        for d in devs:
            f = d["f"]
            if d["mac"] in flagged or d["med"] is None or d["med"] > FAR_MIN_DBM or f.get("x") is None:
                continue
            best = None
            for e in devs:
                g = e["f"]
                if (e is d or e["med"] is None or g.get("floor") != f.get("floor") or g.get("ap") == f.get("ap")
                        or g.get("band") != f.get("band") or g.get("x") is None):
                    continue
                dist = math.hypot(g["x"] - f["x"], g["y"] - f["y"])
                if dist <= NEAR_M and e["med"] >= d["med"] + GAP_DB and (best is None or e["med"] > best[0]["med"]):
                    best = (e, dist)
            if not best:
                continue
            e, dist = best
            nb = f"{e['f'].get('name')}, {dist:.1f} m away, is heard at {round(e['med'])} dBm by {apn(e['f'].get('ap'))}"
            if d["hears"] is None:
                flagged.add(d["mac"])
                out.append({"id": f"far_ap_check:{f.get('name')}", "kind": "far_ap_check", "level": "warn",
                            "title": f"{f.get('name')}: a far access point, or a weak transmitter",
                            "detail": f"{apn(f.get('ap'))} hears it at {round(d['med'])} dBm; {nb}. Either it is on the "
                                      f"farther access point or its own transmitter is weak.",
                            "advice": ask, **where(f)})
            elif d["hears"] <= DEV_POOR_DBM:
                flagged.add(d["mac"])
                out.append({"id": f"far_ap:{f.get('name')}", "kind": "far_ap",
                            "level": "bad" if d["med"] <= WEAK_DBM else "warn",
                            "title": f"{f.get('name')} is on a far access point",
                            "detail": f"It and {apn(f.get('ap'))} hear each other weakly ({round(d['hears'])} and "
                                      f"{round(d['med'])} dBm); {nb}.",
                            "advice": "A device chooses its access point from what it hears when it connects, and "
                                      "usually stays with it. Restarting it makes it choose again; this list shows within "
                                      "five minutes what it picked.",
                            **where(f)})

        # Weak, and PadSpan's own receivers on weak Wi-Fi.
        for d in devs:
            f = d["f"]
            if d["mac"] in flagged or d["med"] is None:
                continue
            both = (f" It hears {apn(f.get('ap'))} at {round(d['hears'])} dBm too, so both ways are weak: it is the spot."
                    if d["hears"] is not None else "")
            move = "Moving it, or an access point, nearer to the other is the fix."
            if f.get("kind") == "receiver" and d["med"] <= RECEIVER_WEAK_DBM:
                out.append({"id": f"receiver_weak:{f.get('name')}", "kind": "receiver_weak",
                            "level": "bad" if d["med"] <= WEAK_DBM else "warn",
                            "title": f"PadSpan receiver {f.get('name')} has weak Wi-Fi",
                            "detail": f"{apn(f.get('ap'))} hears it at {round(d['med'])} dBm. Its reports travel that way, "
                                      f"so they can arrive late or not at all, which shows in PadSpan as gaps in tracking.{both}",
                            "advice": (move + " A receiver can often move a little without changing what it covers.")
                            if d["hears"] is not None else ask,
                            **where(f)})
            elif d["med"] <= WEAK_DBM:
                out.append({"id": f"weak:{f.get('name')}", "kind": "weak", "level": "bad",
                            "title": f"{f.get('name')} has very weak Wi-Fi",
                            "detail": f"{apn(f.get('ap'))} hears it at {round(d['med'])} dBm; below about {WEAK_DBM} dBm "
                                      f"Wi-Fi drops out.{both}",
                            "advice": move if d["hears"] is not None else ask,
                            **where(f)})

        # Phones: rooms where they spend time on weak Wi-Fi.
        cut = time.strftime("%Y-%m-%d", time.localtime(now - PHONE_DAYS * 86400))
        agg: dict[tuple, list] = {}
        for k, row in (self.data.get("phone_time") or {}).items():
            parts = k.split("|")
            if len(parts) != 6 or parts[0] < cut:
                continue
            _day, label, fl, room, band, ap = parts
            secs, weak, ssum = row
            a = agg.setdefault((label, fl, room, band), [0.0, 0.0, 0.0, {}])
            a[0] += secs
            a[1] += weak
            a[2] += ssum
            a[3][ap] = a[3].get(ap, 0.0) + weak
        for (label, fl, room, band), (secs, weak, ssum, by_ap) in sorted(agg.items(), key=lambda kv: -kv[1][1]):
            if weak < PHONE_MIN_S or weak < 0.5 * secs:
                continue
            ap = max(by_ap, key=by_ap.get) if by_ap else ""
            hrs = weak / 3600
            span = f"{hrs:.1f} hours" if hrs >= 1 else f"{round(weak / 60)} minutes"
            out.append({"id": f"phone_weak:{label}|{fl}|{room}|{band}", "kind": "phone_weak", "level": "warn",
                        "title": f"{room}: weak Wi-Fi where {label} spends time",
                        "detail": f"{label} spent {span} there on weak Wi-Fi in the last {PHONE_DAYS} days, about "
                                  f"{round(ssum / secs)} dBm on {band} GHz, mostly on {apn(ap)}.",
                        "advice": "This is where an access point nearer, or one moved, makes the most difference to you.",
                        "floor": fl, "room": room, "band": band})

        # Before and after: devices that moved to another access point in the last week.
        for d in devs:
            f = d["f"]
            for ch in (f.get("changes") or [])[-1:]:
                if ch["t"] < now - PHONE_DAYS * 86400:
                    continue
                after = [v[1] for v in f.get("vals") or [] if v[0] >= ch["t"]]
                if not after:
                    continue
                a, b = _q(after, 0.5), ch.get("before")
                better = b is not None and a >= b + 6
                worse = b is not None and a <= b - 6
                when = time.strftime("%a %H:%M", time.localtime(ch["t"]))
                tail = f", {round(b)} dBm before on {apn(ch['from'])}." if b is not None else "."
                out.append({"id": f"changed:{f.get('name')}", "kind": "changed",
                            "level": "good" if better else ("bad" if worse else "info"),
                            "title": f"{f.get('name')} moved to {apn(ch['to'])}",
                            "detail": f"Since {when}, {apn(ch['to'])} hears it at {round(a)} dBm{tail}",
                            "advice": "Better: nothing more to do." if better else ("Worse than before." if worse else ""),
                            **where(f)})

        if getattr(self.coordinator, "data", None) is not None:
            out += self.network_findings(now, usual_by_ap)
        order = {"bad": 0, "warn": 1, "good": 2, "info": 3}
        return sorted(out, key=lambda x: order.get(x["level"], 9))

    # ── answers ───────────────────────────────────────────────────────────────

    def heatmap(self, floor: str | None = None, band: str | None = None, ap: str | None = None) -> dict[str, Any]:
        """What PadSpan draws: cells and rooms per floor / band / access point, and the fixed devices.

        Worked out once per poll for each filter and kept until the next one (or a change made through the
        commands): it runs in the event loop and takes a while with a month of readings. Not to be changed
        by the caller: it is the same answer for the next request."""
        key = (floor, band, ap)
        if key not in self._answers:
            if len(self._answers) >= HEATMAP_KEPT:
                self._answers.clear()
            self._answers[key] = self._build_heatmap(floor, band, ap)
        return self._answers[key]

    def _build_heatmap(self, floor: str | None, band: str | None, ap: str | None) -> dict[str, Any]:
        now = time.time()
        aps = dict(self.data.get("aps") or {})
        want_ap = (ap or "*").lower()

        def keep(fl: str, bd: str, a: str) -> bool:
            return (floor is None or fl == floor) and (band is None or bd == band) and a == want_ap

        cells, rooms, bands, floors = [], [], set(), set()
        for k, vals in (self.data.get("cells") or {}).items():
            fl, bd, a, cx, cy = k.split("|")
            vals = [v for v in vals if v[0] >= now - KEEP_S]
            if not vals:
                continue
            bands.add(bd)
            floors.add(fl)
            if keep(fl, bd, a):
                cells.append({"floor": fl, "band": bd, "x": (int(cx) + 0.5) * CELL_M, "y": (int(cy) + 0.5) * CELL_M,
                              "size": CELL_M, **summarise(vals)})
        for k, vals in (self.data.get("rooms") or {}).items():
            fl, bd, a, room = k.split("|", 3)
            vals = [v for v in vals if v[0] >= now - KEEP_S]
            if not vals:
                continue
            # A room reading counts for its band and floor too: PadSpan can have a phone's room before its spot.
            bands.add(bd)
            floors.add(fl)
            if keep(fl, bd, a):
                rooms.append({"floor": fl, "band": bd, "room": room, **summarise(vals)})
        fixed = []
        for mac, f in (self.data.get("fixed") or {}).items():
            vals = [v for v in f.get("vals") or [] if v[0] >= now - KEEP_S]
            if not vals or (floor is not None and f.get("floor") != floor) or (band is not None and f.get("band") != band):
                continue
            if want_ap != "*" and f.get("ap") != want_ap:
                continue
            bands.add(f.get("band"))
            h = self._recent(f, now, "dev_vals")
            fixed.append({"name": f.get("name"), "kind": f.get("kind"), "floor": f.get("floor"), "x": f.get("x"),
                          "y": f.get("y"), "band": f.get("band"), "ap": aps.get(f.get("ap"), f.get("ap")), **summarise(vals),
                          "hears": round(_q(h, 0.5), 1) if h else None})
        out = {"version": API_VERSION, "generated_at": int(now), "cell_m": CELL_M,
               "floors": sorted(x for x in floors if x), "bands": sorted(x for x in bands if x),
               "aps": self.ap_positions(), "findings": self.findings(),
               "cells": cells, "rooms": rooms, "fixed": fixed, "status": self.status()}
        rb = self.robot(now, floor, band, ap)
        if rb is not None:       # only with PadSpan's robot readings: otherwise the answer is as it was
            out["robot"] = rb
        return out

    def status(self) -> dict[str, Any]:
        objs, model = self._padspan()
        wl = self._wireless() if self.coordinator.data else {}
        lk = self.links(objs, wl) if objs else {}
        return {"padspan": bool(objs), "linked": [{"wifi_name": (wl.get(m) or {}).get("name") or m,
                                                    "padspan_label": o.get("user_label")} for m, o in lk.items()],
                "fixed_devices": len(self.data.get("fixed") or {}), "stats": dict(self.stats)}

    def link_candidates(self) -> dict[str, Any]:
        objs, _m = self._padspan()
        wl = self._wireless()
        lk = self.links(objs, wl)
        return {"wifi": [{"mac": m, "name": c.get("name") or c.get("hostname") or m, "band": BANDS.get(str(c.get("radio")), c.get("radio")),
                          "signal": c.get("signal"), "linked_to": (lk.get(m) or {}).get("user_label"),
                          "manual": m in (self.data.get("links") or {}), "excluded": m in (self.data.get("excluded") or [])}
                         for m, c in sorted(wl.items(), key=lambda kv: str(kv[1].get("name") or kv[0]).lower())],
                "padspan": [{"id": self._obj_id(o), "label": o.get("user_label"), "room": o.get("room")}
                            for o in objs if o.get("user_label")]}

    async def set_link(self, mac: str, ps_id: str | None, exclude: bool = False) -> None:
        mac = mac.lower()
        links = self.data.setdefault("links", {})
        excl = set(self.data.get("excluded") or [])
        links.pop(mac, None)
        excl.discard(mac)
        if exclude:
            excl.add(mac)
        elif ps_id:
            links[mac] = {"ps": ps_id, "set": int(time.time())}
        self.data["excluded"] = sorted(excl)
        self._track.pop(mac, None)
        self._answers.clear()
        await self.store.async_save(self.data)


# ── websocket ─────────────────────────────────────────────────────────────────

def _survey(hass: HomeAssistant) -> WifiSurvey | None:
    from .const import DOMAIN  # noqa: PLC0415
    for v in (hass.data.get(DOMAIN) or {}).values():
        if isinstance(v, dict) and isinstance(v.get("wifi_survey"), WifiSurvey):
            return v["wifi_survey"]
    return None


def _heatmap(hass: HomeAssistant, connection, msg) -> None:
    """Any signed-in user (PadSpan's map views are open to household screens): signal by place, no addresses."""
    s = _survey(hass)
    if s is None:
        connection.send_error(msg["id"], "not_running", "The Wi-Fi survey is not running.")
        return
    why = not_licensed(hass)
    if why:
        connection.send_result(msg["id"], not_licensed_answer(why))
        return
    out = s.heatmap(msg.get("floor"), msg.get("band"), msg.get("ap"))
    if not connection.user.is_admin:
        out = {k: v for k, v in out.items() if k != "status"}     # a copy: the kept answer stays whole
    connection.send_result(msg["id"], out)


def _links(hass: HomeAssistant, connection, msg) -> None:
    s = _survey(hass)
    if s is None:
        connection.send_error(msg["id"], "not_running", "The Wi-Fi survey is not running.")
        return
    why = not_licensed(hass)
    if why:
        connection.send_result(msg["id"], not_licensed_answer(why))
        return
    connection.send_result(msg["id"], {**s.link_candidates(), "status": s.status()})


async def _link_set(hass: HomeAssistant, connection, msg) -> None:
    s = _survey(hass)
    if s is None:
        connection.send_error(msg["id"], "not_running", "The Wi-Fi survey is not running.")
        return
    why = not_licensed(hass)
    if why:
        connection.send_error(msg["id"], "not_licensed", NOT_LICENSED_WORDS[why])
        return
    await s.set_link(msg["mac"], msg.get("padspan_id"), msg.get("exclude", False))
    connection.send_result(msg["id"], {"ok": True, **s.link_candidates()})


async def _ap_set(hass: HomeAssistant, connection, msg) -> None:
    """Where an access point is (floor and spot on PadSpan's plan); no floor = back to the estimate."""
    s = _survey(hass)
    if s is None:
        connection.send_error(msg["id"], "not_running", "The Wi-Fi survey is not running.")
        return
    why = not_licensed(hass)
    if why:
        connection.send_error(msg["id"], "not_licensed", NOT_LICENSED_WORDS[why])
        return
    await s.set_ap_position(msg["mac"], msg.get("floor"), msg.get("x"), msg.get("y"))
    connection.send_result(msg["id"], {"ok": True, "aps": s.ap_positions()})


def _commands(prefix: str) -> tuple:
    """The three commands under one name prefix."""

    @websocket_api.websocket_command({
        vol.Required("type"): f"{prefix}/wifi_heatmap",
        vol.Optional("floor"): vol.All(str, vol.Length(max=64)),
        vol.Optional("band"): vol.In(["2.4", "5", "6"]),
        vol.Optional("ap"): vol.All(str, vol.Length(max=32)),
    })
    @callback
    def ws_wifi_heatmap(hass: HomeAssistant, connection, msg) -> None:
        _heatmap(hass, connection, msg)

    @websocket_api.require_admin
    @websocket_api.websocket_command({vol.Required("type"): f"{prefix}/wifi_links"})
    @callback
    def ws_wifi_links(hass: HomeAssistant, connection, msg) -> None:
        _links(hass, connection, msg)

    @websocket_api.require_admin
    @websocket_api.websocket_command({
        vol.Required("type"): f"{prefix}/wifi_link_set",
        vol.Required("mac"): vol.All(str, vol.Match(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")),
        vol.Optional("padspan_id"): vol.Any(None, vol.All(str, vol.Length(max=200))),
        vol.Optional("exclude", default=False): bool,
    })
    @websocket_api.async_response
    async def ws_wifi_link_set(hass: HomeAssistant, connection, msg) -> None:
        await _link_set(hass, connection, msg)

    @websocket_api.require_admin
    @websocket_api.websocket_command({
        vol.Required("type"): f"{prefix}/wifi_ap_set",
        vol.Required("mac"): vol.All(str, vol.Match(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")),
        vol.Optional("floor"): vol.Any(None, vol.All(str, vol.Length(min=1, max=64))),
        vol.Optional("x"): vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=-1000, max=1000))),
        vol.Optional("y"): vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=-1000, max=1000))),
    })
    @websocket_api.async_response
    async def ws_wifi_ap_set(hass: HomeAssistant, connection, msg) -> None:
        await _ap_set(hass, connection, msg)

    return (ws_wifi_heatmap, ws_wifi_links, ws_wifi_link_set, ws_wifi_ap_set)


# PadSpan HA 0.38.283 and earlier ask under the integration's old name (UniFi Blocker); they are answered
# under it too until PadSpan asks under unifi_utopia.
LEGACY_PREFIX = "unifiblocker"


def register(hass: HomeAssistant) -> None:
    from .const import DOMAIN  # noqa: PLC0415
    for prefix in (DOMAIN, LEGACY_PREFIX):
        for h in _commands(prefix):
            websocket_api.async_register_command(hass, h)
