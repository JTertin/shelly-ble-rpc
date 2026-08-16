# Shelly BLE RPC

Shelly BLE RPC is a Home Assistant custom integration for controlling compatible Shelly Gen3 and Gen4 devices directly over Bluetooth Low Energy RPC. Shelly firmware 2.0.0 or newer is required. Gen2 compatibility may be possible but has not yet been validated.

> [!IMPORTANT]
> This is an independent community integration and is not affiliated with or supported by Shelly Group or the Home Assistant project.

## Features

- Direct local communication over Bluetooth Low Energy; no Shelly Cloud connection is required for control.
- Home Assistant UI configuration flow.
- BLE pairing/bonding support for Shelly devices that require authenticated BLE RPC access.
- Shared `Shelly.GetStatus` polling to minimize BLE connections and RPC traffic.
- Relay control for discovered switch components.
- Power, voltage, frequency, current, energy, returned-energy, temperature, RSSI, and uptime sensors when exposed by the device.
- Input and protection-condition binary sensors when exposed by the device.
- Restart and Bluetooth unpair actions.
- Device model and firmware information in the Home Assistant device registry.

## Requirements

- Home Assistant with a functioning Bluetooth adapter accessible to Home Assistant.
- A supported Shelly device exposing Shelly BLE RPC. Development and testing have focused on Shelly Gen4 relay devices.
- The Shelly must be within reliable Bluetooth range of the Home Assistant Bluetooth adapter.

The explicit Bluetooth pairing implementation interacts with BlueZ over the system D-Bus. It is intended for Home Assistant installations where the Home Assistant Bluetooth stack has access to the local BlueZ service, such as Home Assistant OS installations using a local Bluetooth adapter.

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

For an existing configured device, the **Unpair Bluetooth** button intentionally removes the Bluetooth bond and marks the integration for re-pairing. After unpairing:

1. Put the Shelly into Bluetooth pairing mode.
2. Reload the Shelly BLE RPC integration entry.
3. The integration will perform one explicit pairing attempt and validate the bond with `Shelly.GetStatus`.

Ordinary Bluetooth timeouts, temporary range loss, adapter startup conditions, or service-discovery failures do not intentionally delete the existing bond.

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
