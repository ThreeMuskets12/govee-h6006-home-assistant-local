# Govee H6006 bulbs in Home Assistant, No Govee API

Credit to [Beshelmek's Govee BLE Lights](https://github.com/Beshelmek/govee_ble_lights) for a lot of the work on the Govee BLE protocol. I have H6006 bulbs, and the BLE connection direct from the Raspberry Pi was not reliable enough to count on for daily use.

This project uses ESP32s to hold persistent BLE connections to the H6006s. The ESP32s send keepalives and reconnect if a bulb drops out. They plug into the Raspberry Pi over USB, and Home Assistant sends light commands over serial. The bulbs and ESP32s can be completely offline!

Both parts are here: the Home Assistant integration in `custom_components/esp32_bulb_relay/` and the [ESP32 Arduino sketch](firmware/govee_controller_serial/govee_controller_serial.ino).

## Setup

You need 1 ESP32 per 3 Govee H6006s.

1. Open the sketch in Arduino IDE. Set `DEBUG` to `1`, upload, and open Serial Monitor at 115200 baud with Newline endings. Reset the board, select your bulbs from the scan, give each a unique name, and send `-1` to save.
2. Set `DEBUG` back to `0` and upload again without erasing flash or changing partitions. The board remembers its bulbs. Close Serial Monitor and plug the ESP32 into your Home Assistant host.
3. In HACS, add `https://github.com/ThreeMuskets12/govee-h6006-home-assistant-local` as a custom repository, category Integration. Install **Govee H6006 Local (ESP32 Bulb Relay)** and restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration → ESP32 Bulb Relay**, select the serial port, and choose your bulbs. Add more boards through **Configure → Add ESP32 Port**.

The [setup guide](docs/setup.md) covers flashing, changing bulb assignments, manual installation, USB passthrough, and troubleshooting. First-time setup needs `DEBUG=1` because the without it the naming prompts don't get sent.

## A few things to know

Power, brightness, RGB, and white temperature are supported for the H6006. Every bulb needs a different name, even across separate ESP32s.

Home Assistant remembers the commands it sends; it doesn't read the bulb's actual power or color back. Commands on each ESP32 are spaced at least 500 ms apart, so several queued changes take longer.

[MIT license](LICENSE).
