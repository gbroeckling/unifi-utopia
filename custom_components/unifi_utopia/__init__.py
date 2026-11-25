"""UniFi Utopia – network device review & quarantine for Home Assistant."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DOMAIN = "unifi_utopia"
PLATFORMS = ["sensor", "binary_sensor"]

try:
    from homeassistant.helpers.config_validation import config_entry_only_config_schema
    CONFIG_SCHEMA = config_entry_only_config_schema(DOMAIN)
except ImportError:
    import homeassistant.helpers.config_validation as cv
    CONFIG_SCHEMA = cv.removed(DOMAIN, raise_if_present=False)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    try:
        await _do_setup(hass, entry)
    except Exception:
        _LOGGER.exception("UniFi Utopia setup failed")
        raise

    return True


async def _do_setup(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """All real setup logic lives here — deferred imports only."""
    import voluptuous as vol
    from homeassistant.helpers import config_validation as cv

    from .const import (
        CONF_HOST, CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_SITE,
        CONF_USERNAME, CONF_VERIFY_SSL, DEFAULT_SCAN_INTERVAL,
        DEFAULT_SITE, DEFAULT_VERIFY_SSL,
        STATE_IGNORED, STATE_QUARANTINED, STATE_TRUSTED,
    )
    from .coordinator import UniFiUtopiaCoordinator
    from .device_store import DeviceStore
    from .unifi_api import UniFiApi

    data = {**entry.data, **entry.options}

    api = UniFiApi(
        host=data[CONF_HOST],
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        site=data.get(CONF_SITE, DEFAULT_SITE),
        verify_ssl=data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
    )

    try:
        await api.login()
    except Exception as err:
        _LOGGER.error("Failed to connect to UniFi controller: %s", err)
        await api.close()
        raise

    from .config_flow import (
        DEFAULT_AUTO_SCAN_COUNT, DEFAULT_LOCAL_CIDR,
        async_remove_old_saved_config, local_prefix_from_cidr,
    )
    await async_remove_old_saved_config(hass)

    store = DeviceStore(hass)
    await store.async_load()

    # Opt-in usage report: load consent before the first poll so it's counted.
    from .policy import DEFAULT_CAMERA_POLICY, DEFAULT_DEVICE_POLICY
    from .usage_report import ReportManager
    report = ReportManager(hass)
    await report.async_load()

    # Port scanner
    try:
        from .port_scanner import PortScanner
        scanner = PortScanner(hass)
        await scanner.async_load()
    except Exception:
        _LOGGER.warning("Port scanner failed to load", exc_info=True)
        scanner = None

    # ONVIF probe engine
    try:
        from .onvif_probe import OnvifProbe
        onvif = OnvifProbe(hass)
        await onvif.async_load()
    except Exception:
        _LOGGER.warning("ONVIF probe failed to load", exc_info=True)
        onvif = None

    # Learning engine
    try:
        from .learning import LearnedPatterns
        learned = LearnedPatterns(hass)
        await learned.async_load()
    except Exception:
        _LOGGER.warning("Learning engine failed to load", exc_info=True)
        learned = None

    # Local network manager
    try:
        from .local_network import LocalNetworkManager
        local_net = LocalNetworkManager(hass)
        await local_net.async_load()
        cidr = data.get("local_cidr", DEFAULT_LOCAL_CIDR)
        if local_net.cidr != cidr:
            await local_net.update_config(subnet=local_prefix_from_cidr(cidr), cidr=cidr)
    except Exception:
        _LOGGER.warning("Local network manager failed to load", exc_info=True)
        local_net = None

    coordinator = UniFiUtopiaCoordinator(
        hass, api, store,
        update_interval=data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        scanner=scanner,
        onvif=onvif,
        learned=learned,
        auto_scan_count=data.get("auto_scan_count", DEFAULT_AUTO_SCAN_COUNT),
        camera_policy=data.get("new_camera_policy", DEFAULT_CAMERA_POLICY),
        device_policy=data.get("new_device_policy", DEFAULT_DEVICE_POLICY),
    )
    coordinator.local_net = local_net
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api, "store": store, "coordinator": coordinator,
        "local_net": local_net, "scanner": scanner, "onvif": onvif,
        "learned": learned, "report": report,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Wi-Fi coverage survey (wifi_survey.py): reads UniFi and PadSpan HA, writes neither;
    # PadSpan HA draws it as a heatmap when this answers.
    try:
        from .wifi_survey import WifiSurvey, register as _register_wifi_survey
        survey = WifiSurvey(hass, coordinator, raw_log=bool(data.get("wifi_raw_log", False)))
        await survey.async_start()
        hass.data[DOMAIN][entry.entry_id]["wifi_survey"] = survey
        # The coroutine function itself: Home Assistant runs it and waits for it while unloading.
        entry.async_on_unload(survey.async_stop)
        _register_wifi_survey(hass)
    except Exception:
        _LOGGER.warning("Wi-Fi survey failed to start", exc_info=True)

    # Options and reconfigure both land here.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    report.refresh_entity_ids(entry.entry_id)
    report.start()
    entry.async_on_unload(report.stop)

    # CISA's known-exploited list for the firmware check: saved copy now,
    # download a couple of minutes after startup and then daily.
    from .vuln import KevFeed
    kev = KevFeed(hass)
    await kev.async_load()
    kev.start()
    entry.async_on_unload(kev.stop)

    # Sidebar panel
    try:
        from .panel import async_register_panel
        await async_register_panel(hass)
    except Exception:
        _LOGGER.warning("Could not register sidebar panel", exc_info=True)

    # WebSocket API
    try:
        from .websocket import async_register_websocket_commands
        async_register_websocket_commands(hass)
    except Exception:
        _LOGGER.warning("Could not register WebSocket commands", exc_info=True)

    # Services
    from homeassistant.helpers.service import async_register_admin_service

    MAC_SCHEMA = vol.Schema({vol.Required("mac"): cv.string})

    if not hass.services.has_service(DOMAIN, "trust_device"):
        async def _ed():
            for _, d in hass.data.get(DOMAIN, {}).items():
                if isinstance(d, dict) and "api" in d: return d
            raise RuntimeError("Not loaded")

        async def h_trust(c):
            from .policy import trust
            d = await _ed(); await trust(d, c.data["mac"])
        async def h_ignore(c):
            d = await _ed(); await d["store"].set_state(c.data["mac"], STATE_IGNORED); await d["coordinator"].async_request_refresh()
        async def h_quarantine(c):
            d = await _ed(); await d["store"].set_state(c.data["mac"], STATE_QUARANTINED); await d["api"].block_client(c.data["mac"]); await d["coordinator"].async_request_refresh()
        async def h_block(c):
            d = await _ed(); await d["api"].block_client(c.data["mac"]); await d["coordinator"].async_request_refresh()
        async def h_unblock(c):
            d = await _ed(); await d["api"].unblock_client(c.data["mac"]); await d["coordinator"].async_request_refresh()
        async def h_reconnect(c):
            d = await _ed(); await d["api"].reconnect_client(c.data["mac"]); await d["coordinator"].async_request_refresh()

        # Admin only: these change the controller.
        for n, h in [("trust_device", h_trust), ("ignore_device", h_ignore), ("quarantine_device", h_quarantine), ("block_device", h_block), ("unblock_device", h_unblock), ("reconnect_device", h_reconnect)]:
            async_register_admin_service(hass, DOMAIN, n, h, schema=MAC_SCHEMA)

    # Tool services, so automations can run scans (e.g. when a new camera
    # event fires). Registered separately so an install that already has
    # the services above still gets these after an update.
    if not hass.services.has_service(DOMAIN, "scan_device"):
        from homeassistant.exceptions import HomeAssistantError

        def _loaded():
            for d in hass.data.get(DOMAIN, {}).values():
                if isinstance(d, dict) and "coordinator" in d:
                    return d
            raise HomeAssistantError("UniFi Utopia is not loaded")

        async def h_scan(c):
            d = _loaded()
            client = d["coordinator"].data.client_by_mac(c.data["mac"]) if d["coordinator"].data else None
            if not client or not client.get("ip"):
                raise HomeAssistantError(f"{c.data['mac']} is not connected or has no IP")
            if not d.get("scanner"):
                raise HomeAssistantError("Port scanner not available")
            await d["scanner"].scan_device(client["ip"], c.data["mac"])
            await d["coordinator"].async_request_refresh()

        async def h_quantify(c):
            d = _loaded()
            if d["coordinator"].data:
                from .websocket import run_quantify
                await run_quantify(hass, d)

        async def h_onvif(c):
            d = _loaded()
            if d.get("onvif"):
                await d["onvif"].discover_and_probe_all()
                await d["coordinator"].async_request_refresh()

        # Admin only as well: they probe devices on the network.
        async_register_admin_service(hass, DOMAIN, "scan_device", h_scan, schema=MAC_SCHEMA)
        async_register_admin_service(hass, DOMAIN, "run_quantify", h_quantify)
        async_register_admin_service(hass, DOMAIN, "onvif_discover", h_onvif)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(hass: HomeAssistant, entry: ConfigEntry, device) -> bool:
    """An access point's device can be deleted once the controller no longer reports that access point
    (one replaced or taken away); one it still reports would come back, so it stays."""
    from .access_points import access_points  # noqa: PLC0415

    macs = {i[1][3:] for i in device.identifiers if i[0] == DOMAIN and i[1].startswith("ap:")}
    coordinator = (hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}).get("coordinator")
    current = access_points(coordinator) if coordinator is not None else {}
    return bool(macs) and not macs & set(current)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, {})
        api = entry_data.get("api")
        if api:
            await api.close()
        from .panel import async_unregister_panel
        async_unregister_panel(hass)
    return unload_ok
