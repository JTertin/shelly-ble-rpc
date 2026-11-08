from __future__ import annotations

import logging
import re

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.const import CONF_NAME

from .bluez_pairing import (
    ShellyBlueZUnavailable,
    async_bluez_available,
    async_get_pairing_state,
)
from .const import CONF_ADDRESS, DEFAULT_NAME, DOMAIN
from .rpc import (
    ShellyBleRpcClient,
    async_bond_via_connection,
    is_write_rejected_error,
)

_LOGGER = logging.getLogger(__name__)

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

# ATT length errors may come from stale GATT discovery, not a missing bond.
_WRITE_REJECTED_HINT = (
    "The Shelly rejected a write with an invalid attribute length. This can "
    "happen when Bluetooth service handles cached before a firmware update "
    "no longer match the device. The integration refreshes the active "
    "adapter or proxy's GATT cache once without deleting bonds. If the "
    "error persists, reload the ESPHome proxy entry and retry. For a new "
    "bond, enable Bluetooth pairing mode on the Shelly and keep it close "
    "to the adapter or proxy."
)


class ShellyBleRpcConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Shelly BLE RPC."""

    VERSION = 2
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._pending_name: str | None = None
        self._pending_address: str | None = None
        self._pairing_error: Exception | None = None

    async def async_step_user(self, user_input=None):
        """Collect name/address, then validate before entry creation."""
        errors = {}

        discovered = bluetooth.async_discovered_service_info(
            self.hass,
            connectable=True,
        )

        choices = {}
        for info in discovered:
            name = info.name or ""
            if "shelly" in name.lower():
                choices[info.address] = f"{name} ({info.address})"

        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper()
            if not _MAC_RE.match(address):
                errors["base"] = "invalid_address"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()

                self._pending_name = user_input[CONF_NAME]
                self._pending_address = address

                # First try the existing bond with a normal RPC call.
                try:
                    await self._validate_existing_connection()
                except Exception as err:
                    _LOGGER.debug(
                        "Existing bond validation for %s failed: %s: %s",
                        address,
                        type(err).__name__,
                        err,
                    )
                    # Do not create a broken config entry. Move to a retryable
                    # pairing screen instead.
                    return await self.async_step_pairing()
                else:
                    return self._create_pending_entry()

        address_schema = vol.In(choices) if choices else str

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_ADDRESS): address_schema,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_pairing(self, user_input=None):
        """Prompt the user to place the Shelly into pairing mode."""
        if not self._pending_address or not self._pending_name:
            return self.async_abort(reason="missing_pending_device")

        return self.async_show_menu(
            step_id="pairing",
            menu_options=["retry_pairing"],
            description_placeholders={
                "device": self._pending_name,
                "address": self._pending_address,
            },
        )

    async def async_step_retry_pairing(self, user_input=None):
        """Start pairing retry behind a Home Assistant progress step."""
        if not self._pending_address or not self._pending_name:
            return self.async_abort(reason="missing_pending_device")

        self._pairing_error = None

        async def _pair_and_validate() -> None:
            try:
                await self._attempt_pair_and_validate()
            except Exception as err:
                self._pairing_error = err

        return self.async_show_progress(
            step_id="retry_pairing_progress",
            progress_action="retry_pairing",
            progress_task=self.hass.async_create_task(_pair_and_validate()),
            description_placeholders={
                "device": self._pending_name,
                "address": self._pending_address,
            },
        )

    async def async_step_retry_pairing_progress(self, user_input=None):
        """Continue the pairing retry behind the progress step.

        Home Assistant's flow engine invokes this step while the background
        pairing task is still running and again once the task completes. A
        SHOW_PROGRESS step may only transition to another SHOW_PROGRESS step
        or to the progress-done marker, so success and failure are routed
        through async_show_progress_done here.
        """
        if not self._pending_address or not self._pending_name:
            return self.async_abort(reason="missing_pending_device")

        task = self.async_get_progress_task()

        if task is not None and not task.done():
            # Invoked while the pairing attempt is still running. Re-show the
            # progress pane with the same task; finishing the flow here would
            # create a config entry without validation.
            return self.async_show_progress(
                step_id="retry_pairing_progress",
                progress_action="retry_pairing",
                progress_task=task,
                description_placeholders={
                    "device": self._pending_name,
                    "address": self._pending_address,
                },
            )

        if self._pairing_error is not None:
            return self.async_show_progress_done(next_step_id="pairing_failed")

        if task is None:
            # The background attempt is gone without a recorded reason. Fall
            # back to the pairing instructions instead of creating an
            # unvalidated config entry.
            return await self.async_step_pairing()

        return self.async_show_progress_done(next_step_id="pairing_complete")

    async def async_step_pairing_failed(self, user_input=None):
        """Show the retry menu after a failed pairing attempt.

        Reached through async_show_progress_done(next_step_id=
        "pairing_failed"), which is required because the flow engine only
        lets a SHOW_PROGRESS step transition to another SHOW_PROGRESS step
        or to the progress-done marker. Selecting the Retry option from this
        menu routes directly into async_step_retry_pairing, which starts a
        fresh attempt behind a new progress step.
        """
        if not self._pending_address or not self._pending_name:
            return self.async_abort(reason="missing_pending_device")

        err = self._pairing_error
        error_text = "unknown pairing failure"
        hint = ""
        if err is not None:
            error_text = str(err) or type(err).__name__
            if is_write_rejected_error(err):
                hint = _WRITE_REJECTED_HINT

        return self.async_show_menu(
            step_id="pairing_failed",
            menu_options=["retry_pairing"],
            description_placeholders={
                "device": self._pending_name,
                "address": self._pending_address,
                "error": error_text,
                "hint": hint,
            },
        )

    async def async_step_pairing_complete(self, user_input=None):
        """Create the entry after a validated pairing attempt.

        Reached through async_show_progress_done(next_step_id=
        "pairing_complete"); the flow engine requires the progress-done
        marker between the SHOW_PROGRESS step and this step.
        """
        if not self._pending_address or not self._pending_name:
            return self.async_abort(reason="missing_pending_device")

        return self._create_pending_entry()

    async def _validate_existing_connection(self) -> None:
        """Verify that an existing bond can complete Shelly.GetStatus."""
        client = ShellyBleRpcClient(
            hass=self.hass,
            address=self._pending_address,
            pair_on_connect=False,
        )
        try:
            await client.get_status()
        finally:
            await client.async_shutdown()

    async def _attempt_pair_and_validate(self) -> None:
        """Pair on the selected connection, with an optional local BlueZ agent."""
        address = self._pending_address
        name = self._pending_name
        if not address or not name:
            raise RuntimeError("The pending Shelly device information was lost")

        client = ShellyBleRpcClient(
            hass=self.hass,
            address=address,
            pair_on_connect=True,
        )

        try:
            bluez_available = await async_bluez_available()
            device_present = False

            if bluez_available:
                try:
                    device_present, _, _ = await async_get_pairing_state(address)
                except ShellyBlueZUnavailable:
                    device_present = False

            if device_present:
                try:
                    await client.pair_explicitly()
                    await client.get_status()
                    return
                except Exception as err:
                    _LOGGER.info(
                        "Completing pairing for %s through BlueZ failed: %s: %s; "
                        "falling back to the connection path",
                        address,
                        type(err).__name__,
                        err,
                    )
            elif bluez_available:
                _LOGGER.debug(
                    "BlueZ has not observed %s; bonding through the connection path",
                    address,
                )
            else:
                _LOGGER.debug(
                    "No local BlueZ adapter; bonding through the connection path "
                    "(an ESPHome Bluetooth proxy bonds while the Shelly is in "
                    "pairing mode)"
                )

            await async_bond_via_connection(
                client,
                address,
                entry_title=name,
            )
        finally:
            await client.async_shutdown()

    def _create_pending_entry(self):
        """Create a validated config entry."""
        return self.async_create_entry(
            title=self._pending_name,
            data={
                CONF_NAME: self._pending_name,
                CONF_ADDRESS: self._pending_address,
            },
        )
