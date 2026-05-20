# Gecko Spa / Hot Tub Plugin for Domoticz

Controls Gecko Alliance spas locally over your LAN through the **in.touch 2** (and in.touch 3) WiFi module. No cloud, no Gecko account, no internet dependency — the plugin talks UDP directly to the spa transmitter on port 10022.

Built on top of [geckolib](https://github.com/gazoodle/geckolib), which reverse-engineered the in.touch binary protocol.

## What it does

- Combined **Thermostat 6** device showing current water temperature + setpoint in one tile (read + write the target temp from the same widget)
- Heating-active indicator
- One Domoticz switch per pump, light, and blower the spa actually reports — with the speed levels the controller exposes (e.g. `OFF/LO/HI` for jets, `OFF/ON` for a waterfall)
- Watercare mode selector (Away From Home / Standard / Energy Saving / Super Energy Saving / Weekender)
- Gateway-connected switch and status text (both read-only — see note below)

Devices are created **dynamically from the live spa**. A spa with 3 pumps and 1 light gets exactly that — no empty placeholders.

## Requirements

- Domoticz with Python plugin support
- Python 3.10+ (matches Domoticz's bundled Python on Windows)
- [`geckolib`](https://pypi.org/project/geckolib/) installed **into the same Python interpreter that Domoticz uses** — not just any system Python:
  ```
  pip install geckolib
  ```
  If `pip` runs against a different interpreter, the plugin will fail at startup with `geckolib is not installed for Domoticz's Python`. To target the right one, invoke pip via the Domoticz Python explicitly, e.g.:
  ```
  "C:\Path\To\Domoticz\python.exe" -m pip install geckolib       # Windows
  /usr/bin/python3 -m pip install geckolib                       # Linux (matches the python3 Domoticz was built against)
  ```
  The interpreter path is whatever Domoticz was configured to use at build time (CMake `-DPYTHON_EXECUTABLE=...`) or, on Windows, the Python whose DLL Domoticz loads at startup. Setup → Settings → System will show the detected Python version.
- Domoticz host and the spa's in.touch module on the same LAN (UDP broadcast for auto-discovery, or a routable IP)

## Install

1. Drop this folder into `domoticz/plugins/GeckoSpa/`.
2. `pip install geckolib` in the same Python that Domoticz uses.
3. Restart Domoticz.

## Set up

Setup → Hardware → add **Gecko Spa / Hot Tub**:

| Field | Notes |
|---|---|
| Spa IP | Leave blank for UDP-broadcast auto-discovery. Set to the in.touch's LAN IP if discovery is blocked (different VLAN, firewall, etc.). |
| Poll interval | How often Domoticz reads the cached snapshot (10 / 30 / 60 / 120 seconds). |
| Debug | `All` (-1) enables geckolib's own trace logging too. |

On first start the plugin connects, enumerates the spa, and creates the devices. Look for log lines like:

```
Starting Gecko Spa plugin. Spa IP=192.168.0.84, poll every 3 heartbeats.
Probing spa at 192.168.0.84 ...
Spa found: Home @ 192.168.0.84 (SPAe8:eb:1b:3b:b1:46)
Spa facade ready: Home (pumps=3, lights=1, blowers=0)
```

If you ever see `geckolib is not installed for Domoticz's Python` — `pip` ran against a different interpreter. Find Domoticz's Python and install there.

## Diagnose without Domoticz

Two standalone scripts in `Tests/` (no Domoticz required):

```
python Tests/discover_lan.py            # confirms reachability + dumps spa state
python Tests/plugin_smoke.py            # runs plugin.py against the live spa with a Domoticz stub
```

See `Tests/README.md` for details.

## Notes

- One spa per hardware entry (the first one found). Multi-spa setups would need separate hardware entries — not currently implemented.
- The plugin uses a fixed client UUID so the transmitter recognises repeat connections instead of accumulating fresh client entries.
- The Watercare mode list comes from your spa — names may differ from the table above on older/newer firmware.
- The **Gateway** device is a Domoticz Switch but is *read-only*: it reflects whether the in.touch transmitter is currently reachable on the LAN (`On` ⇢ `CONNECTED`, `Off` ⇢ any other state). The plugin has no `onCommand` handler for it, so toggling it from the Domoticz UI does nothing to the spa — the next poll will just overwrite the UI back to the real state. The same information is also visible in plaintext on the **Status** device.
- The plugin is declared with `shared="true"`, which makes it run in Domoticz's main Python interpreter instead of a private sub-interpreter. This is **required**: without it, geckolib's async/thread usage prevents `Py_EndInterpreter` from completing on hardware disable, which then deadlocks the next enable inside Domoticz's `MainWorker::GetHardware()` (frozen UI). The trade-off is that the plugin shares the Python interpreter with other Python plugins — `sys.modules`, asyncio policy, and the root logger are all process-wide. For this plugin (network-only, one instance per Domoticz install) that's harmless.

## Why local instead of cloud

Gecko Alliance runs two parallel clouds:

- **`api-intouch2.geckoal.com`** — for in.touch 2 modules, used by the "Gecko – for in.touch 2 module" app. Undocumented REST.
- **`api.geckowatermonitor.com` + `gecko-prod.us.auth0.com`** — for in.touch 3 / Gecko Water Monitor, used by `ha-gecko-integration`. Auth0 application is locked to Authorization Code + PKCE (no Resource Owner Password Grant), so server-side automation needs an interactive browser login — awkward to fit into a Domoticz plugin.

Local UDP via geckolib sidesteps both. It works for in.touch 2 *and* in.touch 3 transmitters, doesn't require an account, has no rate limits, and reacts immediately.

## License

GPLv3 — see [LICENSE](LICENSE).
