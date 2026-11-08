# STATUS — Shelly BLE RPC investigation

Last reviewed: 2026-11-08. Integration version: 2.1.4, uncommitted.

This document separates current observations, source findings, reported history,
and hypotheses. It supersedes the earlier diagnosis that a stale proxy bond was
established as the cause. A stale proxy GATT handle cache has now been demonstrated. After clearing it,
complete `Shelly.GetDeviceInfo` RPC responses were verified on two independent
BLE connections through the ESPHome proxy.

Evidence labels:

- `[live]`: observed against the devices from this workspace on 2026-11-08.
- `[source]`: checked against repository or linked upstream source.
- `[sandbox]`: exercised locally using a limited test or stub.
- `[reported history]`: retained from the previous investigation log; the
  underlying user logs/build configuration have not been revalidated here.
- `[hypothesis]`: possible explanation, not an established cause.

## 1. Current result

Both devices are reachable from this workspace. The Shelly HTTP RPC endpoint
works, and the ESPHome encrypted native API accepts the supplied key. The key is
not recorded in this file. `[live]`

The proxy's Bluetooth controller MAC exactly matches a bond entry on the Shelly.
That confirms controller identity, not that matching keys exist on both devices
or that a BLE connection is encrypted. `[live]`

The initial probes reproduced ATT 0x0D using a stale UUID-to-handle map. Later
cache invalidation corrected the map, and two complete end-to-end BLE RPC
transactions succeeded, including disconnect/reconnect between them. See the
follow-up below. `[live]`

The integration's four-byte big-endian TX framing was accepted after clearing
the stale proxy GATT cache. Its pairing recovery has concrete implementation
issues recorded in section 5; v2.1.4 replaces that recovery as described below.

## 2. Device and environment evidence

| Item | Observation | Evidence |
| --- | --- | --- |
| Shelly HTTP | `10.10.124.225:80` responds to RPC; HTTP authentication disabled | `[live]` |
| Shelly identity | `shelly1pmg4-a085e3bdb084`, model `S4SW-001P16EU`, Gen4 | `[live]` `Shelly.GetDeviceInfo` |
| Shelly firmware | `2.1.1`, build `20261026-151238/2.1.1-g07e9ad2` | `[live]` |
| Shelly BLE | RPC enabled; address `a0:85:e3:bd:b0:86,p`; status flags include `advertising` | `[live]` `BLE.GetConfig`, `BLE.GetStatus` |
| Shelly bond list | One entry: `30:ae:a4:05:8e:f6,p` | `[live]` `BLE.ListPairedDevices` |
| ESP native API | `10.10.124.36:6053` reachable; encrypted login succeeds | `[live]` |
| ESP identity | `esp32-bluetooth-proxy-058ef4`, friendly name `kitchenesp`, model `esp32` | `[live]` |
| ESP firmware | ESPHome `2026.10.2`, compilation time `2026-11-08 07:20:29 +0200` | `[live]` `device_info()` |
| ESP MACs | Network `30:AE:A4:05:8E:F4`; Bluetooth `30:AE:A4:05:8E:F6` | `[live]` |
| ESP capabilities | Bluetooth proxy feature flags `255` | `[live]` |
| ESP other ports | TCP 3232 reachable; TCP 80 refuses connections; advertised webserver port is 0 | `[live]` |
| ESP-IDF version | Previously reported 5.5.6, then rebuilt with 5.5.5; not independently verified on running firmware | `[reported history]` |
| HA host | Previously reported Raspberry Pi, HA 2026.10.4, local hci0 `DC:A6:32:08:96:0A` | `[reported history]` |
| Historical signal | Reported RSSI about −84 to −100 dBm; no current RSSI measurement established here | `[reported history]` |

A 12-second decoded advertisement subscription did not observe the Shelly.
That is a limited negative observation: it does not establish that the device
is absent or out of range, especially without validating raw-advertisement
subscription behavior and allowing a longer observation window. `[live]`

Direct BLE probe: `aioesphomeapi` 46.28.0 in `/tmp/shelly-review-env`, Shelly
public address type 0, proxy feature flags passed from device info. Failure:
`TimeoutAPIError: Timeout waiting for connect response ... after 15s,
disconnect timed out: True ... after 20.0s`. This is a transport-stage failure;
range, proxy availability/connection slots, concurrency, and firmware behavior
remain possible explanations. No bonds were deleted, relay outputs changed, or devices rebooted by that first
probe. Explicit pairing was subsequently requested in the follow-up below. `[live]`

### Follow-up: explicitly authorized pairing test

The user requested a real pairing attempt through the proxy. `[live]`

- Encrypted API login succeeded; the proxy initially reported all **3 of 3**
  connection slots free.
- A raw-advertisement subscription observed the Shelly, public address type 0,
  RSSI approximately **−87 to −101 dBm**. This resolves the earlier short decoded
  subscription's lack of sightings; it was not evidence of absence.
- The proxy connected successfully, reporting **MTU 247**, error 0.
- `bluetooth_device_pair()` returned **`paired=True, error=0`**. This establishes
  API-reported security-procedure success; it does not distinguish reuse of an
  existing bond from generation of fresh keys. No unpair was requested.
- GATT discovery confirmed data handle **3**, RX-control handle **6**, and
  TX-control handle **9**. The returned characteristic list did not include the
  previously hypothesized JWT characteristic.
- The read-only `Shelly.GetDeviceInfo` RPC attempt failed at its initial
  four-byte big-endian TX-control write, with write response requested:
  **`handle=9 error=13 description=Invalid attribute length`**.
- The test explicitly disconnected the BLE link and API client afterward.

This reproduces the failure after API-reported pairing success on the same
connection. It weakens any claim that simply calling PAIR is sufficient. Actual
security events, key reuse, characteristic write semantics, and Shelly firmware
behavior remain investigation targets. No bond deletion, device reboot, or relay
operation was performed.

### Investigation: fresh bond and stale GATT cache

Subsequent user-authorized diagnosis established the following. `[live]`

1. Explicit pairing mode was enabled via `BLE.StartPairing?timeout=90` and
   stopped after each trial. Merely enabling pairing mode did not fix RPC.
2. A targeted fresh-bond test confirmed proxy UNPAIR `success=True, error=0`,
   deleted only the matching ESP controller entry from the Shelly, verified the
   Shelly list was empty, enabled pairing mode, and paired again. PAIR succeeded
   in **1.45 seconds**, and a new Shelly bond entry appeared. The same initial
   four-byte write to handle 9 still returned 0x0D. This rules out an old bond
   as a sufficient explanation for that failure.
3. Writes without response did not establish a working RPC transaction; TX
   control advertised only the WRITE property, and sending the following data
   to the old handle 3 returned Write Not Permitted. Varying the control write
   length was diagnostic only, not a proposed protocol change.
4. Reading the alleged data handle **3** returned the Bluetooth device name.
   Reading alleged RX control **6** returned the RPC service UUID bytes. The
   cached UUID-to-handle map therefore did not describe the current device.
5. A targeted `bluetooth_device_clear_cache()` returned `success=True, error=0`.
   On a subsequent successful connection, discovery reported:

   | Attribute | Stale cached handle | Rediscovered handle |
   | --- | --- | --- |
   | GAP device name | 12 | 3 |
   | RPC service | 1 | 6 |
   | RPC data | 3 | 8 |
   | RPC RX control | 6 | 11 |
   | RPC TX control | 9 | 14 |

   Handle **9** in the actual database is the data characteristic's **CCCD
   descriptor** (`0x2902`), not TX control. A four-byte write to this two-byte
   descriptor explains Invalid Attribute Value Length without invoking bond
   corruption. The prior UUID resolution was based on stale discovery data.
6. Using rediscovered handles **14/8/11**, the standard TX write succeeded,
   request data was accepted, and RX control returned **`00000149`**, indicating
   a **329-byte response** to `Shelly.GetDeviceInfo`. That connection dropped
   during response reading with reason 0x08/error 133. A complete response
   was verified in the next two trials; weak radio conditions remain an
   independent concern.

7. **End-to-end verification passed twice** on separate subsequent connections.
   Each reused the new bond (pairing mode closed), rediscovered the correct
   handles **14/8/11**, and received a complete **328-byte JSON response** to
   `Shelly.GetDeviceInfo`, read as **244 + 84 bytes**. Response IDs matched the
   requests (1 and 2), and identity/firmware matched HTTP results. Both links
   were explicitly disconnected afterward. The successful follow-up sent the
   request payload in a single write within MTU 247; the earlier accepted trial
   used 20-byte request chunks. This validates the transport path and read-only
   RPC, not every Home Assistant entity or command.

**Current outcome:** standalone RPC through the ESPHome proxy works. Reload the
Home Assistant ESPHome proxy entry and this integration so client-side cached
services, if any, are refreshed. That HA path has not been exercised from here;
it must rediscover UUIDs rather than hard-code the observed handles. Automatic
recovery should invalidate stale GATT cache when indicated, rather than assume
ATT 0x0D requires deleting bonds.

The exact event that made the old cache stale is unknown; a firmware GATT-layout
change is a candidate, not established history. Clearing GATT cache preserves
bonds and is distinct from unpairing. No relay outputs were changed or devices
rebooted. The temporary Shelly debug level was restored to its original value 2.
The new ESP–Shelly bond remains stored.

A separate standalone-probe issue was also identified: the proxy routes BLE
operations through its advertisement subscriber and tears down links if that
subscriber is absent. Two diagnostic scripts omitted that subscription, so their
connection timeouts must not be assigned to range or device failure. Subsequent
probes subscribed to raw advertisements. Home Assistant normally maintains this
subscription; this omission was in the standalone test harness.

## 3. Protocol: what is established and what is not

The configured UUIDs match the documented Shelly RPC GATT interface:

- Service: `5f6d4f53-5f52-5043-5f53-56435f49445f`
- Data: `5f6d4f53-5f52-5043-5f64-6174615f5f5f`
- TX control: `5f6d4f53-5f52-5043-5f74-785f63746c5f`
- RX control: `5f6d4f53-5f52-5043-5f72-785f63746c5f`

Writing a four-byte big-endian request length, followed by JSON data, then
reading the response length and response data is consistent with
[Shelly's BLE RPC documentation](https://kb.shelly.cloud/knowledge-base/kbsa-communicating-with-shelly-devices-via-bluetoo)
and its [reference client](https://github.com/ALLTERCO/Utilities/blob/master/shelly-bluetooth-rpc/shelly-bt-rpc.py).
This supports the overall design; it does not prove compatibility with this
firmware, the resolved handles, write properties, or response timing. A fixed
0.2-second delay is an implementation choice, not a verified protocol guarantee.

ATT 0x0D means **Invalid Attribute Value Length**. A missing or unusable encrypted
link is a hypothesis for the reported Shelly failures, not the standard meaning
of this code. Standard ATT security errors distinguish insufficient authentication
(0x05) and insufficient encryption (0x0F). The earlier text incorrectly described
an encryption-only requirement as necessarily producing 0x05. See the Bluetooth
[ATT error codes](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core-61/out/en/host/attribute-protocol--att-.html).

The previous log reported that an upstream Mongoose OS `rpc-gatts` handler does
not emit 0x0D. Even if correct for that source revision, it does not identify the
producer in proprietary Shelly Gen4 firmware or exclude a length/handle problem.
The exact deployed handler has not been established. `[reported history]`

A JWT characteristic was previously mentioned. Its presence, purpose, and any
requirement on this device remain unverified. Failure at TX control does not by
itself exclude an authentication prerequisite. `[hypothesis]`

## 4. ESPHome pairing semantics and ESP-IDF regression

Checked against ESPHome **2026.10.2** source, matching the reported API version.
The live proxy logs identify the newer `bluetooth_connection` backend; the older
`BLEClientBase` source below is background, not proof of its active implementation:

- [BLEClientBase](https://github.com/esphome/esphome/blob/2026.10.2/esphome/components/esp32_ble_client/ble_client_base.cpp)
  resets `paired_` to false when connecting. `pair()` invokes
  `esp_ble_set_encryption(..., ESP_BLE_SEC_ENCRYPT)`; successful authentication
  completion sets `paired_` true. `[source]`
- The [proxy PAIR handler](https://github.com/esphome/esphome/blob/2026.10.2/esphome/components/bluetooth_proxy/bluetooth_proxy.cpp)
  calls `pair()` when `is_paired()` is false and reports success directly when
  it is true. **A stored NVS bond alone is not shown to set this flag.** A quick
  response is compatible with an already authenticated connection or successful
  reuse of existing keys; elapsed time alone cannot distinguish these cases
  from a faulty state. `[source]`
- UNPAIR calls `unpair_device`; **only on success** does it clear the live
  connection's paired flag. The API returns a success boolean and error code.
  A subsequent PAIR can request security again, but a fresh successful SMP
  exchange and usable keys are not guaranteed. `[source]`

[ESPHome issue 21880](https://github.com/esphome/esphome/issues/21880) reports
reconnect encryption failure associated with IDF 5.5.6, recoverable by a proxy
reboot without re-pairing. The linked
[ESP-IDF proposed fix 19322](https://github.com/espressif/esp-idf/pull/19322)
describes in-memory keys being cleared during reuse of an existing bond while an
NVS bond entry can remain. These sources do **not** establish that all bonds are
deleted from persistent storage on every disconnect. The proposed fix was open
when checked. They also do not establish this Shelly's root cause.

The active backend's
[Bluedroid implementation](https://github.com/esphome/esphome/blob/2026.10.2/esphome/components/bluetooth_connection/bluetooth_connection_bluedroid.cpp)
also invokes `esp_ble_set_encryption()` for PAIR and forwards authentication
completion to the [connection wrapper](https://github.com/esphome/esphome/blob/2026.10.2/esphome/components/bluetooth_connection/bluetooth_connection_hub.cpp),
which sets `paired_` from that result. `[source]`

A normal reflash may preserve NVS, but persistence does not imply stale keys.
The earlier chain “5.5.6-era stale bond → instant PAIR → definitely unencrypted”
is unsupported. A downgrade to 5.5.5 is a reported workaround worth testing,
not a guarantee of successful pairing or a reason to require bond deletion.

## 5. Pre-fix implementation findings (resolved in v2.1.4)

These findings were established before the code fix. The old implementation
is described here for context; see section 10 for its replacement.

1. **Nested recovery exceeds the RPC deadline.** `call()` wraps the entire
   `_call_connected()` in `RPC_TIMEOUT = 20`. Within it, recovery can wait up to
   20 seconds for the first pair, sequential unpair requests (default 30-second
   API timeout per proxy), then another 20-second pair and the RPC retry. The
   outer deadline can cancel recovery mid-procedure, including after bond
   removal. Separate recovery and ordinary RPC time budgets.
2. **Unpair success is not checked.** `async_forget_proxy_bonds()` ignores the
   returned `BluetoothDeviceUnpairing.success` and increments its confirmation
   count whenever the await returns. A local stub returning `success=False`
   reproduced a result of **1 confirmed removal**. Error responses must not be
   logged as confirmed bond removal. `[sandbox]`
3. **Pairing is attempted for every initial TX-control write exception.**
   `_call_connected()` and `call_no_response()` call `_bond_active_connection()`
   regardless of `is_write_rejected_error()`; that helper currently controls
   logging only. Transport failures can therefore initiate pairing too.
4. **`pair_on_connect` does not govern behavior.** It is stored and assigned,
   but never read to gate pairing. Existing-connection validation sets it false
   yet can still pair after a write error. The unpair button's assignment also
   cannot enforce its stated policy by itself.
5. **Timing is used as a destructive recovery trigger.** A pair taking less
   than 0.4 seconds can trigger unpair before retrying the protected write.
   Timing is not proof of a stale bond or missing encryption. Check actual RPC
   behavior/security evidence before deleting a bond.
6. **Recovery targets every eligible loaded proxy.** It does not identify the
   backend serving the active connection. It may remove valid bonds held by
   other controllers for the same Shelly and adds sequential timeout exposure.
   Prefer the controller serving the connection.

The `allow_proxy_recovery` flag does gate proxy UNPAIR in this client and is
restored in a `finally` block. It does not make all pairing behavior opt-in,
validate unpair success, or establish that the selected proxy is the active one.
`[source]`

The old claim “only exact 0x0E causes recovery that removes bonds” is inaccurate:
explicit pairing flows also enable proxy removal, and the 0x0E detector matches
text such as `unlikely error`, not a verified numeric status alone. That legacy
setup branch marks pairing required and attempts pairing; it does not itself
explicitly delete Shelly or BlueZ bonds. `[source]`

## 6. Historical observations and their limits

The previous investigation records the following; original field logs were not
rechecked in this review:

- Local BlueZ operation reportedly worked with earlier hardware/firmware.
  This is not a known-good baseline for this exact Gen4 firmware.
- v2.1.1 reportedly fixed the HA config-flow progress transition error.
- v2.1.2 added pair-on-open-connection after an initial TX-control write error.
- The 2026-11-08 08:13–08:17 run reportedly showed repeated initial TX-control
  ATT 0x0D failures, 6–9 ms pair responses, and one roughly 2.755-second attempt
  ending with error 22. A Shelly bond entry appeared afterward.
- v2.1.3 added timing-based proxy UNPAIR and another PAIR attempt.

These observations do not establish:

- that every connection succeeds (the live probe timed out);
- that errors occur independently of every possible bond/security state;
- that error 22 proves interference or a radio drop;
- that a Shelly bond entry or UI “successful” state proves matching keys and a
  usable encrypted session on both sides;
- that fast pair responses prove the proxy skipped encryption;
- that other Shelly entries work with the same code/path.

Weak reported RSSI makes radio reliability a credible concern, but does not
identify the cause of a particular GATT or pairing error.

## 7. Validation scope

The previous log reports 13/13 stub checks in `/tmp/opencode/test_v213.py`.
The file exists, but that historical pass count was not rerun here. Stub tests
exercise the implemented choreography; they cannot validate the premise that
fast pairing means stale keys or prove real ESPHome/Shelly interoperability.
The unpair-failure reproduction in section 5 exposes a case those reported
checks did not establish as correct.

The workspace has Python 3.11.2. The `type ShellyBleRpcConfigEntry = ...` statement
requires Python 3.12+ and is **PEP 695**, not PEP 604. Compiling a modified copy
with that statement removed checks only the modified syntax. Use a suitable
Python version to validate the actual integration; do not change production
syntax just to accommodate the review interpreter.

Device access from this workspace is possible, as section 2 demonstrates. Full
Home Assistant integration testing has not been performed here. Direct native
API probes exercise a different client path from HA's Bluetooth manager and
Bleak backend and cannot substitute for that integration test.

## 8. Next investigation steps

1. Install the updated v2.1.4 integration in Home Assistant and verify setup,
   polling, and user-initiated control. Automatic bond removal has been removed.
2. Confirm the running ESP-IDF framework version from build metadata/logs;
   ESPHome API device info does not report it.
3. Preserve the now-correct proxy GATT cache and verify the Home Assistant path.
   Establish reliable BLE connectivity. Observe advertisements using the
   appropriate API subscription, inspect free connection slots and competing
   clients, and collect proxy logs. Improve proximity if possible.
4. Retain the observed stale-versus-current handle evidence above. If an HA
   read-only RPC using existing keys fails, record handles and write properties,
   and capture
   the ATT status together with proxy authentication/encryption events.
5. If needed, explicitly request security on that same connection and retry the
   protected write before inferring stale keys from timing. Use a separately
   budgeted recovery procedure.
6. Only if evidence supports stale keys, test targeted unpair/re-pair in the
   Shelly pairing window. Check the API success field and validate an RPC after
   pairing and again after disconnect/reconnect. Bond deletion is not a required
   first step. Preserve the exact address representation returned by Shelly
   (including its type suffix) when invoking its deletion API.
7. A controlled local BlueZ comparison with the same Shelly firmware and RPC
   can narrow the problem to differences in the path, security handling, timing,
   MTU, or write semantics. It cannot alone prove proxy bond corruption or rule
   framing issues out “for good.” Ensure the test actually uses the local adapter.

Interpretation rules:

| Result | Supported conclusion |
| --- | --- |
| RPC succeeds after unpair/re-pair | Recovery helped that trial; the original root cause and reconnect reliability still need validation |
| No “forgot bond” log | Could be filtering, disconnection, failure/timeout, or deadline cancellation; inspect evidence rather than assume an old API |
| Error 22 during pairing | Pairing failed; inspect proxy/security logs before assigning a radio cause |
| Pair reports success but TX control still fails | Inspect actual security events, handles, write semantics, and firmware behavior; neither timing nor a bond list proves encryption |
| Setup succeeds but later fails | Reconnect/security behavior, slot contention, firmware, and radio conditions remain candidates |

## 9. Working tree

Version 2.1.4 changes remain uncommitted. Earlier user changes to the integration
are preserved. No firmware flash or Home Assistant deployment was performed.
The existing proxy–Shelly bond is retained; live implementation testing clears
only GATT caches and issues read-only RPC calls.

## 10. Home Assistant integration fix (v2.1.4)

- `rpc.py` validates the read-only RX-control value on each new connection.
  A wrong-length value or incomplete RPC service triggers cache recovery before
  any request payload is sent. Invalid-length errors on the initial TX-control
  write can also trigger recovery, once per operation.
- `proxy_recovery.py` clears the connected backend's native cache, checks its
  confirmation, disconnects, and reconnects with service-cache reuse disabled.
  This targets the serving proxy rather than iterating all ESPHome entries.
  It intentionally avoids HA's generic BlueZ cache-clear fallback because that
  can invoke RemoveDevice and delete a bond.
- Automatic UNPAIR and pairing-duration heuristics are removed. Security is
  requested on connection before the RPC deadline; `pair_on_connect=False` is
  respected. Cache clearing, connection, pairing, and RPC have separate budgets.
- Only pre-payload failures can retry. No failure after payload transmission
  starts can automatically replay a switch command, reboot, or bond deletion.
- A zero RX-control length is polled until ready within the RPC timeout.
  Setup-flow clients are shut down after validation; runtime leases are reused.
  Connection establishment allows three attempts; payload failures never replay.
- README, setup hints, translations, and release notes describe stale GATT
  discovery correctly. Manifest version is 2.1.4; config-entry schema is unchanged.

Validation uses the actual modified integration transport with real
`bleak_esphome` / `aioesphomeapi` backends and the physical proxy/Shelly. HA lookup
and task creation are supplied by a small test harness, rather than a running HA
instance. The obsolete handle map is seeded into the actual backend process
cache. Recovery clears it, reconnects, and successfully completes
`Shelly.GetDeviceInfo`, then `Shelly.GetStatus`, and another `Shelly.GetDeviceInfo`
after disconnect/reconnect. No relay output is changed.

Transport regression tests cover recovery boundaries, native backend selection,
cache failure, cancellation, missing services, pairing policy, response readiness,
and connection reuse. Python 3.13 syntax compilation checks the unmodified
integration source, including its PEP 695 type alias. Full installation and
entity behavior inside the user's HA instance remain to be verified after update.

Final v2.1.4 checks: **18 regression tests passed**, targeted lint and formatting
checks passed, Python 3.13 compilation passed, and JSON/translation consistency
checks passed. The final live run used the real `bleak_retry_connector` connection
helper as well as `bleak_esphome`; stale-cache recovery, status RPC, and a further
RPC after reconnect all passed. An earlier repeat hit a transient connect error;
weak signal remains a practical reliability limit, not a cache-recovery failure.
