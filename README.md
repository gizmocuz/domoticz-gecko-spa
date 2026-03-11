# domoticz-gecko-spa

A [Domoticz](https://www.domoticz.com/) Python plugin for controlling
[Gecko Alliance](https://www.geckoalliance.com/) spa, hot tub, and pool equipment via the
local network using the [geckolib](https://github.com/gazoodle/geckolib) library.

## Features

* Current water temperature sensor
* Water set-point thermostat (read and write)
* Water care mode selector (Away From Home / Standard / Energy Saving / Super Energy Saving / Weekender)
* Eco mode switch
* Error sensor (aggregated spa error / warning text)
* Pump controls (supports single-speed and multi-speed pumps P1–P5, Waterfall)
* Blower switch
* Lights switch
* Binary sensors (Circulating Pump, Pump Run, Ozone, Smart Winter Mode, Filter Status Clean/Purge)

## Prerequisites

* Domoticz with Python plugin support enabled
* Python 3 with the `geckolib` package installed:

  ```bash
  pip3 install geckolib
  ```

* A Gecko Alliance in.touch 2 enabled spa on your local network

## Installation

1. Navigate to the Domoticz plugins directory (e.g. `~/.domoticz/plugins/` or
   `/home/pi/domoticz/plugins/`).
2. Clone this repository:

   ```bash
   git clone https://github.com/gizmocuz/domoticz-gecko-spa.git
   ```

3. Restart Domoticz (or reload the plugin engine).
4. In the Domoticz web interface go to **Setup → Hardware** and add a new device of
   type **Gecko Alliance Spa**.

## Configuration

| Parameter | Description |
|-----------|-------------|
| **Spa IP Address** | Static IP of the spa's in.touch 2 module. Leave blank to use automatic LAN broadcast discovery. |
| **Spa Name or Identifier** | The identifier string shown on the in.touch 2 module (optional). Leave blank to connect to the first spa found. |
| **Client UUID** | A unique UUID that identifies this controller. Generate one once and reuse it across restarts (see below). |
| **Debug** | Set to *True* to enable verbose debug logging in the Domoticz log. |

### Generating a Client UUID

Run the following command once and paste the result into the *Client UUID* field:

```bash
python3 -c "import uuid; print(uuid.uuid4())"
```

## Devices created

The plugin creates the following Domoticz devices (only those supported by your spa are
created):

| Unit | Name | Type | Description |
|------|------|------|-------------|
| 1 | Water Temperature | Temperature | Current water temperature |
| 2 | Water Set Point | Thermostat Setpoint | Target temperature (read/write) |
| 3 | Water Care | Selector Switch | Active water-care programme |
| 4 | Eco Mode | Switch | Economy mode on/off |
| 5 | Error | Text | Aggregated error/warning string |
| 10 | Pump 1 (P1) | Selector Switch | Pump 1 speed control |
| 11 | Pump 2 (P2) | Selector Switch | Pump 2 speed control |
| 12 | Pump 3 (P3) | Selector Switch | Pump 3 speed control |
| 13 | Pump 4 (P4) | Selector Switch | Pump 4 speed control |
| 14 | Pump 5 (P5) | Selector Switch | Pump 5 speed control |
| 15 | Blower | Switch | Air blower on/off |
| 16 | Lights | Switch | Spa lights on/off |
| 17 | Waterfall | Selector Switch | Waterfall pump control |
| 20+ | Binary Sensors | Switch | Circulating Pump, Ozone, Filter Status, etc. |

Only devices supported by your spa model will be created.

## Troubleshooting

* Enable *Debug* mode in the hardware settings to see detailed log output.
* Make sure the Domoticz server and the spa are on the same LAN subnet (UDP broadcast
  must be able to reach the in.touch 2 module).
* If you use a static IP address, the spa does not need to be on the same broadcast
  domain.
* The plugin automatically attempts to reconnect if the spa connection is lost.

## License

This project is licensed under the [MIT License](LICENSE).
