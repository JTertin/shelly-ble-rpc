from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DEFAULT_SCAN_INTERVAL_SECONDS
from .rpc import ShellyBleRpcClient, ShellyBleRpcError

_LOGGER = logging.getLogger(__name__)


class ShellyBleRpcCoordinator(DataUpdateCoordinator[dict]):
    """Coordinate all status updates from a Shelly over BLE RPC."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: ShellyBleRpcClient,
        address: str,
        name: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{name} BLE RPC",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL_SECONDS),
        )
        self.client = client
        self.address = address

    async def _async_update_data(self) -> dict:
        """Fetch all device/component status with one RPC transaction."""
        try:
            data = await self.client.get_status()

            # RSSI here is the BLE advertisement RSSI observed by HA's best
            # connectable scanner. It is intentionally NOT Wi-Fi RSSI.
            service_info = bluetooth.async_last_service_info(
                self.hass,
                self.address,
                connectable=True,
            )
            data["_ble_rssi"] = (
                service_info.rssi if service_info is not None else None
            )
            return data

        except ShellyBleRpcError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed(f"BLE RPC temporarily unavailable: {err}") from err
    async def async_refresh_immediate(self) -> dict:
        """Refresh immediately, bypassing DataUpdateCoordinator debounce.
    
        Intended for command confirmation after Switch.Set while a short-lived
        BLE GATT lease is still active.
        """
        data = await self._async_update_data()
        self.async_set_updated_data(data)
        return data
