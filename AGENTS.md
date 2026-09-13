# ESP32 Bulb Relay project guidance

## Scope and layout

- This repository contains the Home Assistant custom integration in
  `custom_components/esp32_bulb_relay/` and the recovered Arduino firmware in
  `firmware/govee_controller_serial/`. See `firmware/PROVENANCE.md` for source identity.
- Preserve the recovered sketch unless firmware changes are explicitly in scope. Setup prompts
  require `DEBUG=1`; normal operation uses `DEBUG=0`. Default NimBLE allows three connections,
  even though the sketch has four slots. Never flash deployed boards as part of documentation work.
- `api.py` owns the serial transport and one rate-limited command queue per port.
- `coordinator.py` owns port clients, 30-second polling, and the dynamic bulb-name-to-port map.
- `config_flow.py` stores serial ports in config-entry data and enabled bulb names in options.
- `light.py` exposes optimistic light state; `/bulbs` reports connectivity, not power/color state.

## Invariants and hazards

- Treat bulb names as routing and entity identifiers. Duplicate names across ESP32 devices collide;
  the last scanned port wins.
- Opening a serial port can reset an ESP32. Preserve bounded boot waits, buffer clearing, and read
  timeouts, and keep blocking serial enumeration off Home Assistant's event loop.
- Port failures are intentionally isolated: a failed scan must not make healthy ports unavailable.
- Control commands are serialized and rate-limited per port. Status polling bypasses the queue but
  still shares the API lock.
- Hardware-dependent behavior requires an ESP32 speaking the newline-delimited JSON protocol in
  `README.md`; do not claim end-to-end validation without that hardware.

## Validation

- Run `python -m compileall -q custom_components/esp32_bulb_relay` after Python changes.
- Validate every JSON file after metadata or translation changes.
- Keep `translations/en.json` aligned with config-flow steps, abort reasons, and services.
- Prefer focused unit tests with mocked serial streams for transport or coordinator changes.

