# Gecko Spa / Hot Tub Domoticz Plugin
#
# Author: Domoticz community
#
# Integrates Gecko Alliance spa/hot tub controllers with Domoticz.
# Uses the same cloud API as the Home Assistant gecko integration.
#
# Authentication: Auth0 OAuth2 (Resource Owner Password Grant)
# Real-time API:  https://api.geckowatermonitor.com
#
"""
<plugin key="GeckoSpa" name="Gecko Spa / Hot Tub" author="domoticz" version="1.0.0"
        wikilink="https://wiki.domoticz.com/Plugins"
        externallink="https://github.com/gizmocuz/domoticz-gecko-spa">
    <description>
        <h2>Gecko Spa Plugin</h2><br/>
        Controls Gecko Alliance spa and hot tub controllers via the Gecko cloud API.
        Supports temperature, heating, lights, pumps, blower and watercare modes.
        <h3>Prerequisites</h3>
        <ul style="list-style-type:square">
            <li>A Gecko Alliance account (same credentials used in the Gecko app)</li>
            <li>Spa must already be set up and visible in the Gecko app</li>
        </ul>
        <h3>Devices created per spa</h3>
        <ul style="list-style-type:square">
            <li>Water Temperature (read-only)</li>
            <li>Temperature Setpoint (read/write)</li>
            <li>Heating Status (read-only indicator)</li>
            <li>Light Zone 1 (on/off)</li>
            <li>Light Zone 2 (on/off)</li>
            <li>Pump 1 (off/low/medium/high)</li>
            <li>Pump 2 (off/low/medium/high)</li>
            <li>Blower (off/low/medium/high)</li>
            <li>Watercare Mode (Away/Standard/Savings/Super Savings/Weekender)</li>
            <li>Gateway Status (connectivity indicator)</li>
            <li>Spa Status (status text)</li>
        </ul>
        <h3>Configuration</h3>
        Enter your Gecko Alliance account email and password below.
        The plugin supports up to 12 spas on one account.
    </description>
    <params>
        <param field="Username" label="Gecko Account Email" width="250px" required="true"/>
        <param field="Password" label="Gecko Account Password" width="200px" required="true" password="true"/>
        <param field="Mode1" label="Poll interval (heartbeats)" width="75px" required="false" default="3">
            <options>
                <option label="Every heartbeat (~10s)" value="1"/>
                <option label="Every 3 heartbeats (~30s)" value="3" default="true"/>
                <option label="Every 6 heartbeats (~60s)" value="6"/>
                <option label="Every 12 heartbeats (~2min)" value="12"/>
            </options>
        </param>
        <param field="Mode6" label="Debug" width="150px">
            <options>
                <option label="None" value="0" default="true"/>
                <option label="Python Only" value="2"/>
                <option label="Basic Debugging" value="62"/>
                <option label="Basic+Messages" value="126"/>
                <option label="Connections Only" value="16"/>
                <option label="Connections+Queue" value="144"/>
                <option label="All" value="-1"/>
            </options>
        </param>
    </params>
</plugin>
"""

import Domoticz
import json
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTH0_HOST    = "gecko-prod.us.auth0.com"
AUTH0_PORT    = "443"
API_HOST      = "api.geckowatermonitor.com"
API_PORT      = "443"

AUTH0_CLIENT_ID = "L81oh6hgUsvMg40TgTGoz4lxNy8eViM0"
AUTH0_AUDIENCE  = "https://api.geckowatermonitor.com"

# Token expiry safety margin: refresh 5 minutes before actual expiry
TOKEN_REFRESH_MARGIN = 300

# Device unit offsets within each vessel block (vessel_index * VESSEL_BLOCK + offset)
VESSEL_BLOCK        = 20
UNIT_TEMPERATURE    = 1   # Water temperature (read-only)
UNIT_SETPOINT       = 2   # Temperature setpoint
UNIT_HEATING        = 3   # Heating active indicator
UNIT_LIGHT1         = 4   # Light zone 1
UNIT_LIGHT2         = 5   # Light zone 2
UNIT_PUMP1          = 6   # Pump 1
UNIT_PUMP2          = 7   # Pump 2
UNIT_BLOWER         = 8   # Blower
UNIT_WATERCARE      = 9   # Watercare mode
UNIT_GATEWAY        = 10  # Gateway connectivity
UNIT_STATUS         = 11  # Spa status text

# Watercare mode labels and their API values (index = selector level / 10)
WATERCARE_MODES = ["Away", "Standard", "Savings", "Super Savings", "Weekender"]

# Pump/blower speed labels (index = selector level / 10)
SPEED_LEVELS = ["Off", "Low", "Medium", "High"]

# Plugin state machine states
STATE_IDLE          = "IDLE"
STATE_AUTH          = "AUTH"
STATE_REFRESH_TOKEN = "REFRESH_TOKEN"
STATE_GET_USER      = "GET_USER"
STATE_GET_VESSELS   = "GET_VESSELS"
STATE_GET_STATUS    = "GET_STATUS"
STATE_COMMAND       = "COMMAND"

# ---------------------------------------------------------------------------
# Helper: build selector switch Options dict
# ---------------------------------------------------------------------------

def _selector_options(labels):
    """Return the Options dict for a Selector Switch device."""
    return {
        "LevelActions":  "|".join([""] * len(labels)),
        "LevelNames":    "|".join(labels),
        "LevelOffHidden": "false",
        "SelectorStyle": "0",
    }


# ---------------------------------------------------------------------------
# Main plugin class
# ---------------------------------------------------------------------------

class BasePlugin:

    def __init__(self):
        # Authentication tokens
        self._access_token   = None
        self._refresh_token  = None
        self._token_expiry   = 0        # Unix timestamp

        # Discovered vessels (list of dicts from API)
        self._vessels        = []

        # State machine
        self._state          = STATE_IDLE

        # Heartbeat counter for polling interval
        self._heartbeat_count = 0
        self._poll_interval   = 3

        # Pending command: (vessel_index, unit_offset, command_str, level)
        self._pending_command = None

        # Index of vessel currently being polled
        self._poll_vessel_index = 0

        # Request queue for sequential multi-step operations
        # Each entry is a callable that initiates the next HTTP step
        self._request_queue = []

        # Domoticz.Connection objects
        self._auth_conn = None
        self._api_conn  = None

        # Flag for auth grant type fallback
        self._use_standard_grant = False

    # -----------------------------------------------------------------------
    # Lifecycle callbacks
    # -----------------------------------------------------------------------

    def onStart(self):
        debug_mode = Parameters.get("Mode6", "0")
        if debug_mode != "0":
            Domoticz.Debugging(int(debug_mode))
            DumpConfigToLog()

        Domoticz.Heartbeat(10)

        try:
            self._poll_interval = int(Parameters.get("Mode1", "3"))
        except ValueError:
            self._poll_interval = 3

        Domoticz.Log("Gecko Spa plugin starting. Poll interval: every {} heartbeats (~{}s).".format(
            self._poll_interval, self._poll_interval * 10))

        # Kick off authentication immediately
        self._start_auth()

    def onStop(self):
        Domoticz.Log("Gecko Spa plugin stopping.")
        self._disconnect_all()

    def onConnect(self, Connection, Status, Description):
        Domoticz.Debug("onConnect: {} Status={} {}".format(Connection.Name, Status, Description))
        if Status != 0:
            Domoticz.Error("Connection failed to {}: {} ({})".format(
                Connection.Name, Description, Status))
            self._handle_connection_error()
            return

        # Dispatch to the appropriate handler based on current state
        if self._state == STATE_AUTH or self._state == STATE_REFRESH_TOKEN:
            self._send_auth_request(Connection)
        elif self._state == STATE_GET_USER:
            self._send_get_user(Connection)
        elif self._state == STATE_GET_VESSELS:
            self._send_get_vessels(Connection)
        elif self._state == STATE_GET_STATUS:
            self._send_get_status(Connection)
        elif self._state == STATE_COMMAND:
            self._send_command_request(Connection)
        else:
            Domoticz.Debug("onConnect in unexpected state: {}".format(self._state))

    def onMessage(self, Connection, Data):
        status_code = int(Data.get("Status", 0))
        Domoticz.Debug("onMessage: {} HTTP {}".format(Connection.Name, status_code))

        try:
            raw = Data.get("Data", b"")
            if isinstance(raw, (bytes, bytearray)):
                body_str = raw.decode("utf-8", errors="replace")
            else:
                body_str = str(raw)
        except Exception as e:
            Domoticz.Error("Failed to decode response body: {}".format(e))
            body_str = ""

        # Parse JSON body (best-effort)
        body = {}
        if body_str.strip():
            try:
                body = json.loads(body_str)
            except json.JSONDecodeError:
                Domoticz.Debug("Non-JSON response body: {}".format(body_str[:200]))

        if status_code in (200, 201, 204):
            self._handle_success(Connection, status_code, body, body_str)
        elif status_code == 401:
            Domoticz.Log("HTTP 401 Unauthorised — will re-authenticate.")
            self._access_token = None
            self._token_expiry  = 0
            self._state = STATE_IDLE
            self._start_auth()
        elif status_code == 429:
            Domoticz.Log("HTTP 429 Too Many Requests — backing off.")
            self._state = STATE_IDLE
        else:
            Domoticz.Error("HTTP {} from {}: {}".format(status_code, Connection.Name, body_str[:300]))
            self._state = STATE_IDLE

    def onCommand(self, Unit, Command, Level, Hue):
        Domoticz.Debug("onCommand Unit={} Command='{}' Level={}".format(Unit, Command, Level))

        vessel_index  = (Unit - 1) // VESSEL_BLOCK
        unit_offset   = ((Unit - 1) % VESSEL_BLOCK) + 1

        if vessel_index >= len(self._vessels):
            Domoticz.Error("Command for unknown vessel index {}".format(vessel_index))
            return

        self._pending_command = (vessel_index, unit_offset, str(Command).strip(), int(Level))
        self._execute_command()

    def onDisconnect(self, Connection):
        Domoticz.Debug("onDisconnect: {}".format(Connection.Name))
        # Clean up connection object references so we can reconnect
        if self._auth_conn and Connection.Name == self._auth_conn.Name:
            self._auth_conn = None
        if self._api_conn and Connection.Name == self._api_conn.Name:
            self._api_conn  = None

    def onHeartbeat(self):
        self._heartbeat_count += 1
        Domoticz.Debug("onHeartbeat #{}, state={}".format(self._heartbeat_count, self._state))

        # Don't interrupt an in-progress operation
        if self._state != STATE_IDLE:
            return

        # Check if token needs refreshing
        if self._refresh_token and time.time() >= (self._token_expiry - TOKEN_REFRESH_MARGIN):
            Domoticz.Log("Access token expiring soon — refreshing.")
            self._start_token_refresh()
            return

        # Poll on schedule
        if self._heartbeat_count % self._poll_interval == 0:
            if self._access_token:
                self._start_status_poll()
            else:
                self._start_auth()

    # -----------------------------------------------------------------------
    # State machine: initiators
    # -----------------------------------------------------------------------

    def _start_auth(self):
        """Begin Auth0 authentication (Resource Owner Password Grant)."""
        Domoticz.Log("Authenticating with Gecko cloud...")
        self._state = STATE_AUTH
        self._safe_disconnect(self._auth_conn)
        self._auth_conn = self._make_auth_conn()
        self._auth_conn.Connect()

    def _start_token_refresh(self):
        """Refresh the access token using the stored refresh_token."""
        Domoticz.Log("Refreshing access token...")
        self._state = STATE_REFRESH_TOKEN
        self._safe_disconnect(self._auth_conn)
        self._auth_conn = self._make_auth_conn()
        self._auth_conn.Connect()

    def _start_get_user(self):
        """After successful auth, fetch the user's vessel list."""
        self._state = STATE_GET_USER
        self._safe_disconnect(self._api_conn)
        self._api_conn = self._make_api_conn()
        self._api_conn.Connect()

    def _start_get_vessels(self):
        """Fetch detailed info for each vessel."""
        self._state = STATE_GET_VESSELS
        self._safe_disconnect(self._api_conn)
        self._api_conn = self._make_api_conn()
        self._api_conn.Connect()

    def _start_status_poll(self):
        """Poll the current status of the next vessel in rotation."""
        if not self._vessels:
            self._start_get_user()
            return
        self._poll_vessel_index = 0
        self._state = STATE_GET_STATUS
        self._safe_disconnect(self._api_conn)
        self._api_conn = self._make_api_conn()
        self._api_conn.Connect()

    def _execute_command(self):
        """Send a pending command to the API."""
        if not self._pending_command:
            return
        if not self._access_token:
            Domoticz.Log("No access token — authenticating before sending command.")
            self._start_auth()
            return
        self._state = STATE_COMMAND
        self._safe_disconnect(self._api_conn)
        self._api_conn = self._make_api_conn()
        self._api_conn.Connect()

    # -----------------------------------------------------------------------
    # State machine: senders (called from onConnect)
    # -----------------------------------------------------------------------

    def _send_auth_request(self, Connection):
        if self._state == STATE_REFRESH_TOKEN and self._refresh_token:
            payload = {
                "grant_type":    "refresh_token",
                "client_id":     AUTH0_CLIENT_ID,
                "refresh_token": self._refresh_token,
            }
            Domoticz.Debug("Sending token refresh request.")
        elif self._use_standard_grant:
            # Fallback: standard password grant (realm grant was rejected)
            payload = {
                "grant_type": "password",
                "client_id":  AUTH0_CLIENT_ID,
                "username":   Parameters["Username"],
                "password":   Parameters["Password"],
                "scope":      "openid profile email offline_access",
                "audience":   AUTH0_AUDIENCE,
            }
            self._use_standard_grant = False
            Domoticz.Debug("Sending standard password grant (fallback).")
        else:
            # Try realm-specific password grant first
            payload = {
                "grant_type": "http://auth0.com/oauth/grant-type/password-realm",
                "client_id":  AUTH0_CLIENT_ID,
                "username":   Parameters["Username"],
                "password":   Parameters["Password"],
                "realm":      "Username-Password-Authentication",
                "scope":      "openid profile email offline_access",
                "audience":   AUTH0_AUDIENCE,
            }
            Domoticz.Debug("Sending password-realm auth request.")

        body = json.dumps(payload).encode("utf-8")
        Connection.Send({
            "Verb":    "POST",
            "URL":     "/oauth/token",
            "Headers": {
                "Host":           AUTH0_HOST,
                "Content-Type":   "application/json",
                "Accept":         "application/json",
                "Content-Length": str(len(body)),
                "User-Agent":     "Domoticz GeckoSpa/1.0",
                "Connection":     "close",
            },
            "Data": body,
        })

    def _send_get_user(self, Connection):
        Domoticz.Debug("GET /api/v1/user/vessels")
        Connection.Send({
            "Verb":    "GET",
            "URL":     "/api/v1/user/vessels",
            "Headers": self._api_headers(API_HOST),
        })

    def _send_get_vessels(self, Connection):
        """Fetch detail for the first vessel in self._vessels that lacks full data."""
        # Find first vessel without details (marked by missing 'zones' key)
        idx = next((i for i, v in enumerate(self._vessels) if "zones" not in v), None)
        if idx is None:
            Domoticz.Debug("All vessel details fetched.")
            self._ensure_devices_created()
            self._state = STATE_IDLE
            return
        vessel_id = self._vessels[idx].get("id") or self._vessels[idx].get("vessel_id", "")
        Domoticz.Debug("GET vessel detail for id={}".format(vessel_id))
        Connection.Send({
            "Verb":    "GET",
            "URL":     "/api/v1/vessels/{}".format(vessel_id),
            "Headers": self._api_headers(API_HOST),
        })

    def _send_get_status(self, Connection):
        if self._poll_vessel_index >= len(self._vessels):
            self._state = STATE_IDLE
            return
        vessel = self._vessels[self._poll_vessel_index]
        vessel_id = vessel.get("id") or vessel.get("vessel_id", "")
        Domoticz.Debug("GET status for vessel[{}] id={}".format(self._poll_vessel_index, vessel_id))
        Connection.Send({
            "Verb":    "GET",
            "URL":     "/api/v1/vessels/{}/zones".format(vessel_id),
            "Headers": self._api_headers(API_HOST),
        })

    def _send_command_request(self, Connection):
        if not self._pending_command:
            self._state = STATE_IDLE
            return

        vessel_index, unit_offset, command, level = self._pending_command
        if vessel_index >= len(self._vessels):
            Domoticz.Error("Vessel index {} out of range ({} vessels known)".format(
                vessel_index, len(self._vessels)))
            self._pending_command = None
            self._state = STATE_IDLE
            return
        vessel = self._vessels[vessel_index]
        vessel_id = vessel.get("id") or vessel.get("vessel_id", "")

        url, body = self._build_command_payload(vessel, vessel_id, unit_offset, command, level)
        if url is None:
            Domoticz.Error("Could not build command payload for unit_offset={}".format(unit_offset))
            self._pending_command = None
            self._state = STATE_IDLE
            return

        Domoticz.Log("Command: {} {}".format("POST" if body else "GET", url))
        try:
            encoded = json.dumps(body).encode("utf-8") if body else b""
        except (TypeError, ValueError) as e:
            Domoticz.Error("Failed to serialize command payload: {}".format(e))
            self._pending_command = None
            self._state = STATE_IDLE
            return
        send_dict = {
            "Verb":    "POST",
            "URL":     url,
            "Headers": self._api_headers(API_HOST, len(encoded)),
        }
        if encoded:
            send_dict["Data"] = encoded
        Connection.Send(send_dict)

    # -----------------------------------------------------------------------
    # State machine: response handlers (called from onMessage)
    # -----------------------------------------------------------------------

    def _handle_success(self, Connection, status_code, body, body_str):
        state = self._state

        if state in (STATE_AUTH, STATE_REFRESH_TOKEN):
            self._handle_auth_response(body)

        elif state == STATE_GET_USER:
            self._handle_get_user_response(body)

        elif state == STATE_GET_VESSELS:
            self._handle_get_vessels_response(body)

        elif state == STATE_GET_STATUS:
            self._handle_get_status_response(body)

        elif state == STATE_COMMAND:
            self._handle_command_response(body)

        else:
            Domoticz.Debug("Unhandled success in state={}".format(state))
            self._state = STATE_IDLE

    def _handle_auth_response(self, body):
        if "access_token" not in body:
            # The realm grant might have been rejected — try standard password grant
            if self._state == STATE_AUTH and body.get("error") == "unsupported_grant_type":
                Domoticz.Log("Realm grant not supported, falling back to standard password grant.")
                self._state = STATE_AUTH
                self._auth_conn = self._make_auth_conn()
                # Monkey-patch to use standard grant on next connect
                self._use_standard_grant = True
                self._auth_conn.Connect()
                return
            Domoticz.Error("Auth failed: {}".format(body.get("error_description", body)))
            self._state = STATE_IDLE
            return

        self._access_token  = body["access_token"]
        self._refresh_token = body.get("refresh_token", self._refresh_token)
        expires_in          = int(body.get("expires_in", 3600))
        self._token_expiry  = time.time() + expires_in

        Domoticz.Log("Authentication successful. Token valid for {}s.".format(expires_in))
        self._state = STATE_IDLE

        # After fresh auth, start vessel discovery; after refresh, go back to polling
        if not self._vessels:
            self._start_get_user()
        else:
            # Execute any queued command if present
            if self._pending_command:
                self._execute_command()

    def _handle_get_user_response(self, body):
        """
        The /api/v1/user/vessels response is expected to be a list of vessel
        summary objects, or a dict with a 'vessels' key. Both shapes are handled.
        """
        vessels_raw = []
        if isinstance(body, list):
            vessels_raw = body
        elif isinstance(body, dict):
            # Try common wrapper keys
            for key in ("vessels", "data", "items", "results"):
                if key in body and isinstance(body[key], list):
                    vessels_raw = body[key]
                    break
            if not vessels_raw and body:
                # Possibly the body itself is one vessel
                vessels_raw = [body]

        if not vessels_raw:
            Domoticz.Error("No vessels found in user profile response: {}".format(body))
            self._state = STATE_IDLE
            return

        Domoticz.Log("Found {} vessel(s) on account.".format(len(vessels_raw)))

        # Preserve any existing vessel data while updating summary fields
        existing_ids = {v.get("id") or v.get("vessel_id"): v for v in self._vessels}
        self._vessels = []
        for v in vessels_raw[:12]:  # max 12 vessels
            vid = v.get("id") or v.get("vessel_id", "")
            if vid in existing_ids:
                merged = dict(existing_ids[vid])
                merged.update(v)
                self._vessels.append(merged)
            else:
                self._vessels.append(v)

        # Fetch detailed data for each vessel
        self._state = STATE_GET_VESSELS
        self._api_conn = self._make_api_conn()
        self._api_conn.Connect()

    def _handle_get_vessels_response(self, body):
        """Store detailed vessel info (zones, name, etc.)."""
        # Find the vessel entry that's missing zones
        idx = next((i for i, v in enumerate(self._vessels) if "zones" not in v), None)
        if idx is None:
            self._ensure_devices_created()
            self._state = STATE_IDLE
            return

        if isinstance(body, dict):
            # Merge all returned fields into our vessel record
            self._vessels[idx].update(body)
            # Normalise zones: accept list or dict with 'zones' key
            zones_raw = body.get("zones", body.get("data", {}).get("zones", []))
            if isinstance(zones_raw, list):
                self._vessels[idx]["zones"] = zones_raw
            elif isinstance(zones_raw, dict):
                self._vessels[idx]["zones"] = list(zones_raw.values())
            else:
                # Mark as fetched even if empty
                self._vessels[idx].setdefault("zones", [])

        Domoticz.Debug("Vessel[{}] detail stored. zones={}".format(
            idx, len(self._vessels[idx].get("zones", []))))

        # Check if more vessels need fetching
        next_idx = next((i for i, v in enumerate(self._vessels) if "zones" not in v), None)
        if next_idx is not None:
            # Still more to fetch — reconnect and send next request
            self._api_conn = self._make_api_conn()
            self._api_conn.Connect()
        else:
            self._ensure_devices_created()
            self._state = STATE_IDLE

    def _handle_get_status_response(self, body):
        """Parse zone status and update Domoticz devices."""
        vessel_index = self._poll_vessel_index
        if vessel_index >= len(self._vessels):
            self._state = STATE_IDLE
            return

        vessel = self._vessels[vessel_index]

        # Normalise response into a flat zones list
        zones = []
        if isinstance(body, list):
            zones = body
        elif isinstance(body, dict):
            for key in ("zones", "data", "items"):
                if key in body and isinstance(body[key], list):
                    zones = body[key]
                    break
            if not zones:
                # Body might be a single zone or contain top-level spa fields
                zones = [body]

        if zones:
            self._update_devices_from_zones(vessel_index, vessel, zones)

        # Also fetch connectivity status while we're here
        # (fire-and-forget style: connection is still open, send another request)
        vessel_id = vessel.get("id") or vessel.get("vessel_id", "")
        # Note: we piggy-back the connectivity check onto the same connection
        # by sending a second GET before the connection closes.
        # If the connection closed already, this will silently fail — that's OK.
        try:
            if self._api_conn and self._api_conn.Connected():
                self._api_conn.Send({
                    "Verb":    "GET",
                    "URL":     "/api/v1/vessels/{}/connectivity".format(vessel_id),
                    "Headers": self._api_headers(API_HOST),
                })
                # We'll receive another onMessage; treat it as a status update too
                self._state = STATE_GET_STATUS
                return
        except Exception:
            pass

        # Move to next vessel or go idle
        self._poll_vessel_index += 1
        if self._poll_vessel_index < len(self._vessels):
            self._api_conn = self._make_api_conn()
            self._api_conn.Connect()
        else:
            self._state = STATE_IDLE

    def _handle_command_response(self, body):
        Domoticz.Log("Command acknowledged by API: {}".format(body))
        self._pending_command = None
        self._state = STATE_IDLE

        # Immediately re-poll to get updated state
        self._start_status_poll()

    # -----------------------------------------------------------------------
    # Device management
    # -----------------------------------------------------------------------

    def _unit_for(self, vessel_index, offset):
        """Return the Domoticz Unit number for a given vessel + offset."""
        return vessel_index * VESSEL_BLOCK + offset

    def _ensure_devices_created(self):
        """Create Domoticz devices for all known vessels if they don't exist yet."""
        for idx, vessel in enumerate(self._vessels):
            spa_name = vessel.get("name") or vessel.get("vessel_name") or "Spa {}".format(idx + 1)
            self._create_vessel_devices(idx, spa_name)

    def _create_vessel_devices(self, vessel_index, spa_name):
        """Create the full set of devices for one vessel (idempotent)."""

        def _unit(offset):
            return self._unit_for(vessel_index, offset)

        def _exists(offset):
            return _unit(offset) in Devices

        # Water temperature
        if not _exists(UNIT_TEMPERATURE):
            Domoticz.Device(
                Name="{} - Water Temperature".format(spa_name),
                Unit=_unit(UNIT_TEMPERATURE),
                TypeName="Temperature",
            ).Create()
            Domoticz.Log("Created device: {} - Water Temperature (unit {})".format(spa_name, _unit(UNIT_TEMPERATURE)))

        # Setpoint
        if not _exists(UNIT_SETPOINT):
            Domoticz.Device(
                Name="{} - Temperature Setpoint".format(spa_name),
                Unit=_unit(UNIT_SETPOINT),
                Type=242,
                SubType=1,
            ).Create()
            Domoticz.Log("Created device: {} - Temperature Setpoint (unit {})".format(spa_name, _unit(UNIT_SETPOINT)))

        # Heating status
        if not _exists(UNIT_HEATING):
            Domoticz.Device(
                Name="{} - Heating".format(spa_name),
                Unit=_unit(UNIT_HEATING),
                TypeName="Switch",
            ).Create()
            Domoticz.Log("Created device: {} - Heating (unit {})".format(spa_name, _unit(UNIT_HEATING)))

        # Light zone 1
        if not _exists(UNIT_LIGHT1):
            Domoticz.Device(
                Name="{} - Light Zone 1".format(spa_name),
                Unit=_unit(UNIT_LIGHT1),
                TypeName="Switch",
            ).Create()
            Domoticz.Log("Created device: {} - Light Zone 1 (unit {})".format(spa_name, _unit(UNIT_LIGHT1)))

        # Light zone 2
        if not _exists(UNIT_LIGHT2):
            Domoticz.Device(
                Name="{} - Light Zone 2".format(spa_name),
                Unit=_unit(UNIT_LIGHT2),
                TypeName="Switch",
            ).Create()
            Domoticz.Log("Created device: {} - Light Zone 2 (unit {})".format(spa_name, _unit(UNIT_LIGHT2)))

        # Pump 1
        if not _exists(UNIT_PUMP1):
            Domoticz.Device(
                Name="{} - Pump 1".format(spa_name),
                Unit=_unit(UNIT_PUMP1),
                TypeName="Selector Switch",
                Options=_selector_options(SPEED_LEVELS),
            ).Create()
            Domoticz.Log("Created device: {} - Pump 1 (unit {})".format(spa_name, _unit(UNIT_PUMP1)))

        # Pump 2
        if not _exists(UNIT_PUMP2):
            Domoticz.Device(
                Name="{} - Pump 2".format(spa_name),
                Unit=_unit(UNIT_PUMP2),
                TypeName="Selector Switch",
                Options=_selector_options(SPEED_LEVELS),
            ).Create()
            Domoticz.Log("Created device: {} - Pump 2 (unit {})".format(spa_name, _unit(UNIT_PUMP2)))

        # Blower
        if not _exists(UNIT_BLOWER):
            Domoticz.Device(
                Name="{} - Blower".format(spa_name),
                Unit=_unit(UNIT_BLOWER),
                TypeName="Selector Switch",
                Options=_selector_options(SPEED_LEVELS),
            ).Create()
            Domoticz.Log("Created device: {} - Blower (unit {})".format(spa_name, _unit(UNIT_BLOWER)))

        # Watercare mode
        if not _exists(UNIT_WATERCARE):
            Domoticz.Device(
                Name="{} - Watercare Mode".format(spa_name),
                Unit=_unit(UNIT_WATERCARE),
                TypeName="Selector Switch",
                Options=_selector_options(WATERCARE_MODES),
            ).Create()
            Domoticz.Log("Created device: {} - Watercare Mode (unit {})".format(spa_name, _unit(UNIT_WATERCARE)))

        # Gateway status
        if not _exists(UNIT_GATEWAY):
            Domoticz.Device(
                Name="{} - Gateway".format(spa_name),
                Unit=_unit(UNIT_GATEWAY),
                TypeName="Switch",
            ).Create()
            Domoticz.Log("Created device: {} - Gateway (unit {})".format(spa_name, _unit(UNIT_GATEWAY)))

        # Spa status text
        if not _exists(UNIT_STATUS):
            Domoticz.Device(
                Name="{} - Status".format(spa_name),
                Unit=_unit(UNIT_STATUS),
                TypeName="Text",
            ).Create()
            Domoticz.Log("Created device: {} - Status (unit {})".format(spa_name, _unit(UNIT_STATUS)))

    def _update_devices_from_zones(self, vessel_index, vessel, zones):
        """
        Map zone data from the API to Domoticz device updates.

        The Gecko API returns zones as a list of objects.  Each zone has at least:
          - type / zone_type: "heater", "light", "pump", "blower", "watercare", etc.
          - state / value:    current value
          - id / zone_id:     unique identifier

        Because the exact field names are inferred (the real API may differ),
        we try several common alternatives and log what we find at debug level.
        """

        def _field(obj, *names, default=None):
            for n in names:
                if n in obj:
                    return obj[n]
            return default

        # We also accept top-level fields on the response itself (body may be the spa state)
        flat = {}
        if len(zones) == 1 and isinstance(zones[0], dict):
            flat = zones[0]

        # --- Current water temperature ---
        temp_current = _field(flat, "current_temperature", "temperature", "water_temp",
                              "displayedSetpoint", default=None)
        if temp_current is None:
            # Look in zones for a heater/temperature zone
            for z in zones:
                zt = str(_field(z, "type", "zone_type", "category", default="")).lower()
                if "heat" in zt or "temp" in zt:
                    temp_current = _field(z, "current_temperature", "temperature",
                                          "current_value", "value", default=None)
                    if temp_current is not None:
                        break

        if temp_current is not None:
            try:
                UpdateDevice(self._unit_for(vessel_index, UNIT_TEMPERATURE),
                             0, "{:.1f}".format(float(temp_current)))
            except (ValueError, TypeError):
                Domoticz.Debug("Invalid temperature value: {}".format(temp_current))

        # --- Temperature setpoint ---
        temp_set = _field(flat, "target_temperature", "setpoint", "set_temperature",
                          "displayedSetpoint", default=None)
        if temp_set is None:
            for z in zones:
                zt = str(_field(z, "type", "zone_type", "category", default="")).lower()
                if "heat" in zt or "temp" in zt or "setpoint" in zt:
                    temp_set = _field(z, "target_temperature", "setpoint", "set_point",
                                      "value", default=None)
                    if temp_set is not None:
                        break

        if temp_set is not None:
            try:
                UpdateDevice(self._unit_for(vessel_index, UNIT_SETPOINT),
                             0, "{:.1f}".format(float(temp_set)))
            except (ValueError, TypeError):
                Domoticz.Debug("Invalid setpoint value: {}".format(temp_set))

        # --- Heating active ---
        heating = _field(flat, "is_heating", "heating", "heater_active", default=None)
        if heating is None:
            for z in zones:
                zt = str(_field(z, "type", "zone_type", "category", default="")).lower()
                if "heat" in zt:
                    heating = _field(z, "is_active", "active", "state", "value", default=None)
                    break

        if heating is not None:
            nval = 1 if _truthy(heating) else 0
            UpdateDevice(self._unit_for(vessel_index, UNIT_HEATING),
                         nval, "On" if nval else "Off")

        # --- Lights ---
        light_zones = [z for z in zones
                       if "light" in str(_field(z, "type", "zone_type", "category", default="")).lower()]
        for i, lz in enumerate(light_zones[:2]):
            unit_offset = UNIT_LIGHT1 if i == 0 else UNIT_LIGHT2
            active = _field(lz, "is_active", "active", "state", "value", default=False)
            nval = 1 if _truthy(active) else 0
            UpdateDevice(self._unit_for(vessel_index, unit_offset),
                         nval, "On" if nval else "Off")

        # --- Pumps ---
        pump_zones = [z for z in zones
                      if "pump" in str(_field(z, "type", "zone_type", "category", default="")).lower()
                      and "blower" not in str(_field(z, "type", "zone_type", "category", default="")).lower()]
        for i, pz in enumerate(pump_zones[:2]):
            unit_offset = UNIT_PUMP1 if i == 0 else UNIT_PUMP2
            speed = _field(pz, "speed", "state", "value", default="off")
            level = _speed_to_level(speed)
            UpdateDevice(self._unit_for(vessel_index, unit_offset), level, str(level))

        # --- Blower ---
        blower_zones = [z for z in zones
                        if "blow" in str(_field(z, "type", "zone_type", "category", default="")).lower()
                        or "spa_blower" in str(_field(z, "type", "zone_type", "category", default="")).lower()]
        if blower_zones:
            bz = blower_zones[0]
            speed = _field(bz, "speed", "state", "value", default="off")
            level = _speed_to_level(speed)
            UpdateDevice(self._unit_for(vessel_index, UNIT_BLOWER), level, str(level))

        # --- Watercare mode ---
        wc_mode = _field(flat, "watercare_mode", "water_care", "operation_mode", default=None)
        if wc_mode is None:
            for z in zones:
                zt = str(_field(z, "type", "zone_type", "category", default="")).lower()
                if "watercare" in zt or "water_care" in zt or "operation" in zt:
                    wc_mode = _field(z, "mode", "state", "value", default=None)
                    break

        if wc_mode is not None:
            wc_level = _watercare_to_level(wc_mode)
            UpdateDevice(self._unit_for(vessel_index, UNIT_WATERCARE), wc_level, str(wc_level))

        # --- Gateway / connectivity (may arrive as a separate response) ---
        connected = _field(flat, "connected", "is_connected", "gateway_connected",
                           "connectivity", default=None)
        if connected is not None:
            nval = 1 if _truthy(connected) else 0
            UpdateDevice(self._unit_for(vessel_index, UNIT_GATEWAY),
                         nval, "On" if nval else "Off")

        # --- Status text ---
        status_text = _field(flat, "spa_state", "status", "state", "spa_status", default=None)
        if status_text is not None:
            UpdateDevice(self._unit_for(vessel_index, UNIT_STATUS), 0, str(status_text))

        Domoticz.Debug("Vessel[{}] devices updated from {} zone(s).".format(
            vessel_index, len(zones)))

    # -----------------------------------------------------------------------
    # Command builder
    # -----------------------------------------------------------------------

    def _build_command_payload(self, vessel, vessel_id, unit_offset, command, level):
        """
        Return (url, body_dict) for the command, or (None, None) on error.

        Because the exact API shape is inferred, the endpoints are constructed
        from patterns observed in the HA integration source.  If the real API
        uses different paths, this is the place to adapt.
        """
        base = "/api/v1/vessels/{}".format(vessel_id)
        zones = vessel.get("zones", [])

        def _field(obj, *names, default=None):
            for n in names:
                if n in obj:
                    return obj[n]
            return default

        # --- Setpoint ---
        if unit_offset == UNIT_SETPOINT:
            try:
                temp = float(level) if level else 0.0
                # Setpoint is passed as level directly from Domoticz thermostat
                # For Type=242 the Level IS the temperature value in °C
                return "{}/zones/setpoint".format(base), {"temperature": temp}
            except (ValueError, TypeError):
                Domoticz.Error("Invalid setpoint level: {}".format(level))
                return None, None

        # --- Lights ---
        if unit_offset in (UNIT_LIGHT1, UNIT_LIGHT2):
            light_idx = 0 if unit_offset == UNIT_LIGHT1 else 1
            light_zones = [z for z in zones
                           if "light" in str(_field(z, "type", "zone_type", "category", default="")).lower()]
            if light_idx < len(light_zones):
                zone_id = _field(light_zones[light_idx], "id", "zone_id", default="light_{}".format(light_idx + 1))
            else:
                zone_id = "light_{}".format(light_idx + 1)

            action = "activate" if command.lower() == "on" else "deactivate"
            return "{}/zones/{}/{}".format(base, zone_id, action), {}

        # --- Pumps ---
        if unit_offset in (UNIT_PUMP1, UNIT_PUMP2):
            pump_idx = 0 if unit_offset == UNIT_PUMP1 else 1
            pump_zones = [z for z in zones
                          if "pump" in str(_field(z, "type", "zone_type", "category", default="")).lower()
                          and "blower" not in str(_field(z, "type", "zone_type", "category", default="")).lower()]
            if pump_idx < len(pump_zones):
                zone_id = _field(pump_zones[pump_idx], "id", "zone_id", default="pump_{}".format(pump_idx + 1))
            else:
                zone_id = "pump_{}".format(pump_idx + 1)

            speed_name = _level_to_speed(level)
            if speed_name == "off":
                return "{}/zones/{}/deactivate".format(base, zone_id), {}
            return "{}/zones/{}/set-speed".format(base, zone_id), {"speed": speed_name}

        # --- Blower ---
        if unit_offset == UNIT_BLOWER:
            blower_zones = [z for z in zones
                            if "blow" in str(_field(z, "type", "zone_type", "category", default="")).lower()]
            zone_id = _field(blower_zones[0], "id", "zone_id", default="blower") if blower_zones else "blower"
            speed_name = _level_to_speed(level)
            if speed_name == "off":
                return "{}/zones/{}/deactivate".format(base, zone_id), {}
            return "{}/zones/{}/set-speed".format(base, zone_id), {"speed": speed_name}

        # --- Watercare mode ---
        if unit_offset == UNIT_WATERCARE:
            mode_name = _level_to_watercare(level)
            return "{}/operation-mode".format(base), {"mode": mode_name}

        Domoticz.Error("No command handler for unit_offset={}".format(unit_offset))
        return None, None

    # -----------------------------------------------------------------------
    # Connection factories
    # -----------------------------------------------------------------------

    def _make_auth_conn(self):
        return Domoticz.Connection(
            Name="GeckoAuth",
            Transport="TCP/IP",
            Protocol="HTTPS",
            Address=AUTH0_HOST,
            Port=AUTH0_PORT,
        )

    def _make_api_conn(self):
        return Domoticz.Connection(
            Name="GeckoAPI",
            Transport="TCP/IP",
            Protocol="HTTPS",
            Address=API_HOST,
            Port=API_PORT,
        )

    def _api_headers(self, host, content_length=0):
        headers = {
            "Host":          host,
            "Accept":        "application/json",
            "Content-Type":  "application/json",
            "Authorization": "Bearer {}".format(self._access_token or ""),
            "User-Agent":    "Domoticz GeckoSpa/1.0",
            "Connection":    "keep-alive",
        }
        if content_length:
            headers["Content-Length"] = str(content_length)
        return headers

    # -----------------------------------------------------------------------
    # Error handling
    # -----------------------------------------------------------------------

    def _safe_disconnect(self, conn):
        """Disconnect a connection if it's still alive."""
        if conn:
            try:
                if conn.Connected() or conn.Connecting():
                    conn.Disconnect()
            except Exception:
                pass

    def _handle_connection_error(self):
        """Reset state after a connection failure."""
        self._state = STATE_IDLE
        self._auth_conn = None
        self._api_conn  = None

    def _disconnect_all(self):
        """Gracefully disconnect all open connections."""
        for conn in (self._auth_conn, self._api_conn):
            if conn and conn.Connected():
                try:
                    conn.Disconnect()
                except Exception:
                    pass
        self._auth_conn = None
        self._api_conn  = None


# ---------------------------------------------------------------------------
# Pure utility functions (module-level, no Domoticz dependency)
# ---------------------------------------------------------------------------

def _truthy(value):
    """Return True for any truthy representation of an 'on' state."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.lower() in ("true", "on", "1", "yes", "active", "connected")
    return bool(value)


def _speed_to_level(speed):
    """Convert an API speed string/int to a Domoticz selector level (0, 10, 20, 30)."""
    if isinstance(speed, int):
        # If the API returns an integer index
        return min(speed, 3) * 10
    s = str(speed).lower()
    mapping = {"off": 0, "0": 0, "low": 10, "1": 10, "medium": 20, "2": 20, "high": 30, "3": 30}
    return mapping.get(s, 0)


def _level_to_speed(level):
    """Convert Domoticz selector level to API speed string."""
    mapping = {0: "off", 10: "low", 20: "medium", 30: "high"}
    return mapping.get(int(level), "off")


def _watercare_to_level(mode):
    """Convert API watercare mode string/int to Domoticz selector level."""
    if isinstance(mode, int):
        return min(mode, len(WATERCARE_MODES) - 1) * 10
    s = str(mode).lower()
    for i, label in enumerate(WATERCARE_MODES):
        if label.lower() == s or str(i) == s:
            return i * 10
    return 0


def _level_to_watercare(level):
    """Convert Domoticz selector level to API watercare mode string."""
    idx = int(level) // 10
    if 0 <= idx < len(WATERCARE_MODES):
        return WATERCARE_MODES[idx]
    return WATERCARE_MODES[0]


# ---------------------------------------------------------------------------
# Generic Domoticz helper functions
# ---------------------------------------------------------------------------

def UpdateDevice(Unit, nValue, sValue):
    """Update a Domoticz device only when the value actually changes."""
    if Unit not in Devices:
        return
    if Devices[Unit].nValue != nValue or Devices[Unit].sValue != str(sValue):
        Devices[Unit].Update(nValue=nValue, sValue=str(sValue))
        Domoticz.Debug("UpdateDevice {} nValue={} sValue='{}'".format(Unit, nValue, sValue))


def DumpConfigToLog():
    for key in Parameters:
        if Parameters[key] != "":
            # Mask password field
            val = "***" if key == "Password" else Parameters[key]
            Domoticz.Debug("Parameter '{}': '{}'".format(key, val))
    Domoticz.Debug("Device count: {}".format(len(Devices)))
    for unit in Devices:
        Domoticz.Debug("  Unit {:3d}: nValue={} sValue='{}' Name='{}'".format(
            unit,
            Devices[unit].nValue,
            Devices[unit].sValue,
            Devices[unit].Name,
        ))


# ---------------------------------------------------------------------------
# Module-level Domoticz callback functions  (mandatory)
# ---------------------------------------------------------------------------

global _plugin
_plugin = BasePlugin()


def onStart():
    global _plugin
    _plugin.onStart()


def onStop():
    global _plugin
    _plugin.onStop()


def onConnect(Connection, Status, Description):
    global _plugin
    _plugin.onConnect(Connection, Status, Description)


def onMessage(Connection, Data):
    global _plugin
    _plugin.onMessage(Connection, Data)


def onCommand(Unit, Command, Level, Hue):
    global _plugin
    _plugin.onCommand(Unit, Command, Level, Hue)


def onNotification(Name, Subject, Text, Status, Priority, Sound, ImageFile):
    Domoticz.Debug("onNotification: Name='{}' Subject='{}' Text='{}'".format(Name, Subject, Text))


def onDisconnect(Connection):
    global _plugin
    _plugin.onDisconnect(Connection)


def onHeartbeat():
    global _plugin
    _plugin.onHeartbeat()
