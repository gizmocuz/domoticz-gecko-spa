# Gecko Spa / Hot Tub Plugin for Domoticz

A Domoticz Python plugin for controlling **Gecko Alliance** spa, hot tub, and pool equipment via the Gecko cloud API.

This plugin is inspired by the [geckolib](https://github.com/gazoodle/geckolib) library (a local UDP protocol implementation for Gecko in.touch2 transmitters) and adapted from the [ha-gecko-integration](https://github.com/geckoal/ha-gecko-integration) Home Assistant integration which uses the Gecko cloud API.

## Features

- Water temperature monitoring (current and setpoint)
- Heating status indicator
- Light zone control (on/off, up to 2 zones)
- Pump control with speed levels (off/low/medium/high, up to 2 pumps)
- Blower control with speed levels (off/low/medium/high)
- Watercare mode selection (Away, Standard, Savings, Super Savings, Weekender)
- Gateway connectivity status
- Spa status text
- Supports up to 12 spas on a single Gecko account
- Automatic token refresh (no need to re-enter credentials)
- Configurable polling interval

## Requirements

- **Domoticz** 2022.1 or later (with Python plugin support enabled)
- A **Gecko Alliance account** (the same email and password you use in the Gecko mobile app)
- Your spa must already be set up and visible in the Gecko mobile app
- An internet connection (the plugin communicates via the Gecko cloud API)

## Installation

### 1. Copy the plugin

Copy the `GeckoSpa` folder into your Domoticz `plugins` directory:

```
domoticz/
  plugins/
    GeckoSpa/
      plugin.py
      README.md
```

**Linux/macOS:**
```bash
cd ~/domoticz/plugins
git clone https://github.com/gizmocuz/domoticz-gecko-spa.git GeckoSpa
# or manually:
mkdir -p GeckoSpa
cp plugin.py GeckoSpa/
```

**Windows:**
Copy the `GeckoSpa` folder to `C:\Program Files\Domoticz\plugins\` (or wherever your Domoticz is installed).

### 2. Restart Domoticz

Restart the Domoticz service so it picks up the new plugin:

```bash
# Linux
sudo systemctl restart domoticz

# Windows
# Restart via Services (services.msc) or the Domoticz tray icon
```

### 3. Verify plugin detection

After restart, go to **Setup > Hardware** in the Domoticz web UI. In the "Type" dropdown, you should see **"Gecko Spa / Hot Tub"**. If it doesn't appear, check:
- Python plugin support is enabled in Domoticz settings
- The `plugin.py` file is in the correct directory
- Check the Domoticz log for any Python errors

## Setup

### Adding the hardware

1. Go to **Setup > Hardware** in the Domoticz web UI
2. Enter a name (e.g., "My Gecko Spa")
3. Select **"Gecko Spa / Hot Tub"** from the Type dropdown
4. Fill in the fields:

| Field | Description |
|-------|-------------|
| **Gecko Account Email** | Your Gecko Alliance account email address |
| **Gecko Account Password** | Your Gecko Alliance account password |
| **Poll interval** | How often to poll for status updates (default: every 3 heartbeats, ~30 seconds) |
| **Debug** | Debug logging level (leave at "None" unless troubleshooting) |

5. Click **Add**

### What happens next

1. The plugin authenticates with the Gecko cloud (Auth0)
2. It discovers all spas/vessels linked to your account
3. It creates Domoticz devices for each spa
4. Status polling begins at the configured interval

Check the Domoticz log (**Setup > Log**) for messages like:
```
Gecko Spa plugin starting. Poll interval: every 3 heartbeats (~30s).
Authenticating with Gecko cloud...
Authentication successful. Token valid for 86400s.
Found 1 vessel(s) on account.
Created device: My Spa - Water Temperature (unit 1)
Created device: My Spa - Temperature Setpoint (unit 2)
...
```

## Devices Created

For each spa on your account, the plugin creates the following devices:

| Device | Domoticz Type | Access | Description |
|--------|--------------|--------|-------------|
| **Water Temperature** | Temperature | Read-only | Current water temperature |
| **Temperature Setpoint** | Thermostat (SetPoint) | Read/Write | Target water temperature |
| **Heating** | Switch | Read-only | Indicates when heater is active |
| **Light Zone 1** | Switch | Read/Write | First light zone (on/off) |
| **Light Zone 2** | Switch | Read/Write | Second light zone (on/off) |
| **Pump 1** | Selector Switch | Read/Write | First pump (Off/Low/Medium/High) |
| **Pump 2** | Selector Switch | Read/Write | Second pump (Off/Low/Medium/High) |
| **Blower** | Selector Switch | Read/Write | Blower (Off/Low/Medium/High) |
| **Watercare Mode** | Selector Switch | Read/Write | Water care preset |
| **Gateway** | Switch | Read-only | Gateway connectivity status |
| **Status** | Text | Read-only | Spa status text |

> **Note:** Not all spas have all features. Some may have only one pump, no blower, or one light zone. Devices for unavailable features will simply show no data.

### Watercare modes

| Mode | Description |
|------|-------------|
| **Away** | Reduced heating/filtration while away from home |
| **Standard** | Normal operation |
| **Savings** | Energy-saving mode with reduced heating cycles |
| **Super Savings** | Maximum energy savings |
| **Weekender** | Optimized for weekend use |

### Pump speed levels

| Level | Description |
|-------|-------------|
| **Off** | Pump stopped |
| **Low** | Low speed (quiet) |
| **Medium** | Medium speed |
| **High** | High speed (jets) |

## Controlling Your Spa

### Temperature

Click on the **Temperature Setpoint** device in Domoticz to adjust the target water temperature. The spa will heat or cool to reach the setpoint.

### Lights

Toggle the **Light Zone** switches on or off. These correspond to the lighting zones configured on your spa.

### Pumps

Use the **Pump** selector switches to choose a speed level. Select "Off" to stop the pump. The speed levels available depend on your pump type (some pumps only support on/off).

### Watercare Mode

Use the **Watercare Mode** selector to switch between presets. This controls filtration and heating schedules.

### Automation

All devices work with standard Domoticz features:
- **Timers:** Schedule pump and light activation
- **Scenes/Groups:** Create scenes that control multiple spa features at once
- **dzVents/Lua scripts:** Write scripts triggered by temperature changes, time, or other events
- **Notifications:** Get alerts when temperature drops below a threshold

**Example dzVents script** — notify when water is ready:
```lua
return {
    on = { devices = { 'My Spa - Water Temperature' } },
    execute = function(domoticz, device)
        local setpoint = domoticz.devices('My Spa - Temperature Setpoint')
        if device.temperature >= tonumber(setpoint.setPoint) then
            domoticz.notify('Spa Ready', 'Water has reached target temperature!', domoticz.PRIORITY_NORMAL)
        end
    end
}
```

## Authentication

The plugin authenticates with **Auth0** (Gecko's identity provider) using your Gecko account email and password. It uses the OAuth2 Resource Owner Password Grant flow:

1. Your credentials are sent securely over HTTPS to `gecko-prod.us.auth0.com`
2. An access token (valid for ~24 hours) and refresh token are returned
3. The plugin uses the access token for all API calls
4. Before the token expires, it is automatically refreshed using the refresh token
5. Re-authentication only happens if the refresh token is also expired

> **Security note:** Your password is stored in the Domoticz hardware configuration database. Ensure your Domoticz instance is properly secured (HTTPS, strong admin password, no public access without authentication).

## Troubleshooting

### Plugin not appearing in hardware list

- Verify the plugin directory structure:
  ```
  plugins/GeckoSpa/plugin.py
  ```
- Check that Python plugins are enabled in Domoticz (**Setup > Settings > System > Enable Python**)
- Look for Python errors in the Domoticz log at startup

### Authentication fails

- Verify your email and password work in the Gecko mobile app
- Check the Domoticz log for specific error messages
- Enable debug logging (set Debug to "Python Only" or "All" in hardware settings)
- The plugin tries two authentication methods: if the "realm" grant fails, it falls back to standard password grant

### No devices created

- Check that your spa is set up and online in the Gecko mobile app
- Look for "Found X vessel(s)" in the log — if 0 vessels, the API may have returned an unexpected format
- Enable debug logging to see the raw API responses

### Devices show no data

- Your spa may not have all features (e.g., no blower, only one pump)
- Check that the spa is powered on and connected to the internet
- The gateway device should show "On" if the connection is working

### "HTTP 401 Unauthorised" in logs

- The access token has expired and the refresh token also failed
- The plugin will automatically re-authenticate
- If it persists, check your credentials haven't changed

### Debug logging

To enable verbose logging, change the **Debug** setting in hardware configuration:

| Level | What it shows |
|-------|---------------|
| None | Only errors and important status |
| Python Only | Python-level debug messages |
| Basic Debugging | Plugin state machine transitions |
| Basic+Messages | HTTP request/response details |
| Connections Only | Connection lifecycle events |
| Connections+Queue | Connection queue details |
| All | Everything |

## Architecture

### How it works

```
┌─────────────────────┐          HTTPS          ┌─────────────────────┐
│   Domoticz Plugin   │ ◄─────────────────────► │   Auth0 (OAuth2)    │
│   (GeckoSpa)        │    Token management      │ gecko-prod.us.auth0 │
│                     │                          └─────────────────────┘
│  State Machine:     │          HTTPS          ┌─────────────────────┐
│  AUTH → GET_USER    │ ◄─────────────────────► │   Gecko Cloud API   │
│  → GET_VESSELS      │   Vessel discovery &     │ api.geckowatermonitor│
│  → GET_STATUS       │   status polling         │       .com          │
│  → COMMAND          │                          └──────────┬──────────┘
│  → IDLE             │                                     │
└─────────┬───────────┘                                     │
          │ Device updates                        ┌─────────▼──────────┐
          ▼                                       │   Gecko in.touch2  │
┌─────────────────────┐                           │   (at the spa)     │
│   Domoticz Devices  │                           └────────────────────┘
│   (Temperature,     │
│    Switches, etc.)  │
└─────────────────────┘
```

### State machine

The plugin uses an event-driven state machine with `Domoticz.Connection` for all HTTPS communication:

| State | Description |
|-------|-------------|
| `IDLE` | Waiting for next heartbeat or command |
| `AUTH` | Authenticating with Auth0 |
| `REFRESH_TOKEN` | Refreshing an expired access token |
| `GET_USER` | Fetching the user's vessel list |
| `GET_VESSELS` | Fetching detailed vessel information |
| `GET_STATUS` | Polling vessel zone status |
| `COMMAND` | Sending a control command |

### Polling cycle

```
Heartbeat (every ~10s)
  └─► Check poll interval (default: every 3rd heartbeat = ~30s)
       ├─► Token expired? → REFRESH_TOKEN → IDLE
       ├─► No token? → AUTH → GET_USER → GET_VESSELS → IDLE
       └─► Normal → GET_STATUS → update devices → IDLE
```

### Device numbering

Devices are organized in blocks of 20 units per vessel:

```
Vessel 0: units 1-11
Vessel 1: units 21-31
Vessel 2: units 41-51
...
Vessel 11: units 221-231
```

## Related Projects

| Project | Description |
|---------|-------------|
| [geckolib](https://github.com/gazoodle/geckolib) | Python library for local UDP communication with Gecko in.touch2 transmitters. Uses a reverse-engineered binary protocol over the local network (port 10022). This was the original inspiration for Gecko spa integrations. |
| [gecko-home-assistant](https://github.com/gazoodle/gecko-home-assistant) | Home Assistant integration using geckolib for local spa control. |
| [ha-gecko-integration](https://github.com/geckoal/ha-gecko-integration) | Home Assistant integration using the Gecko cloud API (OAuth2 + AWS IoT MQTT). This Domoticz plugin is adapted from this project's cloud-based approach. |
| [gecko-iot-client](https://pypi.org/project/gecko-iot-client/) | Python client library for the Gecko cloud IoT platform, used by ha-gecko-integration. |

### Local vs. Cloud

There are two approaches to controlling Gecko spas:

| | Local (geckolib) | Cloud (this plugin) |
|---|---|---|
| **Protocol** | UDP binary protocol on LAN | HTTPS REST API over internet |
| **Requires** | in.touch2 module on same network | Internet connection + Gecko account |
| **Latency** | Very low (~ms) | Higher (~100-500ms) |
| **Dependency** | No internet needed | Requires Gecko cloud servers |
| **Discovery** | Auto-discovers spas on LAN | Uses account-linked vessels |

This plugin uses the **cloud approach** because Domoticz's plugin framework provides built-in HTTPS connection support, making it straightforward to implement without external dependencies.

## API Notes

The Gecko cloud API (`api.geckowatermonitor.com`) is not publicly documented. The API endpoints used by this plugin are **inferred** from the [ha-gecko-integration](https://github.com/geckoal/ha-gecko-integration) source code and may need adjustment. The plugin includes defensive response parsing that tries multiple field name variants.

If you encounter issues with the API endpoints, enable debug logging and check the raw responses. The key methods to adjust are:

| Method | Purpose |
|--------|---------|
| `_send_get_user()` | Vessel list endpoint |
| `_send_get_status()` | Zone status endpoint |
| `_build_command_payload()` | Command endpoint URLs and body format |
| `_update_devices_from_zones()` | Response field name mapping |

Contributions to improve API compatibility are welcome — please open an issue or PR on the [project page](https://github.com/gizmocuz/domoticz-gecko-spa).

## License

This plugin is licensed under the **GNU General Public License v3** (GPLv3).
See [LICENSE](LICENSE) for the full text.

This project is a derivative work of
[ha-gecko-integration](https://github.com/geckoal/ha-gecko-integration)
by Gecko Alliance, which is licensed under the **Apache License 2.0**.
In compliance with the Apache 2.0 license requirements:

- A copy of the Apache 2.0 license is included as [LICENSE-APACHE-2.0](LICENSE-APACHE-2.0).
- The upstream NOTICE file is reproduced in [NOTICE-UPSTREAM](NOTICE-UPSTREAM).
- Modified files carry prominent notices indicating changes (see source file headers).

## Acknowledgments

- [Gecko Alliance](https://www.geckoalliance.com/) for building local-first spa equipment
- [geckoal/ha-gecko-integration](https://github.com/geckoal/ha-gecko-integration) (Copyright 2025-2026 Gecko Alliance, Apache-2.0) for the cloud API approach this plugin is derived from
- [gazoodle/geckolib](https://github.com/gazoodle/geckolib) for the original reverse-engineering work on the Gecko protocol
- The Domoticz community for the Python plugin framework
