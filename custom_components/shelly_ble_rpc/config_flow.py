from __future__ import annotations

import re
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.const import CONF_NAME

from .const import CONF_ADDRESS, DEFAULT_NAME, DOMAIN
from .rpc import ShellyBleRpcClient, ShellyBleRpcError, ShellyBleRpcUnavailable
from .bluez_pairing import ShellyBlePairingError

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


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
                except Exception:
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
        """Finish pairing retry after the progress task completes."""
        if not self._pending_address or not self._pending_name:
            return self.async_abort(reason="missing_pending_device")

        if self._pairing_error is not None:
            err = self._pairing_error
            return self.async_show_menu(
                step_id="pairing_failed",
                menu_options=["retry_pairing"],
                description_placeholders={
                    "device": self._pending_name,
                    "address": self._pending_address,
                    "error": str(err) or type(err).__name__,
                },
            )

        return self._create_pending_entry()

    async def _validate_existing_connection(self) -> None:
        """Verify that an existing bond can complete Shelly.GetStatus."""
        client = ShellyBleRpcClient(
            hass=self.hass,
            address=self._pending_address,
            pair_on_connect=False,
        )
        await client.get_status()

    async def _attempt_pair_and_validate(self) -> None:
        """Explicitly pair, then verify a real BLE RPC transaction succeeds."""
        client = ShellyBleRpcClient(
            hass=self.hass,
            address=self._pending_address,
            pair_on_connect=True,
        )
        await client.pair_explicitly()
        await client.get_status()

    def _create_pending_entry(self):
        """Create a validated config entry."""
        return self.async_create_entry(
            title=self._pending_name,
            data={
                CONF_NAME: self._pending_name,
                CONF_ADDRESS: self._pending_address,
            },
        )
