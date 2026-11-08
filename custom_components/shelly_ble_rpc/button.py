from __future__ import annotations

import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.components import persistent_notification
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ShellyBleRpcConfigEntry
from .bluez_pairing import (
    async_bluez_available,
    async_get_adapter_address_for_device,
    async_remove_bluez_device,
)
from .const import CONF_ADDRESS, CONF_PAIRING_REQUIRED
from .entity import ShellyBleRpcEntity

_LOGGER = logging.getLogger(__name__)

# The device registry records the network-side MAC of an ESPHome node while
# its Bluetooth controller address is derived from the same base MAC with a
# small platform-specific offset. Candidate offsets cover the common ESP32
# and ESP32-C3/C6 base MAC mappings.
_ESP_BT_MAC_OFFSETS = (0, 1, 2, 3)


def _normalize_ble_address(address: str) -> str:
    """Normalize Shelly/BlueZ BLE addresses for identity comparison.

    Shelly may represent an address with an address-type suffix such as:
        AA:BB:CC:DD:EE:FF,p
        AA:BB:CC:DD:EE:FF,rs

    BlueZ normally exposes only:
        AA:BB:CC:DD:EE:FF

    Comparison should therefore ignore the Shelly suffix, case, and separators.
    """
    base = str(address).split(",", 1)[0]
    return "".join(ch for ch in base.upper() if ch in "0123456789ABCDEF")


def _expanded_controller_candidates(network_mac: str) -> set[str]:
    """Return normalized ESP32 controller MAC candidates for a network MAC."""
    base = _normalize_ble_address(network_mac)
    if len(base) != 12:
        return set()

    try:
        value = int(base, 16)
    except ValueError:
        return set()

    return {
        f"{(value + offset) & 0xFFFFFFFFFFFF:012X}"
        for offset in _ESP_BT_MAC_OFFSETS
    }


async def _async_proxy_controller_candidates(hass: HomeAssistant) -> set[str]:
    """Return normalized controller MAC candidates of configured ESPHome nodes."""
    esphome_entry_ids = {
        entry.entry_id
        for entry in hass.config_entries.async_entries("esphome")
    }
    if not esphome_entry_ids:
        return set()

    registry = dr.async_get(hass)
    candidates: set[str] = set()

    for device in registry.async_entries():
        if not esphome_entry_ids & set(device.config_entries):
            continue

        for kind, mac in device.connections:
            if kind != dr.CONNECTION_NETWORK_MAC:
                continue
            candidates |= _expanded_controller_candidates(str(mac))

    return candidates


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ShellyBleRpcConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Shelly BLE RPC buttons."""
    async_add_entities(
        [
            ShellyBleRpcRestartButton(entry),
            ShellyBleRpcUnpairButton(entry),
        ]
    )


class ShellyBleRpcRestartButton(ShellyBleRpcEntity, ButtonEntity):
    """Restart Shelly over BLE RPC."""

    _attr_name = "Restart"
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ShellyBleRpcConfigEntry) -> None:
        super().__init__(
            entry,
            entry.runtime_data.coordinator,
            "restart",
        )

    async def async_press(self) -> None:
        """Reboot the Shelly."""
        await self._entry.runtime_data.client.reboot(delay_ms=1000)


class ShellyBleRpcUnpairButton(ShellyBleRpcEntity, ButtonEntity):
    """Remove the Home Assistant side bond(s) from Shelly and BlueZ."""

    _attr_name = "Unpair Bluetooth"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:bluetooth-off"

    def __init__(self, entry: ShellyBleRpcConfigEntry) -> None:
        super().__init__(
            entry,
            entry.runtime_data.coordinator,
            "unpair_bluetooth",
        )

    async def async_press(self) -> None:
        """Delete the Home Assistant side bond(s) from both sides.

        The Shelly-side bond is removed first while the authenticated BLE RPC
        connection is still possible. With a local BlueZ adapter the matching
        BlueZ device/bond is removed second. In ESPHome Bluetooth proxy setups
        the bond belongs to a proxy instead of BlueZ, so bonds that match a
        configured ESPHome node are removed from the Shelly and no BlueZ state
        is touched.
        """
        runtime = self._entry.runtime_data
        shelly_address = self._entry.data[CONF_ADDRESS]

        paired = await runtime.client.list_paired_devices()

        bluez_available = await async_bluez_available()

        if bluez_available:
            adapter_address = await async_get_adapter_address_for_device(
                shelly_address
            )
            _LOGGER.info(
                "Shelly reports %d paired BLE device(s); local HA adapter is %s",
                len(paired),
                adapter_address,
            )
            _LOGGER.debug("Shelly BLE.ListPairedDevices response: %s", paired)

            normalized_adapter = _normalize_ble_address(adapter_address)

            matching = next(
                (
                    item
                    for item in paired
                    if _normalize_ble_address(item.get("addr", ""))
                    == normalized_adapter
                ),
                None,
            )

            if matching is None:
                paired_addrs = [
                    str(item.get("addr"))
                    for item in paired
                    if isinstance(item, dict)
                ]
                raise RuntimeError(
                    "The Shelly does not list the Home Assistant Bluetooth "
                    f"adapter {adapter_address} as a bonded device after address "
                    f"normalization. Shelly reported: {paired_addrs}. "
                    "No Shelly-side bond was deleted."
                )

            bonds_to_delete = [matching]
            controller_description = (
                f"the Home Assistant Bluetooth adapter {adapter_address}"
            )
        else:
            # ESPHome Bluetooth proxy path: the bond is held by a proxy node,
            # not by BlueZ. Match the Shelly bond list against the configured
            # ESPHome nodes so only integration-owned bonds are deleted.
            proxy_candidates = await _async_proxy_controller_candidates(self.hass)

            _LOGGER.info(
                "No local BlueZ adapter; Shelly reports %d paired BLE "
                "device(s) and %d ESPHome Bluetooth proxy candidate address(es)",
                len(paired),
                len(proxy_candidates),
            )
            _LOGGER.debug("Shelly BLE.ListPairedDevices response: %s", paired)

            if not proxy_candidates:
                paired_addrs = [
                    str(item.get("addr"))
                    for item in paired
                    if isinstance(item, dict)
                ]
                raise RuntimeError(
                    "No local Bluetooth adapter and no ESPHome Bluetooth "
                    "proxy addresses are known to Home Assistant, so the "
                    "Shelly-side bond cannot be identified. Shelly reported: "
                    f"{paired_addrs}. Add the ESPHome proxy to Home "
                    "Assistant, or remove the bond from the Shelly itself "
                    "before re-adding this integration."
                )

            bonds_to_delete = [
                item
                for item in paired
                if _normalize_ble_address(item.get("addr", ""))
                in proxy_candidates
            ]

            if not bonds_to_delete:
                paired_addrs = [
                    str(item.get("addr"))
                    for item in paired
                    if isinstance(item, dict)
                ]
                raise RuntimeError(
                    "The Shelly does not list any bond that matches a "
                    "configured ESPHome Bluetooth proxy address. Shelly "
                    f"reported: {paired_addrs}. No Shelly-side bond was "
                    "deleted."
                )

            matching_addr_desc = ", ".join(
                str(item.get("addr")) for item in bonds_to_delete
            )
            _LOGGER.info(
                "Matched Shelly bond entries to ESPHome Bluetooth proxy "
                "addresses: %s",
                matching_addr_desc,
            )
            controller_description = (
                "the configured ESPHome Bluetooth proxy bond(s): "
                f"{matching_addr_desc}"
            )

        for bond in bonds_to_delete:
            # Use the exact address representation returned by Shelly for
            # deletion. This avoids assuming whether the firmware expects an
            # address-type suffix.
            shelly_bond_address = str(bond.get("addr"))

            # First delete the controller from the Shelly while RPC is
            # authenticated.
            #
            # IMPORTANT: deleting the active BLE controller's bond can
            # invalidate the encrypted GATT session immediately.
            # Shelly/BlueZ/ESPHome may therefore report ATT error 0x0E
            # ("Unlikely Error") even though the delete command itself has
            # already been applied. Treat ONLY that exact terminal error as
            # expected and continue with local cleanup.
            try:
                await runtime.client.delete_paired_device(shelly_bond_address)
            except Exception as err:
                error_text = f"{type(err).__name__}: {err}".lower()
                expected_post_delete_error = (
                    "unlikely_error" in error_text
                    or "gatt protocol error: unlikely error" in error_text
                    or "protocolerrorcode.unlikely_error" in error_text
                )

                if not expected_post_delete_error:
                    raise

                _LOGGER.info(
                    "Shelly bond-delete request for %s ended with expected "
                    "post-delete GATT 0x0E/Unlikely Error; treating this as "
                    "successful bond invalidation and continuing cleanup",
                    shelly_bond_address,
                )
            else:
                _LOGGER.info(
                    "Deleted Home Assistant side bond %s from Shelly storage",
                    shelly_bond_address,
                )

        # Prevent coordinator activity from attempting to pair again while the
        # user is intentionally testing a clean removal.
        runtime.unpaired = True
        runtime.client.pair_on_connect = False
        runtime.coordinator.update_interval = None

        # Then remove the Shelly and its cached bond from BlueZ when one is
        # actually managing the local side.
        if bluez_available:
            await async_remove_bluez_device(shelly_address)

        # Persist that this entry was intentionally unpaired. A later Reload
        # may therefore perform explicit pairing without confusing ordinary
        # connection timeouts with a pairing problem.
        new_data = dict(self._entry.data)
        new_data[CONF_PAIRING_REQUIRED] = True
        self.hass.config_entries.async_update_entry(
            self._entry,
            data=new_data,
        )
        _LOGGER.info(
            "Marked config entry %s as requiring explicit Bluetooth pairing",
            self._entry.entry_id,
        )

        # Mark existing entities unavailable. The intended next action is to
        # delete this HA config entry and add it again as a fresh device.
        runtime.coordinator.async_set_update_error(
            RuntimeError(
                "Bluetooth bond removed. Delete this integration entry and "
                "re-add the Shelly while it is in pairing mode."
            )
        )

        if bluez_available:
            notification_body = (
                f"Bluetooth unpair completed for **{self._entry.title}**. "
                f"The Shelly bond for {controller_description} and the "
                "corresponding BlueZ device/bond were removed.\n\n"
                "You can now either:\n\n"
                "• Put the Shelly into pairing mode and **reload this "
                "integration** to re-pair the existing device, or\n"
                "• Delete this integration entry, put the Shelly into pairing "
                "mode, and add it again for a clean pairing test."
            )
        else:
            notification_body = (
                f"Bluetooth unpair completed for **{self._entry.title}**. "
                f"The Shelly bond for {controller_description} was removed "
                "from the Shelly; no local BlueZ bond existed.\n\n"
                "You can now either:\n\n"
                "• Put the Shelly into pairing mode and **reload this "
                "integration** so the ESPHome Bluetooth proxy can build a "
                "fresh bond, or\n"
                "• Delete this integration entry and add it again for a clean "
                "pairing test."
            )

        persistent_notification.async_create(
            self.hass,
            notification_body,
            title="Shelly BLE RPC: Unpair successful",
            notification_id=(
                "shelly_ble_rpc_unpair_"
                + shelly_address.replace(":", "").lower()
            ),
        )

        _LOGGER.info(
            "Bluetooth unpair completed successfully for %s",
            shelly_address,
        )
