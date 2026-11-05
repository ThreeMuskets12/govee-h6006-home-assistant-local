# Root cause: one missing bulb degrades the other bulbs on its ESP32

**Bottom line.** The owner's symptom is real and is a firmware bug. While a bulb is missing, the
`BLEMonitor` task holds `bleMutex` for each 30 s connect attempt, about half of the time even in
"slow mode", and every write to a healthy sibling needs that mutex. A sibling command then waits up
to 9 s and may fail. This needs no command to the missing bulb. The integration adds a second layer
of damage by turning a stall into failed commands, misread replies and siblings dropping out of
routing.

**Evidence level.** Nothing here was run on hardware, and there is no field log of a sibling command
during an outage. Firmware verdicts come from reading the sketch and NimBLE source. Integration
verdicts were reproduced with the real `api.py`/`queue.py`/`coordinator.py` against a Python timing
model of the sketch (`tests/fake_esp32.py`). Percentages come from that model, not from a board.

References are to commit `98f5bb0` unless noted. `ino` = `firmware/govee_controller_serial/govee_controller_serial.ino`.
NimBLE lines are 2.5.2 (installed here); every default below was also checked in 2.3.7, the version
`firmware/PROVENANCE.md` records for the original build, and differences are noted.

## Read this first: three mismatches with the brief

1. **LivingRoomLamp is not on any board.** The 2026-11-04 backup lists 3 bulbs on each of 3 boards,
   none named LivingRoomLamp; it is also not in HA's enabled list, and its note says no board has
   reported it (or LeftFloor) since logs begin. So as configured today, firmware never tries to
   connect to it and it cannot be causing the symptom. Any other bulb on a board going dark triggers
   the same mechanism. All 3 boards are also full: pairing it as a 4th bulb hits F6 below.
2. **The NVS count key is `numBulbs`, not `count`** (ino:247, :263, :270). The firmware changes keep
   `numBulbs`; nothing in `saveConfig`/`loadConfig` was touched.
3. **The field evidence could not be re-checked.** `.analysis/evidence_latency.txt` is a 4-line
   summary, so the "all commands while every bulb connected" claim is taken from the brief. The
   backup also shows all three `ttyUSB2` bulbs connected at 16:24 on 11-04, so the bulbs powered off
   for 6 days were either back on by then or port numbers shifted.

## Library facts (read from source)

| Fact | Source |
| --- | --- |
| Default connect timeout is 30 000 ms | `NimBLEClient.cpp:69` (2.3.7: `:63`) |
| `connect()` blocks only the calling task; `asyncConnect` defaults to false | `NimBLEClient.h:57`, `NimBLEClient.cpp:297`, `NimBLEUtils.cpp:92` |
| Blocking wait is bounded by `(T + 350 ms) × 3` in 2.5.2 (0x3e retries), `T + 1 s` in 2.3.7 | `NimBLEClient.cpp:299`; 2.3.7 `:275` |
| An absent peer ends at about T (`ble_gap_connect` duration), so 30 s per attempt | `NimBLEClient.cpp:192-197` |
| Only one connect runs at a time (`BLE_HS_EALREADY`) | `ble_gap.c:7241` |
| Service/characteristic discovery waits forever on the task, limited by a 30 s GATT timeout per procedure | `NimBLEClient.cpp` `retrieveServices`, `ble_gattc.c:83` |
| Write without response never waits and has no dependency on GAP connect state | `NimBLERemoteValueAttribute.cpp:40`, `ble_gattc_write_no_rsp` |
| Max connections 3; `createClient()` returns `nullptr` when full | `nimconfig.h:225`, `NimBLEDevice.cpp:341` |
| `deleteClient()` on a live link is deferred, so the slot stays used until the disconnect event | `NimBLEDevice.cpp:358` |
| Host task is on core 0 at `configMAX_PRIORITIES - 4`; `loop()` is on core 1 | `nimconfig.h:213`, `nimble_port_freertos.c:40,56`, core `boards.txt:1990` |
| Link supervision timeout 2.56 s, initiator scan 10 ms/10 ms (100% duty) | `ble_gap.h:122`, `NimBLEClient.cpp:82-83` |

Task picture: `BLEMonitor` (core 0, priority 1, ino:1008-1016) sleeps inside `connect()`, so it costs
no CPU and the host task keeps running. Nothing in NimBLE blocks a write to a healthy bulb during a
connect. **The blocking comes entirely from the sketch's own mutex.**

## Firmware mechanisms

| # | Mechanism and code path | Worst case | Verdict | Explains symptom? | Fixed? |
| --- | --- | --- | --- | --- | --- |
| F1 | `CONNECT_TIMEOUT` defined (ino:28) and never used; `connect()` at ino:376 runs on the 30 s default | 30 s per attempt (not 5 s) | **CONFIRMED** | Yes, sets every duration below | Yes |
| F2 | `connectToBulb` takes `bleMutex` (ino:346) and holds it across `connect()` (ino:376) and discovery (ino:395, :415). Healthy writes wait `WRITE_TIMEOUT` × 3 (ino:530, :562-565), then return `Command failed` | Each hold 30 s (up to 91 s on 2.5.2 with 0x3e retries, plus 60 s for a half-alive peer). Sibling: 9 s then failure, with `loop()` frozen for all bulbs meanwhile | **CONFIRMED** | **Yes, primary cause.** Steady state, no command to the dead bulb needed | Yes |
| F3 | `writeWithRetry` calls `connectToBulb` inside `loop()` (ino:555 ← :877), stalling serial and keepalives for the board | 3 × (30 + 0.5) = **91.5 s** in fast mode | **CONFIRMED** | Yes, episodic. HA skips unavailable entities (`service.py:792`), so this needs a command in the ≤30 s before the next poll marks the bulb down, or `connect_bulb`. Typical trigger: wall switch off, then a scene | Yes |
| F4 | Slow mode stamps `lastReconnectAttempt` when an attempt **starts** (ino:352) and gates on 60 s (ino:897), so the 30 s attempt is counted inside the "quiet" minute | Cycle 60.5 s, mutex held **49.6%** (fast mode 84.5%; two dead bulbs 99%) | **CONFIRMED** | Yes, slow mode does not deliver the quiet it implies | Yes |
| F4b | `now` read once per pass (ino:887) but used after earlier connects; `now - last` underflows if `loop()` stamped a later attempt | One early attempt | **CONFIRMED**, minor | No | Yes |
| F5a | Keepalives skipped: 100 ms mutex wait (ino:601), and `lastKeepAlive` advances regardless (ino:1053) | 10 skipped keepalives per healthy bulb per 30 s hold; `loop()` loses 100 ms per bulb per 3 s | **CONFIRMED** | Partly (small serial lag) | Yes |
| F5b | Healthy H6006 drops the link after those skips, cascading into more reconnects | Unknown | **SUSPECTED**: no data on the bulb's keepalive tolerance | Would make it much worse | Skips removed, drop untestable |
| F6 | `createClient()` `nullptr` is dereferenced (ino:373-374). Reached by a 4th bulb (`MAX_BULBS` 4 vs NimBLE 3), or on a **full 3-bulb board** when reconnecting a live link (after a failed write, ino:584, or `/connect`) because the delete is deferred | Panic and reboot, dropping all bulbs on the board | **CONFIRMED** latent; no reboot evidence in the field | Not the usual path | Null guard only; limit stays 3 |
| F7 | `onDisconnect` clears `writeChar` with no mutex (ino:157) while writers checked it before waiting on the mutex (ino:531 → :573, :596 → :605) | Null deref and reboot; narrow window | **CONFIRMED** race; no reboot evidence | Not the usual path | Yes |
| F8 | Scan interaction | n/a: scanning only runs in setup (ino:990), before `BLEMonitor` exists | **RULED OUT** | No | n/a |
| F9 | Initiator scanning at 100% duty contends for the radio with healthy links | Not measurable: controller is a closed binary | **SUSPECTED** | Unknown | Reduced only as a side effect (49.6% → 24%) |

## Integration mechanisms

Measured on virtual time at the firmware's real durations (`pytest -s`). "Before" is the same test
against an untouched copy of `98f5bb0`.

| # | Mechanism and code path | Before | After | Verdict | Explains symptom? |
| --- | --- | --- | --- | --- | --- |
| I1 | Sibling queues behind a dead-bulb command (`queue.py:73-98`, lock at `api.py:259`) | Sibling fails at **41.1 s**, and is still written to the stalled board | Fails at 21.2 s, never written | **CONFIRMED** | Yes, amplifies F3 |
| I2 | `COMMAND_TIMEOUT` 15 s (`const.py:20`) < firmware 91.5 s, so HA gives up while the command is still queued on the board | Timed-out commands run minutes later | Commands stop piling up behind it | **CONFIRMED** | Yes, with F3 |
| I3 | Late replies read as the next command's: no IDs, and `_clear_buffer` only removes what has already arrived (`api.py:264`, `:293-325`). Needs a stall over 15 s | A successful sibling command reported as failed; `/bulbs` returns `[]`, emptying the board | Correct replies | **CONFIRMED** (reproduced) | Yes, after F3 |
| I4 | Any failure rescans every port and retries (`coordinator.py:298-316`), even `Command failed` from a bulb that has not moved | Fast failure 0.72 s, one extra poll per port; timeout path 20.5 s | 0.11 s, none; 15.2 s, none | **CONFIRMED** | Yes, amplifier |
| I5 | **Not in the brief:** a failed scan returns `[]` (`coordinator.py:135-138`), so `:171-172` deletes every bulb on that port. Contradicts `coordinator.py:204` and `light.py:111-114` | One missed poll makes all 3 siblings unavailable and unroutable. Sibling error: `not found on any configured port` | Kept for up to `MAX_MISSED_SCANS` (3) | **CONFIRMED** (reproduced) | **Yes, this is the "unresponsive" part** |
| I6 | Polling shares the lock; wait is outside the timeout (`api.py:259`). Ports scan serially (`coordinator.py:150`) | Poll delayed up to about 16 s, later ports wait | Unchanged | **CONFIRMED** as a delay | No. It also means a poll cannot time out during a 9 s sibling stall |
| I7 | Blocking the HA event loop | n/a: serial I/O is awaited, enumeration uses an executor | Unchanged | **RULED OUT** | No |

Steady state with a missing bulb (no commands to it), sweeping a sibling command across one
reconnect cycle, n=121, through the real integration:

| Firmware model / integration | Failed | Over 1 s | Max |
| --- | --- | --- | --- |
| Original / original, 1 dead bulb | 19% | 48% | 18.7 s |
| Original / original, 2 dead bulbs | 69% | 98% | 18.7 s |
| Original / new, 1 dead bulb | 19% | 48% | 18.7 s |
| **Fixed / new, 1 dead bulb** | **0%** | **0%** | **0.11 s** |

The first row matches the hand calculation: 30 s hold in a 60.5 s cycle, with two 9 s waits, fails
11.1/60.5 = 18%. The fixed row shows what the model assumes; it needs confirming on a board.

**Consistency with the field data.** Healthy-path latency (median 11 ms) is untouched by any of
this. `/bulbs` never takes `bleMutex` (ino:716-720), so monitor holds alone cannot delay polls, which
fits a board with 3 dark bulbs missing only 2 polls in 6 days. Those 2 need a `loop()` stall, a board
reset or a USB fault, and the summary cannot show which.

## Fixes

**Firmware.** `connectMutex` now serializes connection attempts; `bleMutex` is held only for short
state changes and non-blocking writes. `setConnectTimeout(CONNECT_TIMEOUT)` is applied. `writeWithRetry`
no longer connects, so a bulb that is down fails immediately and `BLEMonitor` owns reconnecting.
`RECONNECT_SLOW_INTERVAL` goes from 60 s to 20 s: with 5 s attempts, keeping 60 s would have made a
returning bulb wait longer than today. Duty falls from 49.6% to 24%, and the longest gap between
attempts from about 30.5 s to about 15.5 s. Also: `createClient` null guard, `writeChar` read once
under the mutex, `now` taken per bulb. `MAX_BULBS` stays 4, since lowering it would overflow
`bulbs[]` on a board with 4 saved.

**Integration.** Timeouts set a resync flag. The firmware answers in order, so a `/bulbs` reply
proves everything before it was answered: the next control command sends `/bulbs` first and is not
written if that times out. Readers skip replies of the wrong shape. Rescans now happen only for
`Bulb not found` and transport errors. A bulb last reported connected keeps one same-port retry,
without which failures on original firmware rose from 19% to 35% in testing. Failed scans keep the
port's bulbs for 3 misses (about 90 s, spanning the 91.5 s stall).

**Not fixed by choice.** `COMMAND_TIMEOUT` stays 15 s: raising it to 91.5 s would hold siblings
behind a dead bulb for longer. I6 is unchanged because there is nothing left for it to wait on once
the stall is gone.

**Compatibility.** Protocol strings and JSON are unchanged, and `/bulbs` as a barrier works on both
firmwares. Original firmware + new integration and fixed firmware + original integration were both
exercised against the model. The original integration still rescans and retries on fixed firmware
(0.72 s), but works.

**Device removal.** `async_remove_config_entry_device` queries the boards at removal time, refuses
if any board reports the bulb (connected or not), and drops it from `CONF_BULBS` if present. Two
decisions beyond the brief:
- It refuses while any board is not answering, because that board could hold the bulb. A dead port
  must be removed from the options before its devices can be deleted.
- Changing options normally reloads the entry, which reopens every serial port and can reset the
  ESP32s. `_async_options_updated` now returns early when nothing is left to apply, so deleting a
  device does not bounce the boards. Real option changes still reload.

No new translation strings were needed.

## Separate finding, fixed: board can hang in setup at boot

**CONFIRMED by reading the code; unrelated to the symptom, left alone as out of scope.** At boot the
sketch watches serial for 5 s for `s` to enter setup (ino:962-972). That check reads single
characters, and **`/bulbs` contains an `s`**. A poll landing in that window drops the board into
`selectBulbs()`, which waits on serial with `numBulbs = 0` (ino:990-993). HA's 30 s poll hits a 5 s
window about 1 boot in 6. The board then stops answering until reset. NVS looks safe, because
`saveConfig` needs `-1` or 4 selections, which HA's traffic will not produce.

**Fixed:** the 5 s `s` window now only runs in `DEBUG=1` builds. A `DEBUG=0` board with saved
assignments goes straight to connecting; one with no saved config still runs setup. Stop the
integration while flashing anyway, because it holds the serial port.

Also noted: `api.py:9` imports `serial_asyncio`, but the manifest requires `pyserial-asyncio-fast`,
which provides `serial_asyncio_fast`. The import fails in a clean environment, so the deployed HA must
be getting it from another package. It works today but is fragile.

## Summary

**CONFIRMED:** F1, F2, F3, F4, F4b, F5a, F6, F7, I1, I2, I3, I4, I5, I6 (delay only).
**SUSPECTED:** F5b (bulb drops after missed keepalives), F9 (radio contention).
**RULED OUT:** F8 (runtime scanning), I7 (HA event loop blocking).
**Primary cause:** F2 with F1 and F4. **Worst amplifier:** I5.

**Files changed** (uncommitted, on `fix/isolate-missing-bulb`):
- Integration: `__init__.py`, `api.py`, `const.py`, `coordinator.py`, `light.py`
- Firmware: `govee_controller_serial.ino`, `PROVENANCE.md`
- Docs: `docs/setup.md`
- New: `tests/conftest.py`, `tests/fake_esp32.py`, `tests/test_dead_bulb_isolation.py`, `tests/test_device_removal.py`

**Tests.** `python -m pytest tests` needs `pytest` and `voluptuous`; HA itself would not install
(`lru-dict` build failure), so it is stubbed, and HA's device-removal and listener behaviour was
checked by reading the 2026.2.3 source instead.
- Working tree: **25 passed**.
- Same isolation tests against `98f5bb0`: **9 failed, 7 passed**, which are the reproductions above.
- `python -m compileall`: clean. All JSON files valid, translations unchanged.

**Firmware compile.** Use **core esp32:esp32 3.3.5 with NimBLE-Arduino 2.3.7** (`huge_app`). That is
the toolchain the previously deployed binary was built with (IDF v5.5.1, matched segment by segment).
Builds from core 3.3.12 flashed fine but never connected to the bulbs, so do not use them.
Early compile checks on core 3.3.12, kept for reference:

| Build | Flash | RAM |
| --- | --- | --- |
| `DEBUG=0`, NimBLE 2.5.2 | 607,516 | 37,452 |
| `DEBUG=0`, NimBLE 2.3.7 | 604,736 | 37,452 |
| `DEBUG=1`, NimBLE 2.5.2 | 613,460 | 37,452 |
| Original sketch, NimBLE 2.5.2 | 607,372 | 37,444 |

The only sketch warning with `--warnings all` is an empty `if` body from `DEBUG_PRINT` on a line not
changed here. No `Serial.print` or NVS line changed.

**Residual risks, especially for a bulb power-cycled several times a day**

- **Hardware result (2026-11-04, one board, one slot pointed at a dead address).** Original firmware:
  the healthy sibling Sky2 never reconnected during a 4.5 min window, and commands to Sky1 stalled
  24.8 s about once a minute (median 0.21 s). Fixed firmware, same setup: both siblings available,
  60/60 commands OK, median 0.215 s, max 0.308 s. All three boards now run the fix with pairings
  unchanged.
- **The integration alone does not fix the symptom.** It limits the damage, but the steady-state
  19%/48% row stays until the firmware is flashed.
- **New behaviour: commands during reconnect fail immediately.** Previously the command could wait
  and be delivered. A scene that fires just as a bulb returns, or a brief link drop on a powered bulb,
  can now miss that bulb, because nothing re-sends it. This is the main trade-off.
- **Time to usable after power-on:** up to about 15.5 s until the next attempt, plus connect time, plus
  up to 30 s until HA's next poll, so roughly 50 s at worst.
- **HA state can be wrong after a power cycle.** The bulb restores its own last state and `/bulbs`
  reports only connectivity, so HA keeps what it last sent.
- **Commands can falsely succeed for up to 2.56 s after power-off.** Writes have no acknowledgement,
  so the firmware reports success until the link supervision timeout fires.
- **`CONNECT_TIMEOUT` of 5 s has never been exercised on an H6006.** If the bulbs advertise slowly,
  attempts will fail more often and reconnect will take longer. It is a single constant to tune.
- **Attempt rate triples** (about 1,430 to 4,200 a day per missing bulb), each creating and deleting a
  client. Heap effects over weeks are unmeasured; the 6-day outage only covers the old rate.
- **F9 is unmeasured.** A dead bulb still runs the initiator 24% of the time, and two dead bulbs on one
  board double that.
- **Bulbs on an unplugged board stay "available" for about 90 s** because of the missed-scan grace.
- **Debug `/connect` can still block `loop()`** for about 10 s (5 s waiting for an attempt in progress
  plus 5 s for its own), longer with 0x3e retries on 2.5.2. `/disconnect` can block for up to 5 s. HA
  only sends these from the debug services and options menu.
- **A 4th bulb on a board will not work.** It no longer crashes on a null client, but it stays
  disconnected, or takes a slot from a sibling that drops. LivingRoomLamp needs a free slot.
