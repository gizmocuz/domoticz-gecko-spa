#!/usr/bin/env python3
"""
Time how long the bridge thread takes to exit after onStop() — replicates
Domoticz's "Plugin has 1 Python threads still running" check.

Connects, waits until ready, then calls onStop() and measures wall time
until the bridge thread is no longer alive.
"""

from __future__ import annotations
import sys, time, types
from pathlib import Path

def _stub():
    m = types.ModuleType("Domoticz")
    log = lambda p: (lambda msg: print(f"[{p}] {msg}", flush=True))
    m.Log, m.Error, m.Debug = log("LOG"), log("ERROR"), log("DEBUG")
    m.Debugging = lambda *_a, **_k: None
    m.Heartbeat = lambda *_a, **_k: None
    class _D:
        def __init__(self, **kw):
            self._kw=kw; self.nValue=0; self.sValue=""
            self.Type=kw.get("Type",0); self.SubType=kw.get("Subtype",kw.get("SubType",0))
        def Create(self): DEVICES[self._kw["Unit"]]=self
        def Update(self, nValue=0, sValue=""): self.nValue,self.sValue=nValue,sValue
    m.Device=_D; m.Connection=lambda **_kw:None
    return m

PARAMS={"Address":"192.168.0.84","Mode1":"30","Mode6":"0"}
DEVICES={}

sys.modules["Domoticz"]=_stub()
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
sys.modules.pop("plugin",None)
import plugin
plugin.Parameters=PARAMS; plugin.Devices=DEVICES
plugin._update.__globals__["Devices"]=DEVICES

print("onStart...")
plugin.onStart()
ready_deadline = time.time()+60
while time.time()<ready_deadline:
    plugin.onHeartbeat()
    if plugin._plugin._bridge and plugin._plugin._bridge.snapshot().get("ready"):
        break
    time.sleep(0.5)

ready = plugin._plugin._bridge.snapshot().get("ready", False)
print(f"facade ready: {ready}")

import threading
def _other_threads():
    me = threading.current_thread()
    return [t for t in threading.enumerate() if t is not me and t.is_alive()]

print("\nThreads BEFORE onStop:")
for t in _other_threads():
    print(f"  - {t.name} daemon={t.daemon}")

print("\nonStop -- timing thread exit...")
t0 = time.time()
thread = plugin._plugin._bridge._thread
plugin.onStop()
elapsed = time.time() - t0
alive = thread.is_alive() if thread is not None else False
print(f"\n>>> onStop returned in {elapsed:.2f}s; bridge thread still alive: {alive}")

# Domoticz polls ~1s after onStop. Wait a few extra seconds to see if any
# leftover thread shows up (executor workers, etc.).
time.sleep(3)
print(f"\nThreads 3s AFTER onStop:")
others = _other_threads()
if not others:
    print("  (none) -- clean shutdown")
else:
    for t in others:
        print(f"  - {t.name} daemon={t.daemon}")
