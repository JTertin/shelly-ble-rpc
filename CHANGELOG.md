# Changelog

All notable changes to Shelly BLE RPC will be documented in this file.

## v2.1.4 — Recover stale GATT discovery without removing bonds

### Fixed

- Validate the read-only RPC control value before writing to cached handles.
  Refresh the active Bluetooth backend's GATT cache and reconnect once when
  discovery is stale or the initial TX-control write rejects the frame length.
  This fixes the verified ESPHome proxy failure where cached TX handle 9 was
  actually a descriptor and the current TX handle was 14.
- Remove the timing-based proxy UNPAIR workaround. Fast pairing can reuse a
  valid bond; ATT 0x0D does not establish a missing bond. Automatic recovery
  preserves all bonds and targets the connected backend rather than every proxy.
- Request pairing/encryption before starting the RPC deadline, honor disabled
  pairing during existing-connection validation, and close setup-flow connections.
  Retry connection establishment up to three times for transient radio failures;
  this never replays an RPC payload.
- Wait for a nonzero RX-control length while a response is being prepared.
  Recovery never replays a request after payload transmission has started.

### Validation

- Regression tests cover stale-handle recovery, bounded retries, failed cache
  clearing, delayed responses, connection reuse, pairing policy, and preventing
  duplicate commands after payload failures.
- Live testing of the updated integration transport through the real ESPHome
  Bleak backend verified recovery from a seeded obsolete handle map, device
  information and status RPCs, and another RPC after disconnect/reconnect.

## v2.1.3 --- Force a real pairing exchange behind a stale proxy bond

> Superseded diagnosis: the stale-bond explanation below was not established.
> Hardware testing identified stale GATT handles; v2.1.4 removes this workaround.

### Fixed

-   An ESPHome Bluetooth proxy that holds a stale bond answers Home
    Assistant's pair request instantly with "already paired" without
    establishing an encrypted link, so the Shelly kept rejecting RPC writes
    on every connection even with a fresh bond on the Shelly side. The
    bond recovery on the open connection is now measured: a pairing that
    completes in under 0.4 seconds is treated as this shortcut. While a
    pairing/recovery flow is running, Home Assistant then asks every
    loaded ESPHome Bluetooth proxy over its API (aioesphomeapi
    `bluetooth_device_unpair`) to forget its bond for the device and
    repeats the pairing exchange for real on the still-open connection.
    The recovery only ever removes a proxy's own bond, it requires the
    Shelly to accept pairing, and it is disabled for ordinary polling so
    normal RPC traffic can never remove bonds.

### Added

-   The resolved GATT handles of the Shelly RPC characteristics are logged
    at debug level for every connection, so an ATT error that cites a
    handle can be mapped to the exact characteristic that rejected it.
-   The pairing-failure hint and README now document the automatic proxy
    bond recovery and that refashing a proxy to ESP-IDF 5.5.5 keeps stale
    5.5.6-era NVS bonds.

## v2.1.2 --- Bond on the open connection and stale-bond recovery

> Historical release notes: interpreting ATT 0x0D as proof of an unencrypted
> link was incorrect. See the v2.1.4 correction above.

### Fixed

-   RPC writes now recover the Bluetooth bond on the connection that is
    already open instead of relying on the inter-attempt retry loop. ESPHome
    Bluetooth proxies forget the pairing state of every new connection even
    though the Bluetooth bond itself is kept in the proxy, so the Shelly's
    RPC control characteristic kept answering the ATT error 0x0D (Invalid
    attribute length) on every connection-validated bonding attempt even
    after the Shelly listed the proxy as paired. When the first RPC write
    fails this way, the client now asks the Bluetooth backend of the open
    connection to pair (which re-encrypts from the bond the proxy or local
    adapter holds), retries the write once, and never deletes a bond on
    either side. This applies to normal polls as well, so a proxied setup
    stays functional across connection renewals.

### Added

-   The pairing-failure screen explains the recovery sequence end to end
    when the Shelly declined RPC writes without a usable bond: Retry
    pairing in pairing mode near the active adapter/proxy, clear a stale
    Shelly-side pairing in the Shelly Smart Control app or web UI after a
    power cycle, and rebuild ESPHome Bluetooth proxy firmware with ESP-IDF
    5.5.5 because ESP-IDF 5.5.6 (default in ESPHome 2026.10.x) drops
    Bluetooth bonds on disconnect (ESPHome issue 21880).

## v2.1.1 --- Progress step transition and pairing retry fixes

### Fixed

-   The pairing retry flow no longer crashes with
    `ValueError: Show progress can only transition to show progress or show
    progress done`. A Home Assistant progress step is only allowed to
    transition to another progress step or to the progress-done marker, so a
    failed attempt is now routed through the progress-done marker into the
    pairing-failure menu, and a successful attempt routes through it into
    the entry-creation step. Previously the whole flow died with an
    "Error doing job: Task exception was never retrieved" traceback the
    moment an attempt failed.
-   The progress step no longer finishes the flow while the pairing attempt
    is still running. The Home Assistant flow engine re-invokes the progress
    step both while the background attempt runs and again once it completes;
    the flow now re-shows the progress pane when the attempt is still in
    progress and only creates the config entry after the attempt has
    actually finished and validated a real RPC transaction.

### Changed

-   Connection-validated bonding retries were increased from three attempts
    three seconds apart to four attempts five seconds apart, because the
    Bluetooth bond through an ESPHome Bluetooth proxy can drop and settle
    the proxied link mid-procedure while the Shelly's pairing window is
    open.
-   The ATT error 0x0D ("Invalid attribute length") that Shelly devices
    return for RPC writes without a usable Bluetooth bond is now explained
    in an INFO log line ("BLE RPC write to ... rejected") instead of only
    appearing as a raw `BleakError ... error=13` string.

## v2.1.0 --- Config flow and proxy pairing routing fixes

### Fixed

-   The Bluetooth pairing retry flow no longer crashes with
    `UnknownStep ... doesn't support step pairing_failed` when pairing
    fails. The failure menu now has the step handler Home Assistant's flow
    engine requires, so the menu with the last error and a Retry option is
    displayed instead of killing the flow. This defect existed in previous
    releases but was rarely reached because explicit pairing usually
    succeeded on the first attempt.
-   Bluetooth pairing is now aligned with the radio that Home Assistant
    would actually use. Installations that have both a local Bluetooth
    adapter and an ESPHome Bluetooth proxy previously attempted explicit
    BlueZ pairing even when BlueZ had never observed the Shelly, which made
    pairing through a proxy impossible. The retry flow now attempts
    connection-validated bonding first - during the Shelly's Bluetooth
    pairing window the ESPHome Bluetooth proxy bonds with the Shelly during
    its own connect attempt - and falls back to explicit BlueZ pairing only
    when a local adapter has actually observed the device.
-   The initial connection validation failure in the config flow is now
    logged at debug level, making the underlying Bluetooth error visible
    when the flow switches to the pairing screen.

### Changed

-   The Bluetooth re-pair/Reload flow and the pairing error text attempt
    connection bonding before explicit pairing, and mention both the local
    adapter and the ESPHome Bluetooth proxy as valid locations for the
    Shelly.

## v1.1.0 --- ESPHome Bluetooth proxy support

### Features

-   Support setups without a local Bluetooth adapter by operating completely
    through Home Assistant's ESPHome Bluetooth proxies for discovery,
    connection, RPC communication, and control.
-   Automatic detection of BlueZ availability. The BlueZ pairing-state check,
    explicit pairing, and BlueZ bond cleanup are skipped gracefully when no
    system D-Bus/BlueZ stack exists, avoiding setup and unpair failures in
    proxy-only installations and containers without a D-Bus socket.
-   Proxy bonding path: with an active-capable ESPHome Bluetooth proxy
    (ESP32/RP2040), Bluetooth bonding is performed by the proxy while the
    Shelly is in Bluetooth pairing mode; setup and the Retry pairing flow
    validate the bond with real RPC transactions instead of a client-side
    pairing agent.
-   The Bluetooth unpair button identifies and deletes Shelly-side bond
    entries matching configured ESPHome proxy devices when no local BlueZ
    adapter is present, including the platform-dependent ESP32 Bluetooth
    controller MAC offsets.
-   Config-flow pairing text now explains proxy pairing behavior for the
    pairing required and pairing failed steps.
-   Slightly more tolerant delay after the RPC TX-control write, which
    reduces sensitivity to the additional round-trips of proxied GATT
    operations.
-   Local BlueZ adapter behavior is unchanged: explicit NoInputNoOutput
    agent pairing, bond-state checks, and BlueZ-side unpair all continue to
    apply when BlueZ is available.

### Requirements

-   Home Assistant 2026.3 or newer.
-   Bluetooth is now accessible through a local Bluetooth adapter or through
    ESPHome Bluetooth proxies that support active GATT connections
    (ESP32 and RP2040/RP2350 platforms only). Advertisement-only proxy
    platforms (BK72xx, LN882x) are not sufficient.

## v1.0.25 --- Initial Release

Initial public release of the Shelly BLE RPC custom integration for Home
Assistant.

### Features

-   Direct control of compatible Shelly 1PM  devices using Bluetooth Low
    Energy RPC.  Was developed with Gen4, but Gen3 should work also.
-   Automatic Bluetooth discovery, pairing, and bond management through
    BlueZ.
-   Automatic discovery of available Shelly switch components.
-   Home Assistant switch entities for relay control.
-   Electrical monitoring entities when supported by the device:
    -   Power
    -   Voltage
    -   Current
    -   Frequency
    -   Energy
    -   Returned energy
    -   Internal temperature
-   Diagnostic and status entities including:
    -   Input state
    -   BLE signal strength
    -   Device uptime
    -   Overcurrent
    -   Overheating
    -   Overpower
    -   Overvoltage
    -   Cloud connection status
    -   Restart-required status
-   Device information including model and firmware version.
-   Restart button.
-   Bluetooth unpair function for removing the Home Assistant/Shelly
    bond.
-   Re-pairing support for existing Home Assistant configuration
    entries.
-   Shared `Shelly.GetStatus` polling to minimize BLE communication.
-   Short-lived BLE connection reuse for responsive relay commands.
-   Non-destructive handling of temporary Bluetooth communication
    failures.
-   Home Assistant integration branding and configuration-flow support.

### Requirements

-   Home Assistant 2026.3 or newer.
-   Bluetooth adapter accessible to Home Assistant.
-   Compatible Shelly device supporting BLE RPC funning firmware version 2.0.0 or higher.
