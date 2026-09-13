# Govee H6006 bulbs in Home Assistant, without the cloud

[Beshelmek's Govee BLE Lights](https://github.com/Beshelmek/govee_ble_lights) is a really cool project. But with my H6006 bulbs, BLE directly from the Raspberry Pi was flaky. Connections would drop and controlling the lights wasn't reliable enough for everyday use.

I didn't want to fall back to the Govee API. These are lightbulbs; I want them to work with their internet access blocked. That meant finding a way to keep the Bluetooth connections working.

This project uses ESP32s to hold persistent BLE connections to the H6006s. The ESP32s send keepalives and reconnect if a bulb drops out. They plug into the Raspberry Pi over USB, and Home Assistant sends light commands over serial. The bulbs don't need internet access, and the ESP32s don't use Wi-Fi.

Both parts are here: the Home Assistant integration in `custom_components/esp32_bulb_relay/` and the [ESP32 Arduino sketch](firmware/govee_controller_serial/govee_controller_serial.ino).

## Setup

You'll need H6006 bulbs, BLE-capable ESP32 boards, USB data cables, and Home Assistant. The sketch compiles for ESP32 Dev Module with Espressif's Arduino core 3.3.5 and NimBLE-Arduino 2.3.7. Use up to three bulbs per ESP32 with that library's default settings.

1. Open the sketch in Arduino IDE. Set `DEBUG` to `1`, upload, and open Serial Monitor at 115200 baud with Newline endings. Reset the board, select your bulbs from the scan, give each a unique name, and send `-1` to save.
2. Set `DEBUG` back to `0` and upload again without erasing flash or changing partitions. The board remembers its bulbs. Close Serial Monitor and plug the ESP32 into your Home Assistant host.
3. In HACS, add `https://github.com/ThreeMuskets12/govee-h6006-home-assistant-local` as a custom repository, category Integration. Install **Govee H6006 Local (ESP32 Bulb Relay)** and restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration → ESP32 Bulb Relay**, select the serial port, and choose your bulbs. Add more boards through **Configure → Add ESP32 Port**.

The [setup guide](docs/setup.md) covers flashing, changing bulb assignments, manual installation, USB passthrough, and troubleshooting. First-time setup needs `DEBUG=1` because the production build hides the prompts.

## A few things to know

Power, brightness, RGB, and white temperature are supported for the H6006. I haven't verified other models. Every bulb needs a different name, even across separate ESP32s.

Home Assistant remembers the commands it sends; it doesn't read the bulb's actual power or color back. Commands on each ESP32 are spaced at least 500 ms apart, so several queued changes take longer.

The firmware is preserved from the latest local sketch I found. Both production and setup builds compile, but I haven't compared it with a binary readback from the installed boards. [Firmware details](firmware/PROVENANCE.md).

[MIT license](LICENSE).
