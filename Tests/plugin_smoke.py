#!/usr/bin/env python3
"""
Integration smoke test for plugin.py outside of Domoticz.

Stubs the Domoticz module just enough to import plugin.py, calls onStart(),
waits for the bridge to come up against the real spa on the LAN, then prints
the snapshot the plugin would push to Domoticz and exits cleanly.

Usage:
    python Tests/plugin_smoke.py                  # auto-discover
    python Tests/plugin_smoke.py 192.168.0.84     # direct
"""

from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path


def _build_domoticz_stub():
    """Build a minimal Domoticz module mock plus Parameters/Devices globals."""
    mod = types.ModuleType("Domoticz")

    def _log(prefix):
        def _fn(msg):
            print(f"[{prefix}] {msg}", flush=True)
        return _fn

    mod.Log     = _log("LOG")
    mod.Error   = _log("ERROR")
    mod.Debug   = _log("DEBUG")
    mod.Debugging = lambda *_a, **_k: None
    mod.Heartbeat = lambda *_a, **_k: None

    class _StubDevice:
        def __init__(self, **kw):
            self._kw = kw
            self.nValue = 0
            self.sValue = ""
            # Mirror the Domoticz device attributes the plugin reads.
            self.Type    = kw.get("Type", 0)
            self.SubType = kw.get("Subtype", kw.get("SubType", 0))
        def Create(self):
            label = self._kw.get("TypeName") or "Type={}/SubType={}".format(self.Type, self.SubType)
            print(f"[CREATE] {self._kw.get('Name')} unit={self._kw.get('Unit')} {label}")
            DEVICES[self._kw["Unit"]] = self
        def Update(self, nValue=0, sValue=""):
            self.nValue, self.sValue = nValue, sValue

    mod.Device = _StubDevice
    mod.Connection = lambda **_kw: None  # unused in geckolib path
    return mod


PARAMETERS = {"Address": "", "Mode1": "1", "Mode6": "0"}
DEVICES = {}


def main():
    if len(sys.argv) > 1:
        PARAMETERS["Address"] = sys.argv[1]

    # Make the plugin module find the stubs as globals.
    domoticz_mod = _build_domoticz_stub()
    sys.modules["Domoticz"] = domoticz_mod

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    # Force a clean re-import in case of repeat runs
    sys.modules.pop("plugin", None)
    import plugin  # noqa: E402

    # Inject the globals Domoticz would set
    plugin.Parameters = PARAMETERS
    plugin.Devices = DEVICES
    plugin._update.__globals__["Devices"] = DEVICES  # _update reads global Devices

    print("=== onStart ===")
    plugin.onStart()

    # Drive several heartbeats while waiting for the bridge to connect.
    print("=== onHeartbeat loop (up to 60s) ===")
    deadline = time.time() + 60
    last_ready = False
    while time.time() < deadline:
        plugin.onHeartbeat()
        snap = plugin._plugin._bridge.snapshot() if plugin._plugin._bridge else {}
        if snap.get("ready") and not last_ready:
            last_ready = True
            print("--- first snapshot ---")
            print(json.dumps({k: v for k, v in snap.items() if k != "binary_sensors"}, default=str, indent=2))
            print("binary_sensors:", snap.get("binary_sensors"))
            # Let one more heartbeat tick so devices get created + populated
        if last_ready and plugin._plugin._devices_created:
            # Run one more refresh so values land
            plugin.onHeartbeat()
            break
        time.sleep(1)

    if not last_ready:
        print("[ERROR] spa did not become ready in 60s; final snapshot:")
        print(plugin._plugin._bridge.snapshot() if plugin._plugin._bridge else None)
        plugin.onStop()
        return 2

    print("=== Domoticz devices after first push ===")
    for unit, dev in sorted(DEVICES.items()):
        print(f"  unit {unit:>3} {dev._kw.get('Name'):40s} nValue={dev.nValue!s:>5} sValue={dev.sValue!r}")

    print("=== onStop ===")
    plugin.onStop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
