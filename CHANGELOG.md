# Changelog

All notable changes to Shelly BLE RPC will be documented in this file.

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
