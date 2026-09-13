# Firmware provenance

The sketch in `govee_controller_serial/` was recovered on 2026-09-12 from the
owner's `FinalGovee` Arduino project. It is an unchanged copy of the newest
matching source found, last modified **2026-02-12**. No PlatformIO project for
these bulbs was found in the Desktop, Downloads, and Documents search.

Fifteen matching sketches were identified by their H6006 protocol code, BLE
service/write UUIDs, packet builders, and interfaces, rather than filenames alone:

- Two early ESP32 BLE + Wi-Fi/HTTP prototypes, including a misleadingly named
  `SettingsAndStatus` sketch.
- Eight NimBLE + Wi-Fi/HTTP variants, including `Final`, debug, and scan/API experiments.
- Four older USB-serial variants (`serial`, `serial2`, `serial3`, `serial4`).
- The selected `FinalGovee` USB-serial sketch, the only candidate with slow reconnect backoff.

The January 31 `serial4` sketch introduces quiet production output. The February
12 source retains that behavior and adds reconnect tracking, three fast attempts,
a one-minute backoff, and a five-second background connection check. Its
115200-baud, newline-terminated URI commands and JSON responses match the
`esp32_bulb_relay` integration. Arduino's recent-project metadata also lists
`FinalGovee` as the most recently opened Govee project.

## SHA-256 identities

These hashes identify the original files; timestamps alone do not establish a version.

| Source | Modified | SHA-256 |
| --- | --- | --- |
| govee_controller_serial | 2026-01-27 | `89c1e0b83e5a1858fc019cfb867c8959511badf12d40b92a69a21eda6a3f8cfd` |
| govee_controller_serial2 | 2026-01-27 | `7ac2aca1e557ab7e63f4b6f1c2c16633df50d3def5208e16d3a56e7046502643` |
| govee_controller_serial3 | 2026-01-27 | `c607a2a86934c5c3ec510ba193b4dfc0a010dad23d3018f7a6a662335a20347b` |
| govee_controller_serial4 | 2026-01-31 | `fb292f96b816e399d9d949dc163b325d3634ac049f2808aab4e4d593dac30e00` |
| FinalGovee / selected | 2026-02-12 | `e1f7a30ae17f4781d5bcf169fffb19ceb828b0decc297e16796feb9694ca40b2` |

## Verification and limits

- Read-only inspection of the owner's Home Assistant found `esp32_bulb_relay`
  loaded and connected lights on three USB ports. The old `govee_light_ble`
  integration was not installed as a config entry.
- The unchanged production sketch compiled for `esp32:esp32:esp32` using
  Espressif Arduino core **3.3.5** and NimBLE-Arduino **2.3.7**: 610,603 bytes
  of program storage and 36,040 bytes of static RAM.
- A temporary `DEBUG=1` provisioning copy also compiled with the same toolchain:
  616,259 bytes of program storage and 36,048 bytes of static RAM.
- First-time setup requires `DEBUG=1`; the original production source uses
  `DEBUG=0`. See the root README for the two-upload provisioning procedure.
- NimBLE's default connection limit is three, despite four slots in the sketch.
- No boards were flashed or disconnected during this recovery. The firmware has
  no version/hash endpoint, so the exact binary currently on each ESP32 is
  **not verified**. This is the best-supported latest source candidate, not a
  byte-for-byte readback from the deployed hardware.

Older local sketches were left in place. Wi-Fi experiments and local device
configuration were not imported into this repository.
