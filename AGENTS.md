# ESP32 Bulb Relay project guidance

## Scope and layout

- This repository contains the Home Assistant custom integration in
  `custom_components/esp32_bulb_relay/` and the recovered Arduino firmware in
  `firmware/govee_controller_serial/`. See `firmware/PROVENANCE.md` for source identity.
- Preserve the recovered sketch unless firmware changes are explicitly in scope. Setup prompts
  require `DEBUG=1`; normal operation uses `DEBUG=0`. Default NimBLE allows three connections,
  even though the sketch has four slots. Never flash deployed boards as part of documentation work.
- `api.py` owns the serial transport and one rate-limited command queue per port.
- `coordinator.py` owns port clients, 30-second polling, the dynamic bulb-name-to-port map, and
  coordinated recovery of troubled boards.
- `config_flow.py` stores serial ports in config-entry data and enabled bulb names in options.
- `light.py` exposes optimistic light state, restored across restarts from extra restore data with
  the plain last state as a fallback; `/bulbs` reports connectivity, not power/color state.

## Invariants and hazards

- Treat bulb names as routing and entity identifiers. Duplicate names across ESP32 devices collide;
  the last scanned port wins.
- Opening a port does not reset these boards: pyserial's default DTR=1, RTS=1 is a run state,
  confirmed on hardware. On the classic auto-reset circuit DTR=0, RTS=1 holds EN low, DTR=1, RTS=0
  pulls IO0 low (never pass through that state), and DTR=0, RTS=0 runs. Preserve bounded boot waits,
  buffer clearing, and read timeouts, and keep blocking serial work off Home Assistant's event loop.
- The boards share a marginal 5 V supply (suspected from field logs, not measured): overlapping
  boots can sag it enough to reset each other in a self-sustaining loop that floods every port.
  Per-port resets don't break that loop, because the other boards keep it going. Recovery therefore
  holds every troubled board in reset and releases them strictly one at a time (`coordinator.py`,
  `_async_recover`), in a background task. Never leave a board held: every path out must release.
- Sending `/bulbs` too soon after a boot can drop older or debug builds into setup, as it contains an
  `s`; `RECOVERY_BOOT_WAIT` covers that prompt.
- A flood is never a busy board, so it starts a recovery at once, including at startup. Plain silence
  stays gated by missed scans and `RESET_MIN_SILENCE`, because a board with a dead bulb can stall
  for over a minute. Healthy, answering ports are never touched.
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

