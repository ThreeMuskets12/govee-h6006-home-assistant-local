# ESP32 Bulb Relay Integration for Home Assistant

A Home Assistant custom integration for controlling smart lights via ESP32 Bulb Relay devices that expose an HTTP API.

## Features

- **Multi-ESP32 Support**: Add multiple ESP32 devices, each supporting up to 4 bulbs
- **Full Light Control**: On/Off, Brightness, RGB color, and White Temperature (2000-9000K)
- **Proper Color Modes**: Separate RGB and Color Temperature modes (no RGBW encoding)
- **Flexible Configuration**: Add/remove bulbs and ESP32 devices through the UI
- **Debug Commands**: Manual connect/disconnect for troubleshooting (settings only)
- **Command Queue**: Built-in rate limiting (500ms between commands) to prevent overwhelming the ESP32

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click on "Integrations"
3. Click the three dots in the top right corner
4. Select "Custom repositories"
5. Add this repository URL and select "Integration" as the category
6. Click "Add"
7. Search for "ESP32 Bulb Relay" and install it
8. Restart Home Assistant

### Manual Installation

1. Download or clone this repository
2. Copy the `esp32_bulb_relay` folder to your `config/custom_components/` directory
3. Restart Home Assistant

## Configuration

### Initial Setup

1. Go to **Settings** → **Devices & Services**
2. Click **Add Integration**
3. Search for "ESP32 Bulb Relay"
4. Enter the IP address of your ESP32 device
5. Select which bulbs to add to Home Assistant
6. Click Submit

### Managing the Integration

After initial setup, click **Configure** on the integration card to access settings:

#### Manage Bulbs
Enable or disable individual bulbs from appearing in Home Assistant.

#### Add ESP32
Add additional ESP32 Bulb Relay devices to the same integration instance.

#### Remove ESP32
Remove an ESP32 device and all its associated bulbs.

#### Debug Commands
Access manual connect/disconnect commands for troubleshooting. These commands are hidden in the debug menu because they should only be used when necessary.

## ESP32 API Reference

The integration expects your ESP32 to expose the following HTTP endpoints:

| Endpoint | Description |
|----------|-------------|
| `GET /bulbs` | Returns list of connected bulbs |
| `GET /bulb/{name}/on` | Turn bulb on |
| `GET /bulb/{name}/off` | Turn bulb off |
| `GET /bulb/{name}/brightness/{0-100}` | Set brightness |
| `GET /bulb/{name}/rgb/r={0-255}&g={0-255}&b={0-255}` | Set RGB color |
| `GET /bulb/{name}/temperature/{2000-9000}` | Set white temperature in Kelvin |
| `GET /bulb/{name}/connect` | Reconnect bulb (debug) |
| `GET /bulb/{name}/disconnect` | Disconnect bulb (debug) |

### API Response Formats

**`GET /bulbs`**
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

**`GET /bulb/{name}/on`**
```json
{"success": true, "bulb": "lamp", "action": "on"}
```

**`GET /bulb/{name}/off`**
```json
{"success": true, "bulb": "lamp", "action": "off"}
```

**`GET /bulb/{name}/brightness/{value}`**
```json
{"success": true, "bulb": "lamp", "action": "brightness", "value": 75}
```

**`GET /bulb/{name}/rgb/r={r}&g={g}&b={b}`**
```json
{"success": true, "bulb": "lamp", "action": "rgb", "r": 255, "g": 128, "b": 0}
```

**`GET /bulb/{name}/temperature/{value}`**
```json
{"success": true, "bulb": "lamp", "action": "temperature", "value": 4000}
```

**`GET /bulb/{name}/connect`**
```json
{"success": true, "bulb": "lamp", "action": "connect"}
```

**`GET /bulb/{name}/disconnect`**
```json
{"success": true, "bulb": "lamp", "action": "disconnect"}
```

## Services

The integration registers the following services:

### `esp32_bulb_relay.connect_bulb`
Manually reconnect a disconnected bulb.

| Field | Description |
|-------|-------------|
| `esp32_host` | IP address of the ESP32 |
| `bulb_name` | Name of the bulb to connect |

### `esp32_bulb_relay.disconnect_bulb`
Manually disconnect a bulb.

| Field | Description |
|-------|-------------|
| `esp32_host` | IP address of the ESP32 |
| `bulb_name` | Name of the bulb to disconnect |

### `esp32_bulb_relay.refresh_bulbs`
Force refresh all bulb states.

## Light Entity Features

Each bulb is created as a light entity with the following capabilities:

- **On/Off**: Basic on/off control
- **Brightness**: 0-100% (mapped to 0-255 internally)
- **RGB Color Mode**: Full RGB color selection
- **Color Temperature Mode**: White temperature from 2000K to 9000K

The light uses separate color modes (not RGBW combined mode), so you can either set an RGB color OR a white temperature, matching how most smart bulbs actually work.

### Entity Attributes

Each light entity exposes the following attributes:

| Attribute | Description |
|-----------|-------------|
| `bulb_id` | The bulb's ID on the ESP32 |
| `address` | The bulb's Bluetooth MAC address |
| `connected` | Whether the bulb is currently connected |
| `esp32_host` | The IP address of the ESP32 this bulb is connected to |

## Command Queue & Rate Limiting

The integration includes a built-in command queue to prevent overwhelming the ESP32 with rapid requests. Commands are processed with a minimum 500ms delay between each one.

**How it works:**
- Each ESP32 has its own independent command queue
- Commands (on, off, brightness, color, temperature) are queued and executed sequentially
- Status polling (`/bulbs` endpoint) is NOT rate-limited to ensure timely updates
- If multiple commands are sent quickly (e.g., sliding a brightness slider), they queue up and execute in order

**Example scenario:**
1. User rapidly adjusts brightness from 0% to 100%
2. Multiple brightness commands queue up
3. Commands execute one by one, 500ms apart
4. ESP32 receives a manageable stream of requests

The 500ms interval can be adjusted by modifying `MIN_COMMAND_INTERVAL` in `const.py`.

## Troubleshooting

### Cannot connect to ESP32
- Verify the IP address is correct
- Ensure the ESP32 is powered on and connected to your network
- Check that the ESP32's HTTP server is running
- Try accessing `http://<esp32-ip>/bulbs` directly in a browser

### Bulb not responding
1. Go to integration settings
2. Navigate to Debug Commands
3. Try "Disconnect Bulb" followed by "Connect Bulb"
4. Check ESP32 serial output for errors

### Bulbs not appearing
- Ensure the bulbs are enabled in "Manage Bulbs" settings
- Try the "Refresh Bulbs" service
- Restart Home Assistant

## Contributing

Contributions are welcome! Please feel free to submit issues and pull requests.

## License

This project is licensed under the MIT License.
