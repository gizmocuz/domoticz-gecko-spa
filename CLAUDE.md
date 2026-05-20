# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Domoticz Python plugin that controls Gecko Alliance spas locally over the LAN through the in.touch 2 / in.touch 3 WiFi module. Implementation is built on top of [geckolib](https://github.com/gazoodle/geckolib), which speaks the in.touch UDP protocol on port 10022.

There is no cloud component. An earlier iteration of this plugin targeted `api.geckowatermonitor.com` via Auth0 — that path was removed because (a) the in.touch 2 user base lives on a different backend (`api-intouch2.geckoal.com`) than the Water Monitor / in.touch 3 backend the Auth0 client is wired to, and (b) the Water Monitor Auth0 application only accepts Authorization Code + PKCE (no Resource Owner Password Grant), which makes it unusable from a server-side plugin without an interactive browser login. Local control via geckolib bypasses all of that.

## Running / Testing

```bash
pip install geckolib            # in Domoticz's Python
python Tests/discover_lan.py    # confirm reachability + dump spa state
python Tests/plugin_smoke.py    # exercise plugin.py end-to-end with a Domoticz stub
```

Both test scripts work against the real spa on the LAN; there's no simulator wiring here. After code changes, restart Domoticz so it reloads `plugin.py` and re-enable the "Gecko Spa / Hot Tub" hardware. Set hardware Debug to `All` (-1) for full geckolib trace.

Plugin metadata (key, params, version) lives in the XML docstring at the top of `plugin.py` — Domoticz parses it directly. Bumping the docstring requires a Domoticz restart, not just a hardware re-enable.

## Architecture

```
Domoticz thread                 Bridge thread (background)
─────────────                   ──────────────────────────
onStart()    ───► GeckoBridge.start(ip)
                       │  spawns asyncio loop running:
                       │     async with _SpaMan(client_id):
                       │         await async_locate_spas(spa_address=ip)
                       │         await async_set_spa_info(...)
                       │         while not stop_requested:
                       │             _refresh_snapshot()   # reads facade
                       │             await sleep(5)
onHeartbeat()─► bridge.snapshot() ──────► (returns latest dict)
   │     update Domoticz devices from snapshot
   │
onCommand() ─► bridge.set_setpoint/set_pump/... 
                       │  run_coroutine_threadsafe -> facade.async_*()
onStop()    ─► bridge.stop()  (sets flag, joins thread)
```

Key design points:

- **Single spa.** Discovery picks the first descriptor returned; the optional `Address` hardware param scopes the locate to one IP. Multi-spa support would require keeping a list of `_SpaMan` instances or extending the bridge.
- **Snapshot, not callbacks.** geckolib pushes state changes internally; the bridge polls `facade` every 5s and stuffs a flat dict behind a lock. `onHeartbeat` reads that dict (no async, no blocking) and pushes to Domoticz. Avoids cross-thread Domoticz API calls.
- **Dynamic device layout.** Devices are created from what `facade.pumps / lights / blowers / water_care` actually report — modes for selector switches come straight from `entity.modes` (e.g. `['OFF','LO','HI']` for pumps vs `['OFF','ON']` for a waterfall). Don't hardcode counts or speed labels.
- **Unit numbering** (single-spa block):
  - `1` Thermostat 6 (combined current temp + setpoint; Type 73 SubType 0; sValue `"temp;setpoint"`; setpoint command arrives as `Set Level` with `Level=<°C>`)
  - `2` (retired — was the stand-alone Setpoint pre-2.1.0)
  - `3` Heating
  - `4..7` pumps (max 4)
  - `8..11` lights (max 4)
  - `12..15` blowers (max 4)
  - `16` Watercare, `17` Gateway connected, `18` Status text
- **Windows quirk.** Python 3.13's default Proactor asyncio loop can't send UDP broadcasts (`WinError 10022`). The bridge thread switches to `WindowsSelectorEventLoopPolicy` before creating its loop. Don't remove that branch.
- **Stable client UUID.** `CLIENT_ID` is hard-coded so the transmitter recognises repeat connections instead of accumulating fresh client entries on every restart.
- **`shared="true"` is mandatory** (set in the `<plugin>` XML). Without it, Domoticz runs each plugin in a `Py_NewInterpreter()` sub-interpreter. geckolib's `loop.run_in_executor(None, ...)` plus the foreign Python `_DummyThread` instances that Domoticz's own C++ host threads create combine to prevent `Py_EndInterpreter` from completing when the hardware is disabled. The stalled teardown holds `MainWorker::m_devicemutex`, so the next enable click freezes the entire Domoticz UI inside `MainWorker::GetHardware()`. Switching to the shared interpreter sidesteps the whole teardown cascade. The trade-off is shared `sys.modules` / asyncio policy / logging config with other Python plugins, which is fine here (network-only, single instance).
- **Executor lifecycle.** We install our own `ThreadPoolExecutor` as the loop's default executor and `shutdown(wait=True, cancel_futures=True)` it explicitly at shutdown. asyncio's default executor cannot be reliably joined under sub-interpreters; even under the shared interpreter, owning the executor lets us deterministically cancel queued geckolib I/O at stop time.

## Conventions

- `plugin.py` is the only file Domoticz loads. The `Tests/` directory is purely diagnostic.
- `geckolib` is a hard runtime dependency — installed via `pip install geckolib` outside of Domoticz's plugin loader. The plugin logs a clear error if the import fails instead of crashing.
- When changing the XML docstring (params, version), bump the `version=` attribute.
- Don't log the spa's identifier at INFO if it could leak personal location data — name/IP are fine.
