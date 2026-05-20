# GeckoSpa plugin – diagnostic tests

Standalone scripts that exercise the LAN path used by `plugin.py`.
All three need `geckolib` installed in the same Python:

```
pip install geckolib
```

## discover_lan.py — LAN discovery + readout

Broadcasts on the LAN, lists spas, connects to the first one, dumps every
entity it sees (water heater, pumps, lights, blowers, watercare, sensors).
Use this to confirm reachability before enabling the Domoticz hardware.

```
python Tests/discover_lan.py                  # auto-discover (broadcast)
python Tests/discover_lan.py --ip 192.168.0.84  # direct
python Tests/discover_lan.py -v                 # geckolib debug logging
```

## plugin_smoke.py — full plugin integration test

Stubs `Domoticz` just enough to import `plugin.py`, then drives `onStart()`
/ `onHeartbeat()` against the real spa and prints the device list the
plugin would create plus the values it would push.

```
python Tests/plugin_smoke.py                  # auto-discover
python Tests/plugin_smoke.py 192.168.0.84     # direct
```

A successful run ends with a device table like:

```
unit   1 Home - Water Temperature   sValue='38.0'
unit   2 Home - Setpoint            sValue='38.0'
unit   3 Home - Heating             sValue='Off'
unit   4 Home - Pump 1              sValue='0'
unit   5 Home - Pump 2              sValue='0'
unit   6 Home - Waterfall           sValue='0'
unit   8 Home - Light               sValue='Off'
unit  16 Home - Watercare           sValue='10'   (= "Standard")
unit  17 Home - Gateway             sValue='On'
unit  18 Home - Status              sValue='CONNECTED'
```

## Notes

- On **Windows + Python 3.13**, the default Proactor asyncio loop cannot
  send UDP broadcasts (`WinError 10022`). Both scripts switch to the
  Selector loop on Windows automatically. `plugin.py` does the same in
  its background bridge thread.
- The plugin's client UUID is fixed so the in.touch transmitter
  recognises repeat connections instead of treating each restart as a
  new client.
