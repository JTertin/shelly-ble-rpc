from __future__ import annotations

import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.components import persistent_notification
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ShellyBleRpcConfigEntry
from .bluez_pairing import (
    async_get_adapter_address_for_device,
    async_remove_bluez_device,
)
from .const import CONF_ADDRESS, CONF_PAIRING_REQUIRED
from .entity import ShellyBleRpcEntity

_LOGGER = logging.getLogger(__name__)


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
    """Remove the HA controller bond from both Shelly and BlueZ."""

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
        """Delete this HA adapter's bond on both sides.

        The Shelly-side bond is removed first while the authenticated BLE RPC
        connection is still possible. BlueZ is removed second. Automatic
        re-pairing is then disabled for this config entry until HA is restarted
        or the entry is removed/re-added.
        """
        runtime = self._entry.runtime_data
        shelly_address = self._entry.data[CONF_ADDRESS]

        adapter_address = await async_get_adapter_address_for_device(
            shelly_address
        )
        paired = await runtime.client.list_paired_devices()

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

        # Use the exact address representation returned by Shelly for deletion.
        # This avoids assuming whether the firmware expects an address-type suffix.
        shelly_bond_address = str(matching.get("addr"))

        _LOGGER.info(
            "Matched BlueZ adapter %s to Shelly bond entry %s",
            adapter_address,
            shelly_bond_address,
        )

        # First delete the controller from the Shelly while RPC is authenticated.
        #
        # IMPORTANT: deleting the active BLE controller's bond can invalidate
        # the encrypted GATT session immediately. Shelly/BlueZ may therefore
        # report ATT error 0x0E ("Unlikely Error") even though the delete
        # command itself has already been applied. Treat ONLY that exact
        # terminal error as expected and continue with local BlueZ cleanup.
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
                "successful bond invalidation and continuing BlueZ cleanup",
                shelly_bond_address,
            )
        else:
            _LOGGER.info(
                "Deleted Home Assistant adapter bond %s from Shelly storage",
                shelly_bond_address,
            )

        # Prevent coordinator activity from attempting to pair again while the
        # user is intentionally testing a clean removal.
        runtime.unpaired = True
        runtime.client.pair_on_connect = False
        runtime.coordinator.update_interval = None

        # Then remove the Shelly and its cached bond from BlueZ.
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

        persistent_notification.async_create(
            self.hass,
            (
                f"Bluetooth unpair completed for **{self._entry.title}**. "
                f"The Shelly bond for Home Assistant adapter `{adapter_address}` "
                "and the corresponding BlueZ device/bond were removed.\n\n"
                "You can now either:\n\n"
                "• Put the Shelly into pairing mode and **reload this integration** "
                "to re-pair the existing device, or\n"
                "• Delete this integration entry, put the Shelly into pairing mode, "
                "and add it again for a clean pairing test."
            ),
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
