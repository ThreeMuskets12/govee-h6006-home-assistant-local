# Setup and troubleshooting

[Back to the README](../README.md).

## What you need

- Govee H6006 bulbs, powered and within BLE range of their assigned ESP32.
- A BLE-capable ESP32 development board and USB **data** cable per group of bulbs. The compile-checked target is the classic **ESP32 Dev Module** (`esp32:esp32:esp32`); other board variants are not verified.
- Home Assistant with access to those USB serial devices. A powered USB hub may help when using several boards.
- Arduino IDE with **esp32 by Espressif Systems 3.3.5** and **NimBLE-Arduino by h2zero 2.3.7**. `Preferences` comes with the ESP32 core. See [Espressif's installation guide](https://docs.espressif.com/projects/arduino-esp32/en/latest/installing.html) and [NimBLE-Arduino](https://github.com/h2zero/NimBLE-Arduino).

These versions are the locally available build baseline, not a requirement to upgrade a working installation. Internet access is needed to download development tools and install the integration; light commands are local afterward.

## 1. Flash and provision each ESP32

The firmware is [firmware/govee_controller_serial/govee_controller_serial.ino](../firmware/govee_controller_serial/govee_controller_serial.ino). It is an Arduino sketch, not a PlatformIO project. It is preserved unchanged from the recovered source; see [firmware provenance](../firmware/PROVENANCE.md).

**First-time setup needs `DEBUG=1`.** The recovered production sketch sets `DEBUG=0`, which also hides the interactive provisioning prompts. Flashing that default onto a blank board leaves it waiting silently for bulb selection.

1. Download or clone this repository and open the `.ino` file in Arduino IDE. Keep the enclosing folder named `govee_controller_serial`.
2. Install the board core and NimBLE library listed above. Select your board and USB port. For the classic ESP32 target, use **ESP32 Dev Module**.
3. In the sketch, temporarily change `#define DEBUG 0` to `#define DEBUG 1`. Set **Erase All Flash Before Sketch Upload** to **Disabled** and leave it disabled for the subsequent production upload.
4. Upload. Open Serial Monitor at **115200 baud**, with **Newline** line endings, then press the board's reset button so you can see the startup sequence.
5. A board without saved assignments scans BLE devices for about five seconds and prints a numbered list. Choose the number for a known H6006, send it, then send a unique bulb name when prompted. The scan includes unrelated BLE devices: identify your bulbs by their advertised name/address, using one powered bulb at a time if needed. Close the Govee app while connecting.
6. Repeat for up to **three bulbs** with the default library. Use simple names such as `desk_lamp` or `ceiling_1`, containing only letters, digits, underscores, or hyphens. Names must be unique across **all** ESP32s, including case-insensitive comparisons. Names are routing/entity identifiers; quotes, slashes, and other special characters are not safely escaped by this firmware.
7. Send `-1` to finish. At least one successfully connected bulb is required. Wait for setup to complete so the firmware saves the names and addresses in NVS.
8. Send `/bulbs`. Expect one JSON line listing the configured bulbs and their connection status.
9. Change `DEBUG` back to **0** and upload again, **without erasing flash or changing the partition scheme**. Saved bulb assignments survive a normal upload. Let the five-second setup window expire without sending anything; the ESP32 reconnects automatically.
10. Check `/bulbs` again, close Serial Monitor, and connect the board to the Home Assistant host. Only one program should own its serial port at a time.

To change assignments later, use the debug build, open Serial Monitor, reset the board, and send `s` during the five-second saved-configuration prompt. This rebuilds that board's bulb list; finish with `-1`, then return to `DEBUG=0`. Preserve existing names if you want to preserve Home Assistant routing and entities.

## 2. Install the Home Assistant integration

### HACS

Follow [HACS custom repository instructions](https://www.hacs.xyz/docs/faq/custom_repositories/): open HACS, use the three-dot menu → **Custom repositories**, add `https://github.com/ThreeMuskets12/govee-h6006-home-assistant-local`, and choose **Integration**. Search for **Govee H6006 Local (ESP32 Bulb Relay)**, download it, and restart Home Assistant.

### Manual installation

Copy this repository's `custom_components/esp32_bulb_relay` folder to your Home Assistant configuration directory:

```text
/config/custom_components/esp32_bulb_relay/manifest.json
/config/custom_components/esp32_bulb_relay/__init__.py
/config/custom_components/esp32_bulb_relay/...
```

Copy the entire integration folder and restart Home Assistant. The `firmware` folder does not belong in `/config/custom_components`; it is compiled and uploaded separately to the ESP32s.

## 3. Add ports and lights

1. Connect a provisioned ESP32 by USB to the Home Assistant host.
2. Open **Settings → Devices & services → Add integration → ESP32 Bulb Relay**.
3. Select the ESP32's serial port, then select which configured bulbs to expose as lights.
4. For additional boards, use **Configure → Add ESP32 Port** on the existing integration. Use **Manage Bulbs** to select enabled bulbs.
5. Test one light's power, brightness, RGB, and white-temperature controls.

The integration polls configured ports to rebuild a bulb-name-to-port map. If `/dev/ttyUSB0` and `/dev/ttyUSB1` swap, it can find the names again as long as both ports are configured. A completely new port path must be added. Duplicate bulb names on different boards collide; the last scanned port wins.

### USB access

On Home Assistant OS, attach the boards to the host and check **Settings → System → Hardware → All hardware** if ports are missing. When Home Assistant runs in a VM, pass each USB device through to the VM. For a container, map the devices explicitly, for example in Compose:

```yaml
devices:
  - /dev/ttyUSB0:/dev/ttyUSB0
  - /dev/ttyUSB1:/dev/ttyUSB1
```

Include every configured port and ensure the Home Assistant process has serial-device permissions. Avoid giving another serial monitor or service simultaneous access to these ports.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Blank first-time Serial Monitor; `/bulbs` never responds | Provision with `DEBUG=1`, 115200 baud, and Newline. Reset after opening the monitor. A blank board waits for interactive setup. |
| Bulb missing from scan | Keep it powered and nearby, close the phone app, and reset/re-run setup to scan again. The scan only stores the first 50 results. |
| Fourth bulb cannot connect | Default NimBLE builds allow three connections. Use another ESP32 rather than selecting a fourth with this build. |
| Bulbs unavailable just after reboot | Allow the startup/setup window and BLE reconnections to finish; then use **Rescan All Ports**. |
| USB port missing | Check the data cable, power, USB passthrough/permissions, and whether the path changed. Add any new path in Configure. |
| Commands fail intermittently | Check bulb power, BLE range, USB power, and competing controllers. Firmware reconnects automatically; backoff can take up to a minute between attempts. |
| Several light changes take time | Commands are queued with a 500 ms minimum interval per board. Reconnection adds further delay. |
| HA state differs from the bulb | State is optimistic; external power/color changes are not read back. |

Opening a serial connection can reset an ESP32. Provision and test it before handing the port to Home Assistant. Debug disconnect is temporary: the background monitor will try to reconnect the bulb.

## Serial protocol and Home Assistant actions

Requests are plain text terminated by a newline at **115200 baud**. Responses are one JSON object per line. The paths look like HTTP endpoints, but are sent over USB serial; there is no HTTP server in this firmware.

| Request | Purpose |
| --- | --- |
| `/bulbs` | List configured bulbs, including disconnected ones |
| `/bulb/desk_lamp/on` or `/bulb/desk_lamp/off` | Power |
| `/bulb/desk_lamp/brightness/75` | Brightness, 0-100; use `/off` to switch off |
| `/bulb/desk_lamp/rgb/r=255&g=80&b=0` | RGB, each channel 0-255 |
| `/bulb/desk_lamp/temperature/2700` | Temperature, 2000-9000 K |
| `/bulb/desk_lamp/connect` | Attempt reconnection |
| `/bulb/desk_lamp/disconnect` | Debug disconnect; monitor may reconnect |

Example responses (address is a placeholder):

```json
{"bulbs":[{"id":0,"name":"desk_lamp","address":"00:00:00:00:00:00","connected":true}],"count":1}
{"success":true,"action":"on"}
{"success":true,"action":"brightness","value":75}
{"error":"Bulb not found"}
```

Home Assistant exposes `esp32_bulb_relay.rescan_ports` and `esp32_bulb_relay.refresh_bulbs` for discovery/connectivity refresh. `esp32_bulb_relay.connect_bulb` and `esp32_bulb_relay.disconnect_bulb` take a `bulb_name` field for debugging. These are also available through the integration's configuration menus.

The BLE implementation writes 20-byte, XOR-checksummed Govee frames to characteristic `00010203-0405-0607-0809-0a0b0c0d2b11` in service `00010203-0405-0607-0809-0a0b0c0d1910`.

## Source layout and validation

- `custom_components/esp32_bulb_relay/`: Home Assistant config flow, serial API/queue, coordinator, and light entities.
- `firmware/govee_controller_serial/`: recovered ESP32 Arduino sketch.
- `firmware/PROVENANCE.md`: source selection and verification limits.

With the board core and library versions above installed:

```sh
arduino-cli compile --fqbn esp32:esp32:esp32 firmware/govee_controller_serial
python3 -m compileall -q custom_components/esp32_bulb_relay
python3 -m json.tool custom_components/esp32_bulb_relay/manifest.json
python3 -m json.tool custom_components/esp32_bulb_relay/translations/en.json
python3 -m json.tool hacs.json
```

Compilation does not verify radio behavior. End-to-end testing requires a flashed board, H6006 bulbs, and Home Assistant. The recovered sketch has no firmware version/hash query, so its exact match to already-flashed devices has not been established.

