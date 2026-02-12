# ESP32 Bulb Relay Integration for Home Assistant

A Home Assistant custom integration for controlling smart lights via ESP32 Bulb Relay devices over USB serial connection.

## Features

- **Serial over USB Communication**: Direct, low-latency connection via serial port at 115200 baud
- **Dynamic Port Mapping**: Bulbs are identified by name, not by USB port. If ports get reassigned after a reboot, the integration automatically finds bulbs on their new ports
- **Multi-ESP32 Support**: Add multiple ESP32 devices, each supporting up to 4 bulbs
- **Full Light Control**: On/Off, Brightness, RGB color, and White Temperature (2000-9000K)
- **Auto-Rescan**: If a command fails, the integration automatically rescans ports to find the bulb
- **Command Queue**: Built-in rate limiting (500ms between commands) to prevent overwhelming the ESP32

## How Dynamic Port Mapping Works

USB serial ports (like `/dev/ttyUSB0`, `/dev/ttyUSB1`) can be assigned differently each time the system boots. This integration handles this automatically:

1. **Bulbs are stored by name only** - not tied to specific ports
2. **On startup**, all configured ports are scanned to build a `bulb → port` mapping
3. **Every 30 seconds**, the mapping is refreshed
4. **If a command fails**, the integration rescans all ports and retries

This means if you have two ESP32s and they swap ports after a reboot, your bulbs will still work!

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click on "Integrations"
3. Click the three dots in the top right corner
4. Select "Custom repositories"
5. Add this repository URL and select "Integration" as the category
6. Search for "ESP32 Bulb Relay" and install it
7. Restart Home Assistant

### Manual Installation

1. Download or clone this repository
2. Copy the `esp32_bulb_relay` folder to your `config/custom_components/` directory
3. Restart Home Assistant

## Configuration

### Initial Setup

1. Connect your ESP32 to your Home Assistant server via USB
2. Go to **Settings** → **Devices & Services**
3. Click **Add Integration**
4. Search for "ESP32 Bulb Relay"
5. Select the serial port where your ESP32 is connected
6. Select which bulbs to add to Home Assistant
7. Click Submit

### Managing the Integration

Click **Configure** on the integration card to access settings:

#### Manage Bulbs
Enable or disable individual bulbs. The integration scans all ports to show available bulbs.

#### Add ESP32 Port
Add additional serial ports to scan for bulbs.

#### Remove ESP32 Port
Remove a serial port from scanning. Note: Bulbs might still be found on other ports.

#### Rescan All Ports
Manually trigger a rescan of all ports. Shows which bulbs were found on which ports, and which bulbs are missing. Useful for troubleshooting.

#### Debug Commands
Access manual connect/disconnect commands for troubleshooting.

## Services

### `esp32_bulb_relay.rescan_ports`
Rescan all configured serial ports and rebuild the bulb→port mapping. Use this if you've rebooted and ports have changed.

### `esp32_bulb_relay.refresh_bulbs`
Force refresh all bulb states.

### `esp32_bulb_relay.connect_bulb`
Manually reconnect a bulb (debug).

| Field | Description |
|-------|-------------|
| `bulb_name` | Name of the bulb to connect |

### `esp32_bulb_relay.disconnect_bulb`
Manually disconnect a bulb (debug).

| Field | Description |
|-------|-------------|
| `bulb_name` | Name of the bulb to disconnect |

## ESP32 Serial Commands

Commands are sent over serial at 115200 baud:

| Command | Description |
|---------|-------------|
| `/bulbs` | Returns list of connected bulbs |
| `/bulb/{name}/on` | Turn bulb on |
| `/bulb/{name}/off` | Turn bulb off |
| `/bulb/{name}/brightness/{0-100}` | Set brightness |
| `/bulb/{name}/rgb/r={0-255}&g={0-255}&b={0-255}` | Set RGB color |
| `/bulb/{name}/temperature/{2000-9000}` | Set white temperature in Kelvin |
| `/bulb/{name}/connect` | Reconnect bulb (debug) |
| `/bulb/{name}/disconnect` | Disconnect bulb (debug) |

### Response Formats

**`/bulbs`**
```json
{
  "bulbs": [
    {
      "id": 0,
      "name": "lamp",
      "address": "d0:c9:07:81:56:b9",
      "connected": true
    }
  ],
  "count": 1
}
```

**Command responses:**
```json
{"success": true, "bulb": "lamp", "action": "on"}
{"success": true, "bulb": "lamp", "action": "brightness", "value": 75}
```

## Light Entity Attributes

Each light entity shows:

| Attribute | Description |
|-----------|-------------|
| `bulb_id` | The bulb's ID on the ESP32 |
| `address` | The bulb's Bluetooth MAC address |
| `connected` | Whether the bulb is connected |
| `current_port` | Which serial port the bulb is currently on |

## Docker / Home Assistant OS

If running in Docker, pass through USB devices:

```yaml
devices:
  - /dev/ttyUSB0:/dev/ttyUSB0
  - /dev/ttyUSB1:/dev/ttyUSB1
```

For multiple ESP32s, pass through all potential ports.

## Troubleshooting

### Bulbs show as unavailable after reboot
1. Go to integration settings → "Rescan All Ports"
2. Or call the `esp32_bulb_relay.rescan_ports` service
3. The integration will find bulbs on their new ports

### No serial ports detected
- Ensure the ESP32 is connected via USB
- Check that the USB cable supports data
- On Linux, add your user to the `dialout` group
- In Docker, ensure devices are passed through

### Commands failing intermittently
- The integration will auto-rescan on failure
- Try increasing `MIN_COMMAND_INTERVAL` in `const.py` if needed

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Home Assistant                        │
├─────────────────────────────────────────────────────────┤
│  Light Entity: "LeftLamp"    Light Entity: "Ceiling1"   │
│         │                           │                    │
│         └───────────┬───────────────┘                    │
│                     ▼                                    │
│              ┌─────────────┐                             │
│              │ Coordinator │                             │
│              │ bulb→port   │                             │
│              │ mapping     │                             │
│              └─────────────┘                             │
│                     │                                    │
│         ┌───────────┴───────────┐                       │
│         ▼                       ▼                       │
│   ┌───────────┐           ┌───────────┐                │
│   │ API Client│           │ API Client│                │
│   │/dev/ttyUSB0│          │/dev/ttyUSB1│               │
│   └───────────┘           └───────────┘                │
└─────────────────────────────────────────────────────────┘
         │                       │
         ▼                       ▼
    ┌─────────┐            ┌─────────┐
    │  ESP32  │            │  ESP32  │
    │ LeftLamp│            │Ceiling1 │
    │RightLamp│            │Ceiling2 │
    └─────────┘            └─────────┘
```

## Contributing

Contributions are welcome! Please feel free to submit issues and pull requests.

## License

This project is licensed under the MIT License.
