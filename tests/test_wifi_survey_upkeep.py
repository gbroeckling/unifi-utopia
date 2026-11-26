"""The survey's upkeep: when it saves, its raw log (an option, off by default), dropping what has gone stale,
the heatmap answer kept for a poll, and a fault that lasts warned about once.

Its clock is the test's (Clock): a poll is one coordinator update, a minute apart unless said otherwise.
"""
from __future__ import annotations

import logging
import os
import time
from types import SimpleNamespace

import pytest

from custom_components.unifi_utopia import wifi_survey as ws

AP = "02:00:00:00:00:a1"
RECEIVER_BT = "02:00:00:00:00:12"     # a PadSpan receiver; its Wi-Fi address is its Bluetooth one minus 2
RECEIVER_WIFI = "02:00:00:00:00:10"
DAY = 86400


class Clock:
    """The time module as the survey sees it: time() and monotonic() move only when the test moves them."""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


class Coordinator:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.data = SimpleNamespace(
            clients=[{"mac": RECEIVER_WIFI, "name": "Receiver one", "signal": -61, "radio": "ng", "channel": 6,
                      "ap_mac": AP, "last_seen": clock.now}],
            devices=[{"mac": AP, "name": "Hallway AP", "model": "U6", "state": 1, "last_seen": clock.now,
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


@pytest.fixture
def clock(monkeypatch) -> Clock:
    # Mid-morning local time, so a minute or an hour on stays the same day.
    lt = time.localtime()
    c = Clock(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 10, 0, 0, 0, 0, -1)))
    monkeypatch.setattr(ws, "time", c)
    return c


async def start(hass, padspan, clock, **kw) -> ws.WifiSurvey:
    padspan.allowed = True
    hass.data["padspan_ha"].update({
        "snapshot_cache": (clock.monotonic(), {"objects": {"list": [{"key": "phone1", "user_label": "Phone one",
                                                                     "age_s": 1.0}]}}),
        "model": Model()})
    s = ws.WifiSurvey(hass, Coordinator(clock), **kw)
    await s.async_start()
    hass.data["unifi_utopia"] = {"entry1": {"wifi_survey": s}}
    ws.register(hass)
    s.saves = []
    real_save = s.store.async_save

    async def counted(data):
        s.saves.append(clock.now)
        await real_save(data)

    s.store.async_save = counted
    return s


async def poll(hass, survey, clock, minutes: float = 1) -> None:
    """The clock on by this much, the controller seeing its clients then, and one coordinator update."""
    clock.now += minutes * 60
    for c in survey.coordinator.data.clients:
        c["last_seen"] = clock.now
    for cb in list(survey.coordinator.listeners):
        cb()
    await hass.async_block_till_done()


async def test_saved_every_fifteen_minutes_and_when_it_stops(hass, padspan, clock):
    s = await start(hass, padspan, clock)
    t0 = clock.now
    for _ in range(14):                            # 0.4.2 put the save off again on each of these
        await poll(hass, s, clock)
    assert s.data["fixed"][RECEIVER_WIFI]["vals"] and s.saves == []
    await poll(hass, s, clock)                     # 15 minutes since it started
    assert s.saves == [t0 + 15 * 60]
    for _ in range(15):
        await poll(hass, s, clock)
    assert s.saves == [t0 + 15 * 60, t0 + 30 * 60]

    await hass.bus.fire("homeassistant_stop")       # Home Assistant stops (it doesn't unload integrations)
    assert len(s.saves) == 3
    await s.async_stop()                            # and an unload after that has nothing left to remove
    assert len(s.saves) == 4 and s.store.saved["fixed"][RECEIVER_WIFI]["vals"]


async def test_unload_saves_and_stops_listening(hass, padspan, clock):
    s = await start(hass, padspan, clock)
    await poll(hass, s, clock)
    await s.async_stop()
    assert len(s.saves) == 1 and s.coordinator.listeners == [] and hass.bus.once["homeassistant_stop"] == []


async def test_raw_log_is_off_by_default(hass, padspan, clock):
    s = await start(hass, padspan, clock)
    await poll(hass, s, clock)
    assert s.data["fixed"][RECEIVER_WIFI]["vals"]
    assert not os.path.exists(s.raw_dir)


async def test_raw_log_on_keeps_seven_days_pruned_when_the_day_changes(hass, padspan, clock):
    s = await start(hass, padspan, clock, raw_log=True)
    os.makedirs(s.raw_dir)

    def old_file(name: str, age_days: float) -> str:
        p = os.path.join(s.raw_dir, name)
        open(p, "w").close()
        os.utime(p, (clock.now - age_days * DAY, clock.now - age_days * DAY))
        return p

    week_old, yesterday = old_file("week-old.jsonl", 8), old_file("yesterday.jsonl", 1)
    await poll(hass, s, clock)
    files = [f for f in os.listdir(s.raw_dir) if f not in ("yesterday.jsonl",)]
    assert not os.path.exists(week_old) and os.path.exists(yesterday)
    assert len(files) == 1                         # today's
    with open(os.path.join(s.raw_dir, files[0]), encoding="utf-8") as f:
        assert '"wifi":"02:00:00:00:00:10"' in f.read()

    later = old_file("later.jsonl", 8)             # aged out during the day: stays until the day changes
    await poll(hass, s, clock, minutes=60)
    assert os.path.exists(later)
    await poll(hass, s, clock, minutes=24 * 60)
    assert not os.path.exists(later) and os.path.exists(yesterday)


async def test_stale_keys_dropped_once_a_day(hass, padspan, clock):
    s = await start(hass, padspan, clock)
    old, fresh = [[int(clock.now - 31 * DAY), -70.0]], [[int(clock.now - 2 * DAY), -60.0]]
    s.data["cells"].update({"ground|2.4|*|0|0": list(old), "ground|2.4|*|1|0": list(fresh)})
    s.data["rooms"].update({"ground|2.4|*|Den": list(old), "ground|2.4|*|Hall": list(fresh)})
    s.data["fixed"]["02:00:00:00:00:99"] = {"vals": list(old), "dev_vals": list(old), "ap": AP, "name": "Gone"}
    await poll(hass, s, clock)
    assert set(s.data["cells"]) == {"ground|2.4|*|1|0"} and set(s.data["rooms"]) == {"ground|2.4|*|Hall"}
    assert set(s.data["fixed"]) == {RECEIVER_WIFI}

    s.data["cells"]["ground|2.4|*|5|5"] = list(old)
    await poll(hass, s, clock, minutes=60)
    assert "ground|2.4|*|5|5" in s.data["cells"]   # once a day, not every poll
    await poll(hass, s, clock, minutes=24 * 60)
    assert "ground|2.4|*|5|5" not in s.data["cells"]
    assert "ground|2.4|*|1|0" in s.data["cells"]    # 3 days old now: kept


class Connection:
    def __init__(self, admin: bool = True) -> None:
        self.user = SimpleNamespace(is_admin=admin)
        self.results: list = []
        self.errors: list = []

    def send_result(self, msg_id, result) -> None:
        self.results.append(result)

    def send_error(self, msg_id, code, message) -> None:
        self.errors.append((code, message))


async def ask(hass, kind: str, admin: bool = True, **fields) -> dict:
    conn = Connection(admin)
    out = hass.ws_commands[f"unifi_utopia/{kind}"](hass, conn, {"id": 1, "type": f"unifi_utopia/{kind}", **fields})
    if out is not None:
        await out
    assert conn.errors == []
    return conn.results[0]


async def test_heatmap_worked_out_once_per_poll(hass, padspan, clock, monkeypatch):
    s = await start(hass, padspan, clock)
    await poll(hass, s, clock)
    built = []
    real = s._build_heatmap
    monkeypatch.setattr(s, "_build_heatmap", lambda *a: built.append(a) or real(*a))

    first = await ask(hass, "wifi_heatmap")
    assert await ask(hass, "wifi_heatmap") is first and len(built) == 1
    guest = await ask(hass, "wifi_heatmap", admin=False)
    assert "status" not in guest and len(built) == 1
    assert "status" in await ask(hass, "wifi_heatmap")       # the guest's copy left the kept answer whole
    await ask(hass, "wifi_heatmap", band="2.4")
    assert len(built) == 2                                    # another filter: its own answer
    await ask(hass, "wifi_heatmap", ap=AP)
    await ask(hass, "wifi_heatmap", ap=AP.upper())
    assert len(built) == 3                                    # one access point, however it is written
    assert await ask(hass, "wifi_heatmap", ap="*") is first   # "*" is all of them, as no filter is

    await poll(hass, s, clock)                                # a new poll: worked out again
    again = await ask(hass, "wifi_heatmap")
    assert again is not first and len(built) == 4
    await ask(hass, "wifi_ap_set", mac=AP, floor="ground", x=1.0, y=1.0)
    placed = await ask(hass, "wifi_heatmap")                  # a change through the commands: again too
    assert len(built) == 5 and placed["aps"][0]["placed"] == "you"


async def test_a_lasting_fault_warns_once(hass, padspan, clock, monkeypatch, caplog):
    s = await start(hass, padspan, clock)
    broken = {"on": True}
    real = s._collect_inner

    async def collect():
        if broken["on"]:
            raise RuntimeError("controller answered nonsense")
        await real()

    monkeypatch.setattr(s, "_collect_inner", collect)
    caplog.set_level(logging.DEBUG, logger=ws.__name__)

    def warnings() -> int:
        return sum(1 for r in caplog.records if r.levelno == logging.WARNING and r.name == ws.__name__)

    for _ in range(5):
        await poll(hass, s, clock)
    assert warnings() == 1 and s.stats["errors"] == 5
    broken["on"] = False
    await poll(hass, s, clock)                    # it went through: the next fault is news again
    broken["on"] = True
    await poll(hass, s, clock)
    assert warnings() == 2 and s.stats["errors"] == 6


async def test_raw_log_trouble_never_stops_the_survey(hass, padspan, clock, monkeypatch, caplog):
    """A folder named like a log, or a file that can't be removed: the start and the day's first poll go on,
    and the other old files still go."""
    s = ws.WifiSurvey(hass, Coordinator(clock), raw_log=True)
    os.makedirs(os.path.join(s.raw_dir, "folder.jsonl"))
    paths = {n: os.path.join(s.raw_dir, n) for n in ("stuck.jsonl", "old.jsonl")}
    for p in paths.values():
        open(p, "w").close()
        os.utime(p, (clock.now - 8 * DAY, clock.now - 8 * DAY))
    real_remove = os.remove

    def remove(p):
        if p == paths["stuck.jsonl"]:
            raise PermissionError(13, "Permission denied", p)
        real_remove(p)

    monkeypatch.setattr(ws.os, "remove", remove)
    padspan.allowed = True
    hass.data["padspan_ha"].update({"snapshot_cache": (clock.monotonic(), {"objects": {"list": [
        {"key": "phone1", "user_label": "Phone one", "age_s": 1.0}]}}), "model": Model()})
    await s.async_start()
    assert len(s.coordinator.listeners) == 1 and len(hass.bus.once["homeassistant_stop"]) == 1
    assert not os.path.exists(paths["old.jsonl"]) and os.path.exists(paths["stuck.jsonl"])
    assert os.path.isdir(os.path.join(s.raw_dir, "folder.jsonl"))
    assert any("stuck.jsonl" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)

    await poll(hass, s, clock)                     # the day's first poll prunes again, and still collects
    assert s.stats["errors"] == 0 and s.data["fixed"][RECEIVER_WIFI]["vals"]


@pytest.mark.parametrize("failing", ["store load", "raw log prune"])
async def test_a_failed_start_leaves_no_listener(hass, padspan, clock, monkeypatch, failing):
    s = ws.WifiSurvey(hass, Coordinator(clock))

    async def boom():
        raise RuntimeError("storage unreadable")

    def boom_sync():
        raise OSError("disk gone")

    if failing == "store load":
        monkeypatch.setattr(s.store, "async_load", boom)
    else:
        monkeypatch.setattr(s, "_prune_raw", boom_sync)
    with pytest.raises((RuntimeError, OSError)):
        await s.async_start()
    assert s.coordinator.listeners == [] and not hass.bus.once.get("homeassistant_stop")
