# Shelly BLE RPC

Shelly BLE RPC is a Home Assistant custom integration for controlling compatible Shelly Gen3 and Gen4 devices directly over Bluetooth Low Energy RPC. Shelly firmware 2.0.0 or newer is required. Gen2 compatibility may be possible but has not yet been validated.

> [!IMPORTANT]
> This is an independent community integration and is not affiliated with or supported by Shelly Group or the Home Assistant project.

## Features

- Direct local communication over Bluetooth Low Energy; no Shelly Cloud connection is required for control.
- Works over a local Bluetooth adapter and over ESPHome Bluetooth proxies (ESP32/RP2040).
- Home Assistant UI configuration flow.
- BLE pairing/bonding support for Shelly devices that require authenticated BLE RPC access, either through an explicit BlueZ pairing agent (local adapter) or through proxy-initiated bonding (ESPHome Bluetooth proxy).
- Shared `Shelly.GetStatus` polling to minimize BLE connections and RPC traffic.
- Relay control for discovered switch components.
- Power, voltage, frequency, current, energy, returned-energy, temperature, RSSI, and uptime sensors when exposed by the device.
- Input and protection-condition binary sensors when exposed by the device.
- Restart and Bluetooth unpair actions.
- Device model and firmware information in the Home Assistant device registry.

## Requirements

- Home Assistant with Bluetooth available either through a local Bluetooth adapter accessible to Home Assistant or through one or more [ESPHome Bluetooth proxies](https://esphome.io/components/bluetooth_proxy/) (ESP32 or RP2040-based proxies with active connections).
- A supported Shelly device exposing Shelly BLE RPC. Development and testing have focused on Shelly Gen4 relay devices.
- The Shelly must be within reliable Bluetooth range of the adapter or ESPHome Bluetooth proxy that serves it.

### ESPHome Bluetooth proxy support

No local BlueZ adapter is required. When Home Assistant has no local Bluetooth stack, the integration automatically operates through ESPHome Bluetooth proxies:

- Discovery, connection, RPC communication, status polling, and control work through proxies exactly as with a local adapter.
- Pairing is performed by the proxy. With a local adapter, Home Assistant runs an explicit pairing agent; through a proxy, put the Shelly into Bluetooth pairing mode and let the setup flow connect: the proxy performs the bond itself during the pairing window.
- Only proxies that support active GATT connections (ESP32 and RP2040/RP2350) can serve this integration. Advertisement-only proxies (BK72xx and LN882x) are not sufficient.

The explicit Bluetooth pairing implementation interacts with BlueZ over the system D-Bus. It is used only when BlueZ is available, such as Home Assistant OS or container installations with a local Bluetooth adapter.

## Installation with HACS

Until this repository is included in the default HACS catalog, add it as a custom repository:

1. Open **HACS** in Home Assistant.
2. Open the three-dot menu in the upper-right corner.
3. Select **Custom repositories**.
4. Enter this repository URL:

   `https://github.com/jtertin/shelly-ble-rpc`

5. Select **Integration** as the repository type.
6. Click **Add**.
7. Open **Shelly BLE RPC** in HACS and click **Download**.
8. Restart Home Assistant.
9. Go to **Settings > Devices & services > Add integration**.
10. Search for **Shelly BLE RPC** and follow the setup flow.

## Manual installation

1. Copy the directory:

   `custom_components/shelly_ble_rpc`

   into your Home Assistant configuration directory so the final path is:

   `/config/custom_components/shelly_ble_rpc`

2. Restart Home Assistant.
3. Go to **Settings > Devices & services > Add integration**.
4. Search for **Shelly BLE RPC**.

## Pairing

If Home Assistant does not already have a valid Bluetooth bond with the Shelly, the setup flow will prompt you to place the device in Bluetooth pairing mode and retry pairing.

- **Local Bluetooth adapter:** Home Assistant runs an explicit BlueZ pairing agent while the Shelly is in pairing mode.
- **ESPHome Bluetooth proxy:** keep the Shelly close to the proxy, enable Bluetooth pairing mode on the Shelly, and select Retry pairing. The integration explicitly requests pairing/encryption on the selected connection. Existing keys can be reused; a fast pairing response is normal and does not trigger bond deletion. Pairing runs before the RPC timeout starts.

For an existing configured device, the **Unpair Bluetooth** button intentionally removes the Bluetooth bond and marks the integration for re-pairing:

- With a local adapter, the bond is removed from both the Shelly and BlueZ.
- Through an ESPHome Bluetooth proxy, the bond is held by the proxy. The button deletes the Shelly-side bond entries that match configured ESPHome devices, and no local BlueZ state is touched.

After unpairing:

1. Put the Shelly into Bluetooth pairing mode.
2. Reload the Shelly BLE RPC integration entry.
3. The integration will validate the bond with `Shelly.GetStatus`, performing explicit pairing on a local adapter or letting the ESPHome proxy bond again through a proxy.

Ordinary Bluetooth timeouts, temporary range loss, adapter startup conditions, or service-discovery failures do not intentionally delete the existing bond.

### Invalid attribute length (ATT 0x0D) after a firmware update

A proxy can retain an old GATT service map after the Shelly's firmware changes its attribute handles. The UUIDs still look correct in the cache, but an RPC write can reach a different attribute. In the verified Gen4 case, cached TX-control handle 9 had become a two-byte descriptor; the real TX-control handle was 14. Sending a four-byte frame length to the descriptor produced ATT 0x0D even after successful fresh pairing.

From v2.1.4 the integration checks the read-only RPC control value before writing. If the map is stale, or the initial TX-control write returns an invalid-length error, it clears the **active connection's GATT cache**, disconnects, and rediscovers the service once. This preserves Bluetooth bonds and does not replay requests after payload transmission has begun. Handles are always discovered by UUID, never hard-coded.

Automatic clearing requires a native backend cache API; the integration does not invoke the generic BlueZ fallback because it can remove a bond. If cache clearing is unsupported or fails, update the proxy firmware, reload its ESPHome entry and the Shelly BLE RPC entry, then retry. A length error alone does not establish a pairing failure. For a genuinely new bond, the Shelly still needs Bluetooth pairing mode enabled.

A separate [ESP-IDF 5.5.6 reconnect-encryption issue](https://github.com/esphome/esphome/issues/21880) has been reported. That report concerns in-memory keys becoming unusable while a persistent bond entry can remain; it does not establish that bonds are deleted on every disconnect. Framework changes should be considered separately from stale GATT discovery.

## Troubleshooting

For additional logging, add the following to `configuration.yaml` and restart Home Assistant:

```yaml
logger:
  default: info
  logs:
    custom_components.shelly_ble_rpc: debug
    bleak_retry_connector: debug
    habluetooth: debug
```

When reporting a problem, include:

- Home Assistant version
- Shelly BLE RPC version
- Shelly model
- Shelly firmware version
- Relevant log entries surrounding the failure

Please remove passwords, tokens, private addresses, or other sensitive information before posting logs publicly.

## Releases

Release notes are available in [CHANGELOG.md](CHANGELOG.md) and on the GitHub Releases page.

## Disclaimer

This integration communicates directly with device Bluetooth services and is community-developed software. Test changes carefully before relying on it for critical loads or processes.
