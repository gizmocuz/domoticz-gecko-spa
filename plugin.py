# Gecko Spa / Hot Tub Domoticz Plugin
#
# Author: GizMoCuz
#
# Controls Gecko Alliance spa/hot tub controllers (in.touch 2 / in.touch 3
# transmitters) over the LAN via the geckolib library. No cloud, no account
# required.
#
# Requires: pip install geckolib
#
# NOTE on shared="true" in the plugin tag below: this plugin MUST run in
# Domoticz's main interpreter, not in a private Py_NewInterpreter() sub-
# interpreter. geckolib's asyncio + threading usage triggers Python "dummy"
# thread state on Domoticz's own host threads, which prevents
# Py_EndInterpreter from completing on hardware disable. The stalled
# teardown then holds MainWorker::m_devicemutex, and any subsequent
# enable hangs the whole Domoticz UI in MainWorker::GetHardware().
# Removing shared="true" will reintroduce that deadlock.
#
"""
<plugin key="GeckoSpa" name="Gecko Spa / Hot Tub" author="GizMoCuz" version="2.1.0"
        wikilink="https://wiki.domoticz.com/Plugins"
        externallink="https://github.com/gizmocuz/domoticz-gecko-spa"
        shared="true">
    <description>
        <h2>Gecko Spa Plugin (LAN)</h2><br/>
        Controls Gecko Alliance spas locally via the in.touch 2 / in.touch 3
        WiFi module using the <b>geckolib</b> library. No cloud or account
        is required; the plugin talks UDP directly to the transmitter on
        your LAN.
        <h3>Prerequisites</h3>
        <ul style="list-style-type:square">
            <li>geckolib installed for Domoticz's Python: <code>pip install geckolib</code></li>
            <li>The spa transmitter (in.touch 2 home unit) reachable on UDP port 10022</li>
        </ul>
        <h3>Devices</h3>
        Devices are created dynamically from what the spa actually exposes:
        water temperature, setpoint, heating, every pump/light/blower the
        controller reports, watercare mode, connection status.
    </description>
    <params>
        <param field="Address" label="Spa IP (blank = auto-discover)" width="200px" required="false"/>
        <param field="Mode1" label="Poll interval" width="100px" required="false" default="30">
            <options>
                <option label="10 seconds" value="10"/>
                <option label="30 seconds" value="30" default="true"/>
                <option label="60 seconds" value="60"/>
                <option label="120 seconds" value="120"/>
            </options>
        </param>
        <param field="Mode6" label="Debug" width="150px">
            <options>
                <option label="None" value="0" default="true"/>
                <option label="Python Only" value="2"/>
                <option label="Basic Debugging" value="62"/>
                <option label="Basic+Messages" value="126"/>
                <option label="All" value="-1"/>
            </options>
        </param>
    </params>
</plugin>
"""

import asyncio
import logging
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import Domoticz

try:
    from geckolib import GeckoAsyncSpaMan, GeckoSpaEvent, GeckoSpaState
except ImportError as _ie:
    GeckoAsyncSpaMan = None  # type: ignore
    GeckoSpaEvent = None     # type: ignore
    GeckoSpaState = None     # type: ignore
    _import_error = _ie
else:
    _import_error = None

# ---------------------------------------------------------------------------
# Unit layout (single spa)
# ---------------------------------------------------------------------------

UNIT_THERMOSTAT = 1   # Combined Thermostat 6 (Type=73 Subtype=0): "temp;setpoint"
# (unit 2 was previously a stand-alone Setpoint; retired in v2.1.0)
UNIT_HEATING    = 3
# 4..7 = pumps[0..3]
# 8..11 = lights[0..3]
# 12..15 = blowers[0..3]
UNIT_PUMP_BASE   = 4
UNIT_LIGHT_BASE  = 8
UNIT_BLOWER_BASE = 12
UNIT_WATERCARE   = 16
UNIT_GATEWAY     = 17
UNIT_STATUS      = 18

# Domoticz device-type constants
THERMOSTAT6_TYPE    = 73   # pTypeThermostat6
THERMOSTAT6_SUBTYPE = 0    # sTypeThermostat6Temp -> sValue "temp;setpoint"

MAX_PUMPS   = 4
MAX_LIGHTS  = 4
MAX_BLOWERS = 4

# A stable per-installation UUID. The transmitter uses it to recognise repeat
# clients; randomising it on every restart would just churn its client list.
# This particular UUID is unique to this plugin.
CLIENT_ID = "5d8f1c12-3a52-4b29-9e2c-9f3a1c0d1234"


# ---------------------------------------------------------------------------
# Background asyncio bridge
# ---------------------------------------------------------------------------

class _SpaMan(GeckoAsyncSpaMan if GeckoAsyncSpaMan else object):
    """Concrete SpaMan that signals lifecycle events to the bridge."""

    def __init__(self, client_id, on_event):
        if GeckoAsyncSpaMan is None:
            raise RuntimeError("geckolib not available")
        super().__init__(client_id)
        self._on_event = on_event

    async def handle_event(self, event, **_kwargs):
        try:
            self._on_event(event, self.spa_state)
        except Exception as e:  # pragma: no cover
            Domoticz.Error("handle_event callback raised: {}".format(e))


class GeckoBridge:
    """
    Owns a background thread + asyncio loop running geckolib.

    Thread-safe surface:
        start(spa_ip)         — connect (or auto-discover if spa_ip is None)
        stop()                — clean shutdown
        snapshot()            — dict of current values (for onHeartbeat)
        set_setpoint(t)       — set water target temperature
        set_pump(idx, mode)   — set pump mode (string from .modes)
        set_blower(idx, mode) — set blower mode
        set_light(idx, on)    — turn light on/off
        set_watercare(mode)   — set watercare mode (string from .modes)
    """

    def __init__(self):
        self._loop = None
        self._thread = None
        self._spaman = None
        # _stop_event is created on the bridge loop in _run(); _stop_requested
        # is the fallback flag for the brief window before the event exists.
        self._stop_event = None
        self._stop_requested = False
        # We install our own ThreadPoolExecutor as the loop's default so we
        # can force-cancel its workers at shutdown. asyncio's default executor
        # cannot be reliably joined under Domoticz's Py_NewInterpreter
        # sub-interpreter, leading to "Plugin has 1 Python threads still
        # running" loops.
        self._executor = None

        # Latest snapshot; updated from the bridge thread, read from Domoticz thread.
        self._lock = threading.Lock()
        self._snap = {
            "ready": False,
            "spa_state": "IDLE",
            "spa_name": None,
            "spa_id": None,
            "spa_ip": None,
            "water_temp": None,
            "setpoint": None,
            "min_temp": None,
            "max_temp": None,
            "temp_unit": "C",
            "heating": None,
            "pumps":   [],   # list of {name, mode, modes, is_on}
            "lights":  [],   # list of {name, is_on}
            "blowers": [],   # list of {name, mode, modes, is_on}
            "watercare": {"mode": None, "modes": []},
            "binary_sensors": {},
            "last_update": 0,
        }

    # ------------- thread + loop lifecycle -------------

    def start(self, spa_ip):
        if self._thread is not None:
            return
        self._stop_requested = False
        self._thread = threading.Thread(
            target=self._thread_main,
            args=(spa_ip,),
            name="GeckoSpaBridge",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        # Signal both: the asyncio Event (wakes any in-flight sleep) and the
        # plain flag the polling loop checks each iteration.
        self._stop_requested = True
        loop, event = self._loop, self._stop_event
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                Domoticz.Error("Gecko bridge thread did not exit within 5s; abandoning (daemon).")
            self._thread = None

    def _thread_main(self, spa_ip):
        if sys.platform == "win32":
            # Proactor can't broadcast UDP; geckolib needs the selector loop on Windows.
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        # Install our own executor so we can join its workers at shutdown.
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gecko-io")
        self._loop.set_default_executor(self._executor)
        try:
            self._loop.run_until_complete(self._run(spa_ip))
        except Exception as e:
            Domoticz.Error("Gecko bridge thread crashed: {}".format(e))
        finally:
            Domoticz.Debug("bridge: _run returned, cleaning up...")
            # Cancel any tasks geckolib left behind so loop.close() doesn't block.
            # Wrap the gather in wait_for so a task that swallows CancelledError
            # cannot stall shutdown indefinitely.
            try:
                pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
                if pending:
                    Domoticz.Debug("bridge: cancelling {} pending asyncio task(s)".format(len(pending)))
                    for t in pending:
                        t.cancel()
                    try:
                        self._loop.run_until_complete(
                            asyncio.wait_for(
                                asyncio.gather(*pending, return_exceptions=True),
                                timeout=2))
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        Domoticz.Debug("bridge: pending task drain timed out (continuing)")
            except Exception as e:
                Domoticz.Debug("bridge: task drain raised {}".format(e))

            # Shut down our own executor — geckolib schedules blocking I/O via
            # loop.run_in_executor(None, ...) and we install our own so we can
            # cancel pending futures + join the workers deterministically.
            try:
                self._executor.shutdown(wait=True, cancel_futures=True)
            except Exception as e:
                Domoticz.Debug("bridge: executor shutdown raised {}".format(e))

            try:
                self._loop.close()
            except Exception as e:
                Domoticz.Debug("bridge: loop.close raised {}".format(e))
            self._loop = None
            self._executor = None
            self._stop_event = None
            Domoticz.Debug("bridge: thread exiting.")

    async def _run(self, spa_ip):
        # Events that fire every couple of seconds; useless in the log.
        _silent_events = {"RUNNING_PING_RECEIVED"}

        def _on_event(event, state):
            name = getattr(event, "name", str(event))
            with self._lock:
                self._snap["spa_state"] = getattr(state, "name", str(state))
            if name in _silent_events:
                return
            Domoticz.Debug("geckolib event {} (state={})".format(name, getattr(state, "name", state)))

        # Now that we have a running loop, create the stop-event on it.
        self._stop_event = asyncio.Event()
        if self._stop_requested:
            self._stop_event.set()

        async with _SpaMan(CLIENT_ID, _on_event) as spaman:
            self._spaman = spaman

            # geckolib needs a descriptor (id + name) to actually connect, so even
            # when the user supplied an IP we run a targeted locate on that IP to
            # have the spa send its identifier back. Without it the connection
            # state machine never leaves LOCATED_SPAS.
            if spa_ip:
                Domoticz.Log("Probing spa at {} ...".format(spa_ip))
                await spaman.async_locate_spas(spa_address=spa_ip)
            else:
                Domoticz.Log("Discovering spa on LAN (UDP broadcast) ...")
                await spaman.async_locate_spas()

            descs = spaman.spa_descriptors or []
            if not descs:
                Domoticz.Error(
                    "No spa found{}. Check the IP and that UDP port 10022 is "
                    "reachable.".format(" at " + spa_ip if spa_ip else " on the LAN"))
                return
            d = descs[0]
            Domoticz.Log("Spa found: {} @ {} ({})".format(d.name, d.ipaddress, d.identifier_as_string))
            await spaman.async_set_spa_info(d.ipaddress, d.identifier_as_string, d.name)

            # Wait for connection (facade ready) up to 60s, or until stop requested.
            deadline = time.time() + 60
            while time.time() < deadline:
                if spaman.facade is not None or self._stop_event.is_set():
                    break
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
            if self._stop_event.is_set():
                return
            if spaman.facade is None:
                Domoticz.Error("Timed out waiting for spa facade (state={}).".format(spaman.spa_state))
                return

            with self._lock:
                self._snap["ready"]    = True
                self._snap["spa_name"] = spaman.spa_name or spaman.facade.name
                self._snap["spa_id"]   = spaman.unique_id
                self._snap["spa_ip"]   = d.ipaddress

            Domoticz.Log("Spa facade ready: {} (pumps={}, lights={}, blowers={})".format(
                self._snap["spa_name"],
                len(spaman.facade.pumps or []),
                len(spaman.facade.lights or []),
                len(spaman.facade.blowers or []),
            ))

            # Refresh loop — geckolib pushes updates internally via the protocol,
            # so we just periodically snapshot the facade. Wait on the stop event
            # so onStop() can wake us immediately instead of after the full 5s.
            # Also check _stop_requested directly as a belt-and-braces backup for
            # the case where call_soon_threadsafe(event.set) is delayed across
            # sub-interpreter boundaries (Domoticz runs plugins under
            # Py_NewInterpreter, where cross-thread asyncio signalling is flakier).
            while not (self._stop_event.is_set() or self._stop_requested):
                self._refresh_snapshot()
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    break

    def _refresh_snapshot(self):
        sm = self._spaman
        if sm is None or sm.facade is None:
            return
        f = sm.facade
        snap = {
            "ready":     True,
            "spa_state": getattr(sm.spa_state, "name", str(sm.spa_state)),
            "spa_name":  sm.spa_name or f.name,
            "spa_id":    sm.unique_id,
            "last_update": int(time.time()),
        }

        wh = f.water_heater
        if wh is not None:
            snap["water_temp"] = _as_float(wh.current_temperature)
            snap["setpoint"]   = _as_float(wh.target_temperature)
            snap["min_temp"]   = _as_float(wh.min_temp)
            snap["max_temp"]   = _as_float(wh.max_temp)
            snap["temp_unit"]  = str(wh.temperature_unit or "C").strip() or "C"
            op = str(getattr(wh, "current_operation", "") or "").lower()
            snap["heating"]    = "heat" in op  # "Heating" -> True, "Idle" -> False

        snap["pumps"]   = [_entity_state(p, with_mode=True)  for p in (f.pumps   or [])[:MAX_PUMPS]]
        snap["lights"]  = [_entity_state(l, with_mode=False) for l in (f.lights  or [])[:MAX_LIGHTS]]
        snap["blowers"] = [_entity_state(b, with_mode=True)  for b in (f.blowers or [])[:MAX_BLOWERS]]

        wc = f.water_care
        if wc is not None:
            mode_idx = getattr(wc, "mode", None)
            modes = list(getattr(wc, "modes", []) or [])
            try:
                mode_name = modes[int(mode_idx)] if mode_idx is not None and 0 <= int(mode_idx) < len(modes) else None
            except (TypeError, ValueError):
                mode_name = None
            snap["watercare"] = {"mode": mode_name, "mode_idx": mode_idx, "modes": modes}
        else:
            snap["watercare"] = {"mode": None, "mode_idx": None, "modes": []}

        snap["binary_sensors"] = {}
        for s in (f.binary_sensors or []):
            try:
                snap["binary_sensors"][s.name] = _truthy(getattr(s, "state", None))
            except Exception:
                pass

        # Preserve identifying/static fields between refreshes
        with self._lock:
            for k in ("spa_ip",):
                snap[k] = self._snap.get(k, snap.get(k))
            self._snap = snap

    def snapshot(self):
        with self._lock:
            return dict(self._snap)  # shallow copy; values are scalars / small lists

    # ------------- command surface -------------

    def _submit(self, coro_factory, what):
        """Run coro_factory() (returning a coroutine) on the bridge loop."""
        if self._loop is None or self._spaman is None or self._spaman.facade is None:
            Domoticz.Error("Cannot {}: spa not connected yet.".format(what))
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(coro_factory(), self._loop)
            # Don't block Domoticz on the result; let geckolib confirm via the next poll.
            fut.add_done_callback(lambda f: _log_future(f, what))
        except RuntimeError as e:
            Domoticz.Error("Cannot {}: bridge loop closed ({}).".format(what, e))

    def set_setpoint(self, temp_c):
        f = self._spaman.facade if self._spaman else None
        if f is None or f.water_heater is None:
            Domoticz.Error("No water heater on this spa.")
            return
        self._submit(lambda: f.water_heater.async_set_target_temperature(float(temp_c)),
                     "set setpoint to {}".format(temp_c))

    def set_pump(self, idx, mode):
        f = self._spaman.facade if self._spaman else None
        if f is None or idx >= len(f.pumps or []):
            Domoticz.Error("No pump #{} on this spa.".format(idx))
            return
        self._submit(lambda: f.pumps[idx].async_set_mode(mode),
                     "set pump {} to {}".format(idx, mode))

    def set_blower(self, idx, mode):
        f = self._spaman.facade if self._spaman else None
        if f is None or idx >= len(f.blowers or []):
            Domoticz.Error("No blower #{} on this spa.".format(idx))
            return
        self._submit(lambda: f.blowers[idx].async_set_mode(mode),
                     "set blower {} to {}".format(idx, mode))

    def set_light(self, idx, on):
        f = self._spaman.facade if self._spaman else None
        if f is None or idx >= len(f.lights or []):
            Domoticz.Error("No light #{} on this spa.".format(idx))
            return
        light = f.lights[idx]
        coro = light.async_turn_on() if on else light.async_turn_off()
        self._submit(lambda: coro, "turn light {} {}".format(idx, "on" if on else "off"))

    def set_watercare(self, mode_idx):
        f = self._spaman.facade if self._spaman else None
        if f is None or f.water_care is None:
            Domoticz.Error("No watercare on this spa.")
            return
        self._submit(lambda: f.water_care.async_set_mode(int(mode_idx)),
                     "set watercare to index {}".format(mode_idx))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _truthy(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "on", "yes", "active", "active.", "high", "low", "medium")
    return False

def _entity_state(e, with_mode):
    out = {"name": getattr(e, "name", "?"), "is_on": bool(getattr(e, "is_on", False)),
           "available": bool(getattr(e, "is_available", True))}
    if with_mode:
        out["mode"]  = getattr(e, "mode", None)
        out["modes"] = list(getattr(e, "modes", []) or [])
    return out

def _log_future(fut, what):
    try:
        fut.result()
        Domoticz.Debug("Command OK: {}".format(what))
    except Exception as e:
        Domoticz.Error("Command failed ({}): {}".format(what, e))

def _selector_options(labels):
    return {
        "LevelActions":   "|".join([""] * len(labels)),
        "LevelNames":     "|".join(labels),
        "LevelOffHidden": "false",
        "SelectorStyle":  "0",
    }

def _level_to_index(level):
    """Domoticz selector levels are 0,10,20,... Convert to integer index."""
    try:
        return max(0, int(level) // 10)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class BasePlugin:

    def __init__(self):
        self._bridge = None
        self._poll_interval = 3  # in heartbeats (10s each); set from Mode1 seconds at onStart
        self._heartbeat_count = 0
        self._devices_created = False
        # Per-unit metadata for command dispatch and selector mode lookup:
        # unit -> {"kind": "pump"/"light"/"blower"/"watercare", "idx": int, "modes": [..]}
        self._unit_meta = {}
        # Set by onCommand to make the next heartbeat push a full snapshot,
        # so users don't have to wait up to poll_interval for a command to
        # be visually confirmed against the spa's actual state.
        self._force_next_push = False

    # --- lifecycle ---

    def onStart(self):
        dbg = Parameters.get("Mode6", "0")
        if dbg and dbg != "0":
            Domoticz.Debugging(int(dbg))

        # Mode1 is the poll interval in seconds (10/30/60/120). Domoticz heartbeat
        # ticks every 10s, so divide to get how many heartbeats per poll.
        try:
            poll_seconds = max(10, int(Parameters.get("Mode1", "30")))
        except ValueError:
            poll_seconds = 30
        self._poll_interval = max(1, poll_seconds // 10)

        Domoticz.Heartbeat(10)

        if GeckoAsyncSpaMan is None:
            Domoticz.Error(
                "geckolib is not installed for Domoticz's Python. Run "
                "'pip install geckolib' in the Python that runs Domoticz, "
                "then restart the hardware. Import error: {}".format(_import_error))
            return

        # Lower the noise from geckolib unless Domoticz debug is on.
        logging.getLogger("geckolib").setLevel(
            logging.DEBUG if dbg and dbg != "0" else logging.WARNING)

        spa_ip = (Parameters.get("Address") or "").strip() or None
        Domoticz.Log("Starting Gecko Spa plugin. Spa IP={}, poll every {}s.".format(
            spa_ip or "(auto-discover)", poll_seconds))

        self._bridge = GeckoBridge()
        self._bridge.start(spa_ip)

    def onStop(self):
        Domoticz.Log("Stopping Gecko Spa plugin.")
        if self._bridge is not None:
            self._bridge.stop()
            self._bridge = None

    def onHeartbeat(self):
        if self._bridge is None:
            return
        self._heartbeat_count += 1
        snap = self._bridge.snapshot()

        # Reflect connection state on the Gateway / Status devices once they exist
        if self._devices_created:
            connected = (snap.get("spa_state") == "CONNECTED")
            _update(UNIT_GATEWAY, 1 if connected else 0, "On" if connected else "Off")
            _update(UNIT_STATUS,  0, snap.get("spa_state") or "?")

        if not snap.get("ready"):
            return

        if not self._devices_created:
            self._create_devices(snap)
            self._devices_created = True

        if self._force_next_push or (self._heartbeat_count % self._poll_interval == 0):
            self._force_next_push = False
            self._push_snapshot_to_devices(snap)

    def onCommand(self, Unit, Command, Level, Hue):
        Domoticz.Debug("onCommand Unit={} Command={} Level={}".format(Unit, Command, Level))
        if self._bridge is None:
            return
        # Any successful command schedules a full snapshot push on the next
        # heartbeat (≤10s) so the user sees spa-confirmed state quickly.
        # _push_snapshot_to_devices is a no-op when values haven't changed.
        self._force_next_push = True

        if Unit == UNIT_THERMOSTAT:
            # Setpoint commands on a Thermostat 6 arrive as Command="Set Level",
            # Level=<temperature in °C>.
            try:
                temp = float(Level)
            except (TypeError, ValueError):
                Domoticz.Error("Setpoint requires a numeric level, got {}".format(Level))
                return
            self._bridge.set_setpoint(temp)
            # Optimistically reflect the new setpoint; the next snapshot push will
            # confirm with the actual reading.
            snap = self._bridge.snapshot()
            current = snap.get("water_temp")
            t = "{:.1f}".format(current) if current is not None else ""
            _update(UNIT_THERMOSTAT, 0, "{};{:.1f}".format(t, temp))
            return

        meta = self._unit_meta.get(Unit)
        if meta is None:
            Domoticz.Debug("No handler for unit {}".format(Unit))
            return

        kind = meta["kind"]
        idx  = meta["idx"]

        if kind == "light":
            on = (str(Command).strip().lower() == "on") or _level_to_index(Level) > 0
            self._bridge.set_light(idx, on)
            _update(Unit, 1 if on else 0, "On" if on else "Off")

        elif kind in ("pump", "blower"):
            modes = meta.get("modes") or []
            level_idx = _level_to_index(Level)
            if str(Command).strip().lower() == "off":
                level_idx = 0
            if level_idx >= len(modes):
                level_idx = max(0, len(modes) - 1)
            mode = modes[level_idx] if modes else None
            if mode is None:
                return
            if kind == "pump":
                self._bridge.set_pump(idx, mode)
            else:
                self._bridge.set_blower(idx, mode)
            _update(Unit, level_idx * 10, str(level_idx * 10))

        elif kind == "watercare":
            modes = meta.get("modes") or []
            level_idx = _level_to_index(Level)
            if not (0 <= level_idx < len(modes)):
                Domoticz.Error("Watercare level {} out of range (0..{})".format(level_idx, len(modes) - 1))
                return
            self._bridge.set_watercare(level_idx)
            _update(Unit, level_idx * 10, str(level_idx * 10))

    def onConnect(self, *_):    pass
    def onMessage(self, *_):    pass
    def onDisconnect(self, *_): pass
    def onNotification(self, *_): pass

    # --- device creation ---

    def _create_devices(self, snap):
        # Device names deliberately omit the spa/hardware name — Domoticz already
        # shows the hardware in its own column on the Devices page.
        if UNIT_THERMOSTAT in Devices:
            existing = Devices[UNIT_THERMOSTAT]
            if existing.Type != THERMOSTAT6_TYPE or existing.SubType != THERMOSTAT6_SUBTYPE:
                Domoticz.Error(
                    "Unit {} exists but is not a Thermostat 6 device (Type={}, SubType={}). "
                    "Delete it in Setup -> Devices and restart the hardware to migrate to the "
                    "combined Thermostat 6 layout.".format(
                        UNIT_THERMOSTAT, existing.Type, existing.SubType))
        else:
            Domoticz.Device(Name="Thermostat",
                            Unit=UNIT_THERMOSTAT,
                            Type=THERMOSTAT6_TYPE,
                            Subtype=THERMOSTAT6_SUBTYPE).Create()
        if UNIT_HEATING not in Devices:
            Domoticz.Device(Name="Heating", Unit=UNIT_HEATING, TypeName="Switch").Create()

        for i, p in enumerate(snap.get("pumps", [])):
            unit = UNIT_PUMP_BASE + i
            modes = p.get("modes") or ["OFF", "ON"]
            self._unit_meta[unit] = {"kind": "pump", "idx": i, "modes": modes}
            if unit not in Devices:
                Domoticz.Device(Name=p.get("name") or "Pump {}".format(i + 1),
                                Unit=unit, TypeName="Selector Switch",
                                Options=_selector_options(modes)).Create()

        for i, l in enumerate(snap.get("lights", [])):
            unit = UNIT_LIGHT_BASE + i
            self._unit_meta[unit] = {"kind": "light", "idx": i}
            if unit not in Devices:
                Domoticz.Device(Name=l.get("name") or "Light {}".format(i + 1),
                                Unit=unit, TypeName="Switch").Create()

        for i, b in enumerate(snap.get("blowers", [])):
            unit = UNIT_BLOWER_BASE + i
            modes = b.get("modes") or ["OFF", "ON"]
            self._unit_meta[unit] = {"kind": "blower", "idx": i, "modes": modes}
            if unit not in Devices:
                Domoticz.Device(Name=b.get("name") or "Blower {}".format(i + 1),
                                Unit=unit, TypeName="Selector Switch",
                                Options=_selector_options(modes)).Create()

        wc = snap.get("watercare") or {}
        wc_modes = wc.get("modes") or []
        if wc_modes:
            self._unit_meta[UNIT_WATERCARE] = {"kind": "watercare", "idx": 0, "modes": wc_modes}
            if UNIT_WATERCARE not in Devices:
                Domoticz.Device(Name="Watercare",
                                Unit=UNIT_WATERCARE, TypeName="Selector Switch",
                                Options=_selector_options(wc_modes)).Create()

        if UNIT_GATEWAY not in Devices:
            Domoticz.Device(Name="Gateway", Unit=UNIT_GATEWAY, TypeName="Switch").Create()
        if UNIT_STATUS not in Devices:
            Domoticz.Device(Name="Status", Unit=UNIT_STATUS, TypeName="Text").Create()

    # --- snapshot -> device updates ---

    def _push_snapshot_to_devices(self, snap):
        # Combined Thermostat 6: sValue = "temp;setpoint"
        temp = snap.get("water_temp")
        setp = snap.get("setpoint")
        if temp is not None or setp is not None:
            # Fall back to the other value if one side is briefly missing
            t = "{:.1f}".format(temp) if temp is not None else ""
            s = "{:.1f}".format(setp) if setp is not None else ""
            _update(UNIT_THERMOSTAT, 0, "{};{}".format(t, s))
        if snap.get("heating") is not None:
            _update(UNIT_HEATING, 1 if snap["heating"] else 0,
                    "On" if snap["heating"] else "Off")

        for i, p in enumerate(snap.get("pumps", [])):
            unit = UNIT_PUMP_BASE + i
            modes = self._unit_meta.get(unit, {}).get("modes", [])
            level = _mode_to_level(p.get("mode"), modes)
            _update(unit, level, str(level))

        for i, l in enumerate(snap.get("lights", [])):
            unit = UNIT_LIGHT_BASE + i
            on = bool(l.get("is_on"))
            _update(unit, 1 if on else 0, "On" if on else "Off")

        for i, b in enumerate(snap.get("blowers", [])):
            unit = UNIT_BLOWER_BASE + i
            modes = self._unit_meta.get(unit, {}).get("modes", [])
            level = _mode_to_level(b.get("mode"), modes)
            _update(unit, level, str(level))

        wc = snap.get("watercare") or {}
        wc_meta = self._unit_meta.get(UNIT_WATERCARE)
        if wc_meta and wc.get("mode") is not None:
            modes = wc_meta.get("modes", [])
            try:
                idx = modes.index(wc["mode"])
            except ValueError:
                idx = wc.get("mode_idx") or 0
            _update(UNIT_WATERCARE, idx * 10, str(idx * 10))


def _mode_to_level(current_mode, modes):
    if not modes or current_mode is None:
        return 0
    try:
        return modes.index(str(current_mode)) * 10
    except ValueError:
        return 0


def _update(unit, nvalue, svalue):
    if unit not in Devices:
        return
    d = Devices[unit]
    if d.nValue != nvalue or d.sValue != svalue:
        d.Update(nValue=nvalue, sValue=svalue)


# ---------------------------------------------------------------------------
# Domoticz entry points
# ---------------------------------------------------------------------------

_plugin = BasePlugin()

def onStart():        _plugin.onStart()
def onStop():         _plugin.onStop()
def onHeartbeat():    _plugin.onHeartbeat()
def onCommand(Unit, Command, Level, Hue):
    _plugin.onCommand(Unit, Command, Level, Hue)
def onConnect(*a):    _plugin.onConnect(*a)
def onMessage(*a):    _plugin.onMessage(*a)
def onDisconnect(*a): _plugin.onDisconnect(*a)
def onNotification(*a): _plugin.onNotification(*a)
