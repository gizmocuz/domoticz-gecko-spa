"""
<plugin key="GeckoSpa" name="Gecko Alliance Spa" author="gizmocuz" version="1.0.0"
    wikilink="https://github.com/gizmocuz/domoticz-gecko-spa"
    externallink="https://github.com/gazoodle/geckolib">
    <description>
        <h2>Gecko Alliance Spa Plugin</h2>
        <p>Controls Gecko Alliance spa, hot tub, and pool equipment via the local network
        using the geckolib library.</p>
        <h3>Prerequisites</h3>
        <p>Install geckolib: <b>pip3 install geckolib</b></p>
        <h3>Client UUID</h3>
        <p>Generate a unique UUID for this plugin instance:<br/>
        <b>python3 -c "import uuid; print(uuid.uuid4())"</b></p>
    </description>
    <params>
        <param field="Address" label="Spa IP Address (leave blank for auto-discovery)"
            width="200px" required="false" default=""/>
        <param field="Username" label="Spa Name or Identifier (leave blank for first found)"
            width="200px" required="false" default=""/>
        <param field="Password" label="Client UUID (see description)"
            width="300px" required="true" default=""/>
        <param field="Mode6" label="Debug" width="75px">
            <options>
                <option label="True" value="Debug"/>
                <option label="False" value="Normal" default="true"/>
            </options>
        </param>
    </params>
</plugin>
"""

import threading
import time

import Domoticz

try:
    from geckolib import GeckoLocator
except ImportError:
    GeckoLocator = None

# ---------------------------------------------------------------------------
# Domoticz device unit assignments
# ---------------------------------------------------------------------------
UNIT_CURRENT_TEMP = 1   # Temperature sensor (current water temp)
UNIT_SETPOINT = 2       # Thermostat setpoint
UNIT_WATER_CARE = 3     # Water care mode selector
UNIT_ECO_MODE = 4       # Eco mode switch
UNIT_ERROR = 5          # Error text sensor

# Pumps (fixed mapping by key)
UNIT_PUMP_P1 = 10
UNIT_PUMP_P2 = 11
UNIT_PUMP_P3 = 12
UNIT_PUMP_P4 = 13
UNIT_PUMP_P5 = 14
UNIT_BLOWER = 15
UNIT_LIGHTS = 16
UNIT_WATERFALL = 17

PUMP_KEY_TO_UNIT = {
    "P1": UNIT_PUMP_P1,
    "P2": UNIT_PUMP_P2,
    "P3": UNIT_PUMP_P3,
    "P4": UNIT_PUMP_P4,
    "P5": UNIT_PUMP_P5,
    "Waterfall": UNIT_WATERFALL,
}

# Binary sensors start at this unit number (indexed by position in facade list)
UNIT_BINARY_BASE = 20

# Reconnect delay in seconds
RECONNECT_DELAY = 30

# Connection attempt timeout in seconds
CONNECTION_TIMEOUT = 90


def _update_device_if_changed(unit, n_value, s_value):
    """Update a Domoticz device only when the value has actually changed."""
    if unit not in Devices:
        return
    device = Devices[unit]
    if device.nValue != n_value or device.sValue != s_value:
        device.Update(nValue=n_value, sValue=s_value)


class BasePlugin:
    """Domoticz base plugin for Gecko Alliance spa control."""

    def __init__(self):
        self._facade = None
        self._lock = threading.Lock()
        self._connection_thread = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Domoticz plugin callbacks
    # ------------------------------------------------------------------

    def onStart(self):
        if Parameters["Mode6"] == "Debug":
            Domoticz.Debugging(1)

        if GeckoLocator is None:
            Domoticz.Error(
                "geckolib is not installed. "
                "Please install it with: pip3 install geckolib"
            )
            return

        Domoticz.Log("Gecko Spa Plugin starting")
        Domoticz.Heartbeat(30)

        self._stop_event.clear()
        self._connection_thread = threading.Thread(
            target=self._connection_worker, daemon=True, name="GeckoSpaConnect"
        )
        self._connection_thread.start()

    def onStop(self):
        Domoticz.Log("Gecko Spa Plugin stopping")
        self._stop_event.set()

        with self._lock:
            facade = self._facade
            self._facade = None

        if facade is not None:
            try:
                facade.complete()
            except Exception as exc:
                Domoticz.Error(f"Error closing spa facade: {exc}")

        if self._connection_thread and self._connection_thread.is_alive():
            self._connection_thread.join(timeout=15)

    def onHeartbeat(self):
        with self._lock:
            facade = self._facade

        if facade is None or not facade.is_connected:
            return

        try:
            self._create_missing_devices(facade)
            self._update_devices(facade)
        except Exception as exc:
            Domoticz.Error(f"Error in heartbeat: {exc}")

    def onCommand(self, Unit, Command, Level, Hue):
        with self._lock:
            facade = self._facade

        if facade is None or not facade.is_connected:
            Domoticz.Error("Cannot send command: spa not connected")
            return

        Command = Command.strip()
        Domoticz.Debug(f"onCommand Unit={Unit} Command={Command} Level={Level}")

        try:
            self._handle_command(facade, Unit, Command, Level)
        except Exception as exc:
            Domoticz.Error(f"Error handling command: {exc}")
            return

        # Brief pause then refresh the relevant device
        time.sleep(0.5)
        try:
            self._update_devices(facade)
        except Exception as exc:
            Domoticz.Error(f"Error refreshing after command: {exc}")

    # ------------------------------------------------------------------
    # Background connection worker
    # ------------------------------------------------------------------

    def _connection_worker(self):
        """Discover and connect to the spa in a background thread."""
        while not self._stop_event.is_set():
            try:
                self._do_connect()
            except Exception as exc:
                Domoticz.Error(f"Spa connection error: {exc}")

            if self._stop_event.is_set():
                return

            Domoticz.Log(
                f"Will retry spa connection in {RECONNECT_DELAY} seconds..."
            )
            for _ in range(RECONNECT_DELAY):
                if self._stop_event.is_set():
                    return
                time.sleep(1)

    def _do_connect(self):
        """Perform a single discovery + connection attempt."""
        client_uuid = Parameters.get("Password", "").strip()
        if not client_uuid:
            Domoticz.Error(
                "Client UUID is required. "
                "Generate one with: python3 -c \"import uuid; print(uuid.uuid4())\""
            )
            return

        spa_identifier = Parameters.get("Username", "").strip() or None
        static_ip = Parameters.get("Address", "").strip() or None

        Domoticz.Log(
            f"Discovering Gecko spa"
            f"{' at ' + static_ip if static_ip else ' via broadcast'}"
            f"{' (id: ' + spa_identifier + ')' if spa_identifier else ''}..."
        )

        # Discover the spa descriptor
        descriptor = self._discover_spa(client_uuid, spa_identifier, static_ip)
        if descriptor is None:
            Domoticz.Error("No Gecko spa found on the network")
            return

        Domoticz.Log(
            f"Found spa: {descriptor.name} ({descriptor.identifier_as_string}) "
            f"at {descriptor.ipaddress}"
        )

        # Connect (blocking until connected or timeout)
        facade = descriptor.get_facade(wait_for_connection=False)

        deadline = time.monotonic() + CONNECTION_TIMEOUT
        while not facade.is_connected:
            if self._stop_event.is_set():
                facade.complete()
                return
            if time.monotonic() > deadline:
                Domoticz.Error("Timeout waiting for spa connection")
                facade.complete()
                return
            time.sleep(0.1)

        Domoticz.Log(f"Connected to spa: {descriptor.name}")

        with self._lock:
            self._facade = facade

        # Monitor the connection until it drops or plugin stops
        while not self._stop_event.is_set():
            time.sleep(5)
            if not facade.is_connected:
                Domoticz.Log("Spa connection lost")
                break

        with self._lock:
            if self._facade is facade:
                self._facade = None

        if not self._stop_event.is_set():
            try:
                facade.complete()
            except Exception as exc:
                Domoticz.Debug(f"Error closing facade after disconnect: {exc}")

    @staticmethod
    def _discover_spa(client_uuid, spa_identifier=None, static_ip=None):
        """Run discovery and return the first matching GeckoSpaDescriptor, or None."""
        try:
            with GeckoLocator(
                client_uuid,
                spa_to_find=spa_identifier,
                static_ip=static_ip,
            ) as locator:
                if not locator.spas:
                    return None
                if spa_identifier:
                    return locator.get_spa_from_identifier(spa_identifier)
                return locator.spas[0]
        except Exception as exc:
            Domoticz.Error(f"Discovery error: {exc}")
            return None

    # ------------------------------------------------------------------
    # Device creation
    # ------------------------------------------------------------------

    def _create_missing_devices(self, facade):
        """Create any Domoticz devices that are not yet present."""

        # Current water temperature
        if UNIT_CURRENT_TEMP not in Devices:
            Domoticz.Device(
                Name="Water Temperature",
                Unit=UNIT_CURRENT_TEMP,
                TypeName="Temperature",
            ).Create()

        # Water setpoint (thermostat)
        if UNIT_SETPOINT not in Devices:
            Domoticz.Device(
                Name="Water Set Point",
                Unit=UNIT_SETPOINT,
                Type=242,
                Subtype=1,
            ).Create()

        # Water care mode selector
        if UNIT_WATER_CARE not in Devices and facade.water_care is not None:
            modes = facade.water_care.modes
            Domoticz.Device(
                Name="Water Care",
                Unit=UNIT_WATER_CARE,
                TypeName="Selector Switch",
                Switchtype=18,
                Options={
                    "LevelActions": "|" * (len(modes) - 1),
                    "LevelNames": "|".join(modes),
                    "LevelOffHidden": "false",
                    "SelectorStyle": "0",
                },
            ).Create()

        # Eco mode switch
        if UNIT_ECO_MODE not in Devices and facade.eco_mode is not None:
            Domoticz.Device(
                Name="Eco Mode",
                Unit=UNIT_ECO_MODE,
                TypeName="Switch",
            ).Create()

        # Error sensor (text)
        if UNIT_ERROR not in Devices:
            Domoticz.Device(
                Name="Error",
                Unit=UNIT_ERROR,
                TypeName="Text",
            ).Create()

        # Pumps
        for pump in facade.pumps:
            unit = PUMP_KEY_TO_UNIT.get(pump.ui_key)
            if unit is None:
                continue
            if unit not in Devices:
                modes = pump.modes
                Domoticz.Device(
                    Name=pump.name,
                    Unit=unit,
                    TypeName="Selector Switch",
                    Switchtype=18,
                    Options={
                        "LevelActions": "|" * (len(modes) - 1),
                        "LevelNames": "|".join(modes),
                        "LevelOffHidden": "false",
                        "SelectorStyle": "0",
                    },
                ).Create()

        # Blower
        if facade.blowers and UNIT_BLOWER not in Devices:
            Domoticz.Device(
                Name="Blower",
                Unit=UNIT_BLOWER,
                TypeName="Switch",
            ).Create()

        # Lights
        if facade.lights and UNIT_LIGHTS not in Devices:
            Domoticz.Device(
                Name="Lights",
                Unit=UNIT_LIGHTS,
                TypeName="Switch",
            ).Create()

        # Binary sensors
        for i, sensor in enumerate(facade.binary_sensors):
            unit = UNIT_BINARY_BASE + i
            if unit not in Devices:
                Domoticz.Device(
                    Name=sensor.name,
                    Unit=unit,
                    TypeName="Switch",
                ).Create()

    # ------------------------------------------------------------------
    # Device value updates
    # ------------------------------------------------------------------

    def _update_devices(self, facade):
        """Synchronise all Domoticz device values from the facade."""

        # Water heater
        heater = facade.water_heater
        if heater is not None and heater.is_present:
            try:
                temp = heater.current_temperature
                if temp is not None and UNIT_CURRENT_TEMP in Devices:
                    _update_device_if_changed(UNIT_CURRENT_TEMP, 0, str(temp))
            except Exception as exc:
                Domoticz.Debug(f"Could not read current temperature: {exc}")

            try:
                setpoint = heater.target_temperature
                if setpoint is not None and UNIT_SETPOINT in Devices:
                    _update_device_if_changed(UNIT_SETPOINT, 0, str(setpoint))
            except Exception as exc:
                Domoticz.Debug(f"Could not read setpoint: {exc}")

        # Water care mode
        if facade.water_care is not None and UNIT_WATER_CARE in Devices:
            mode_idx = facade.water_care.active_mode
            if mode_idx is not None:
                level = mode_idx * 10
                _update_device_if_changed(
                    UNIT_WATER_CARE,
                    0 if level == 0 else 1,
                    str(level),
                )

        # Eco mode
        if facade.eco_mode is not None and UNIT_ECO_MODE in Devices:
            is_on = facade.eco_mode.is_on
            _update_device_if_changed(
                UNIT_ECO_MODE,
                1 if is_on else 0,
                "On" if is_on else "Off",
            )

        # Error sensor
        if facade.error_sensor is not None and UNIT_ERROR in Devices:
            _update_device_if_changed(UNIT_ERROR, 0, str(facade.error_sensor.state))

        # Pumps
        for pump in facade.pumps:
            unit = PUMP_KEY_TO_UNIT.get(pump.ui_key)
            if unit is None or unit not in Devices:
                continue
            modes = pump.modes
            mode = pump.mode
            level = modes.index(mode) * 10 if mode in modes else 0
            _update_device_if_changed(
                unit, 0 if level == 0 else 1, str(level)
            )

        # Blower
        if facade.blowers and UNIT_BLOWER in Devices:
            is_on = facade.blowers[0].is_on
            _update_device_if_changed(
                UNIT_BLOWER,
                1 if is_on else 0,
                "On" if is_on else "Off",
            )

        # Lights
        if facade.lights and UNIT_LIGHTS in Devices:
            is_on = facade.lights[0].is_on
            _update_device_if_changed(
                UNIT_LIGHTS,
                1 if is_on else 0,
                "On" if is_on else "Off",
            )

        # Binary sensors
        for i, sensor in enumerate(facade.binary_sensors):
            unit = UNIT_BINARY_BASE + i
            if unit not in Devices:
                continue
            is_on = sensor.is_on
            _update_device_if_changed(
                unit,
                1 if is_on else 0,
                "On" if is_on else "Off",
            )

    # ------------------------------------------------------------------
    # Command handling
    # ------------------------------------------------------------------

    def _handle_command(self, facade, unit, command, level):
        """Route a Domoticz command to the appropriate facade action."""

        # Setpoint
        if unit == UNIT_SETPOINT:
            if command == "Set Level":
                facade.water_heater.set_target_temperature(float(level))

        # Water care mode
        elif unit == UNIT_WATER_CARE:
            if command == "Set Level":
                mode_idx = int(level) // 10
                facade.water_care.set_mode(mode_idx)

        # Eco mode
        elif unit == UNIT_ECO_MODE:
            if command == "On":
                facade.eco_mode.turn_on()
            elif command == "Off":
                facade.eco_mode.turn_off()

        # Pumps
        elif unit in PUMP_KEY_TO_UNIT.values():
            pump_key = next(
                (k for k, v in PUMP_KEY_TO_UNIT.items() if v == unit), None
            )
            if pump_key is None:
                return
            pump = next((p for p in facade.pumps if p.ui_key == pump_key), None)
            if pump is None:
                return
            modes = pump.modes
            if command == "Set Level":
                mode_idx = int(level) // 10
                if 0 <= mode_idx < len(modes):
                    pump.set_mode(modes[mode_idx])
            elif command == "Off" and "OFF" in modes:
                pump.set_mode("OFF")
            elif command == "On":
                # Turn on to the first non-OFF mode
                on_modes = [m for m in modes if m != "OFF"]
                if on_modes:
                    pump.set_mode(on_modes[0])

        # Blower
        elif unit == UNIT_BLOWER and facade.blowers:
            if command == "On":
                facade.blowers[0].turn_on()
            elif command == "Off":
                facade.blowers[0].turn_off()

        # Lights
        elif unit == UNIT_LIGHTS and facade.lights:
            if command == "On":
                facade.lights[0].turn_on()
            elif command == "Off":
                facade.lights[0].turn_off()

        else:
            Domoticz.Debug(f"Unhandled command: unit={unit} command={command}")


# ---------------------------------------------------------------------------
# Domoticz plugin entry-point functions
# ---------------------------------------------------------------------------

_plugin = BasePlugin()


def onStart():
    _plugin.onStart()


def onStop():
    _plugin.onStop()


def onHeartbeat():
    _plugin.onHeartbeat()


def onCommand(Unit, Command, Level, Hue):
    _plugin.onCommand(Unit, Command, Level, Hue)
