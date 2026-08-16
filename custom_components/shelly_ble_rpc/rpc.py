from __future__ import annotations

import asyncio
import json
import logging
import struct
from itertools import count
from typing import Any

from bleak import BleakClient
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .bluez_pairing import async_pair_device, async_remove_bluez_device

from .const import (
    BLE_CHUNK_SIZE,
    CONNECTION_LEASE_SECONDS,
    RPC_CHAR_DATA_UUID,
    RPC_CHAR_RX_CTL_UUID,
    RPC_CHAR_TX_CTL_UUID,
    RPC_SOURCE,
    RPC_TIMEOUT,
    SHELLY_GATT_SERVICE_UUID,
)

_LOGGER = logging.getLogger(__name__)


class ShellyBleRpcError(Exception):
    """Base error for Shelly BLE RPC."""


class ShellyBleRpcUnavailable(ShellyBleRpcError):
    """Device is unavailable over Bluetooth."""


class ShellyBleRpcClient:
    """Minimal Shelly RPC-over-BLE client."""

    @staticmethod
    def _normalize_uuid(value: str) -> str:
        return str(value).lower()

    def _resolve_rpc_characteristics(self, client: BleakClient):
        """Resolve concrete Shelly RPC characteristic objects by service handle."""
        service_uuid = self._normalize_uuid(SHELLY_GATT_SERVICE_UUID)
        required = {
            self._normalize_uuid(RPC_CHAR_DATA_UUID),
            self._normalize_uuid(RPC_CHAR_TX_CTL_UUID),
            self._normalize_uuid(RPC_CHAR_RX_CTL_UUID),
        }

        candidates = []
        for service in client.services.services.values():
            if self._normalize_uuid(service.uuid) != service_uuid:
                continue
            chars = {
                self._normalize_uuid(char.uuid): char
                for char in service.characteristics
            }
            if required.issubset(chars):
                candidates.append((service, chars))

        if not candidates:
            raise ShellyBleRpcError(
                "Connected device does not expose a complete Shelly BLE RPC GATT service"
            )

        candidates.sort(key=lambda item: item[0].handle)
        service, chars = candidates[0]

        if len(candidates) > 1:
            _LOGGER.warning(
                "Shelly %s exposes %d RPC services with UUID %s; using handle %s "
                "(available handles: %s)",
                self.address,
                len(candidates),
                SHELLY_GATT_SERVICE_UUID,
                service.handle,
                [item[0].handle for item in candidates],
            )

        return (
            service,
            chars[self._normalize_uuid(RPC_CHAR_DATA_UUID)],
            chars[self._normalize_uuid(RPC_CHAR_TX_CTL_UUID)],
            chars[self._normalize_uuid(RPC_CHAR_RX_CTL_UUID)],
        )

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        pair_on_connect: bool = True,
    ) -> None:
        self.hass = hass
        self.address = address
        self.pair_on_connect = pair_on_connect
        self._ids = count(1)
        self._lock = asyncio.Lock()
        self._leased_client: BleakClient | None = None
        self._lease_task: asyncio.Task | None = None

    async def _connect(self) -> BleakClient:
        """Connect using the existing bond when available.

        Normal operation never deletes a bond. Pairing is attempted only when
        BlueZ reports that no usable bond/device object exists and the Shelly
        is explicitly available for pairing.
        """
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass,
            self.address,
            connectable=True,
        )
        if ble_device is None:
            raise ShellyBleRpcUnavailable(
                f"{self.address} is temporarily unavailable over Bluetooth"
            )

        # Startup/transient safety: do not interpret an early timeout as a
        # pairing problem. Try the ordinary bonded connection first.
        _LOGGER.debug(
            "Starting normal BLE GATT connection attempt to %s "
            "(non-destructive, pair=False)",
            self.address,
        )

        try:
            client = await establish_connection(
                BleakClient,
                ble_device,
                ble_device.name or self.address,
                max_attempts=1,
                pair=False,
                timeout=30.0,
            )
        except Exception as err:
            _LOGGER.warning(
                "Normal BLE connection to %s failed without altering the bond: "
                "%s: %s",
                self.address,
                type(err).__name__,
                err,
            )
            raise ShellyBleRpcUnavailable(
                f"Unable to connect to {self.address}: {err}"
            ) from err

        try:
            self._resolve_rpc_characteristics(client)
            return client
        except Exception:
            if client.is_connected:
                await client.disconnect()
            raise

    def _cancel_lease_task(self) -> None:
        """Cancel the current idle-disconnect task."""
        task = self._lease_task
        self._lease_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _drop_leased_connection(self) -> None:
        """Disconnect and forget the currently leased GATT client."""
        self._cancel_lease_task()

        client = self._leased_client
        self._leased_client = None

        if client is not None and client.is_connected:
            try:
                await client.disconnect()
                _LOGGER.debug(
                    "Disconnected BLE GATT lease for %s",
                    self.address,
                )
            except Exception as err:
                _LOGGER.debug(
                    "Error disconnecting BLE GATT lease for %s: %s",
                    self.address,
                    err,
                )

    async def _lease_expiry_worker(self, client: BleakClient) -> None:
        """Disconnect the leased connection after the idle timeout."""
        try:
            await asyncio.sleep(CONNECTION_LEASE_SECONDS)

            async with self._lock:
                if self._leased_client is not client:
                    return

                _LOGGER.debug(
                    "BLE GATT connection lease for %s expired after %.1f seconds",
                    self.address,
                    CONNECTION_LEASE_SECONDS,
                )

                self._lease_task = None
                self._leased_client = None

                if client.is_connected:
                    try:
                        await client.disconnect()
                    except Exception as err:
                        _LOGGER.debug(
                            "Error disconnecting expired BLE lease for %s: %s",
                            self.address,
                            err,
                        )
        except asyncio.CancelledError:
            return

    def _renew_lease(self, client: BleakClient) -> None:
        """Reset the short idle lease on a successful RPC."""
        self._cancel_lease_task()
        self._lease_task = self.hass.async_create_task(
            self._lease_expiry_worker(client),
            f"Shelly BLE RPC lease {self.address}",
        )
        _LOGGER.debug(
            "BLE GATT connection lease for %s renewed for %.1f seconds",
            self.address,
            CONNECTION_LEASE_SECONDS,
        )

    async def _get_rpc_client(self) -> BleakClient:
        """Reuse the leased GATT connection or establish a new one."""
        client = self._leased_client

        if client is not None and client.is_connected:
            self._cancel_lease_task()
            _LOGGER.debug(
                "Reusing active BLE GATT connection lease for %s",
                self.address,
            )
            return client

        if client is not None:
            self._leased_client = None
            self._cancel_lease_task()

        client = await self._connect()
        self._leased_client = client
        return client

    async def async_shutdown(self) -> None:
        """Disconnect any live GATT lease when the config entry unloads."""
        async with self._lock:
            await self._drop_leased_connection()

    async def pair_explicitly(self) -> None:
        """Explicitly pair/bond without deleting an existing BlueZ bond.

        Intended for setup/reload while the Shelly is in pairing mode.
        """
        async with self._lock:
            await self._drop_leased_connection()
            _LOGGER.info(
                "Starting explicit BlueZ pairing for %s",
                self.address,
            )
            await async_pair_device(self.address, timeout=30.0)

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict:
        """Execute one RPC using a short-lived reusable GATT connection."""
        async with self._lock:
            client = await self._get_rpc_client()

            try:
                result = await asyncio.wait_for(
                    self._call_connected(client, method, params or {}),
                    timeout=RPC_TIMEOUT,
                )
            except asyncio.TimeoutError as err:
                await self._drop_leased_connection()
                raise ShellyBleRpcError(
                    f"RPC {method} timed out after {RPC_TIMEOUT} seconds"
                ) from err
            except Exception:
                await self._drop_leased_connection()
                raise

            self._renew_lease(client)
            return result

    async def _call_once(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict:
        """Execute exactly one connect/RPC/disconnect transaction."""
        client = await self._connect()
        try:
            return await asyncio.wait_for(
                self._call_connected(client, method, params),
                timeout=RPC_TIMEOUT,
            )
        except asyncio.TimeoutError as err:
            raise ShellyBleRpcError(
                f"RPC {method} timed out after {RPC_TIMEOUT} seconds"
            ) from err
        finally:
            if client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass



    async def _call_connected(
        self,
        client: BleakClient,
        method: str,
        params: dict[str, Any],
    ) -> dict:
        request_id = next(self._ids)
        request = {
            "id": request_id,
            "src": RPC_SOURCE,
            "method": method,
            "params": params,
        }
        payload = json.dumps(
            request,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

        _LOGGER.debug("Shelly BLE RPC %s -> %s", self.address, request)

        _, data_char, tx_ctl_char, rx_ctl_char = self._resolve_rpc_characteristics(client)

        await client.write_gatt_char(
            tx_ctl_char,
            struct.pack(">I", len(payload)),
            response=True,
        )

        await asyncio.sleep(0.1)

        for offset in range(0, len(payload), BLE_CHUNK_SIZE):
            await client.write_gatt_char(
                data_char,
                payload[offset : offset + BLE_CHUNK_SIZE],
                response=True,
            )

        rx_ctl = await client.read_gatt_char(rx_ctl_char)
        if len(rx_ctl) < 4:
            raise ShellyBleRpcError(
                f"Invalid RX control response: {rx_ctl.hex()}"
            )

        response_length = struct.unpack(">I", bytes(rx_ctl[:4]))[0]
        if response_length <= 0 or response_length > 65536:
            raise ShellyBleRpcError(
                f"Invalid RPC response length: {response_length}"
            )

        response = bytearray()
        while len(response) < response_length:
            chunk = await client.read_gatt_char(data_char)
            if not chunk:
                raise ShellyBleRpcError(
                    "Shelly returned an empty RPC data chunk"
                )
            remaining = response_length - len(response)
            response.extend(bytes(chunk[:remaining]))

        try:
            decoded = json.loads(bytes(response).decode("utf-8"))
        except Exception as err:
            raise ShellyBleRpcError(
                f"Unable to decode RPC response: {bytes(response)!r}"
            ) from err

        _LOGGER.debug("Shelly BLE RPC %s <- %s", self.address, decoded)

        if decoded.get("id") != request_id:
            raise ShellyBleRpcError(
                f"RPC response ID mismatch: expected {request_id}, "
                f"received {decoded.get('id')}"
            )

        if "error" in decoded:
            raise ShellyBleRpcError(
                f"RPC {method} failed: {decoded['error']}"
            )

        if "result" not in decoded:
            raise ShellyBleRpcError(
                f"RPC {method} response contained no result: {decoded}"
            )

        return decoded["result"]

    async def call_no_response(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Send an RPC request but intentionally do not read its response.

        This is needed for RPC methods such as BLE.DeletePairedDevice where
        executing the command invalidates the authenticated BLE session before
        the response can be read.
        """
        async with self._lock:
            client = await self._get_rpc_client()
            try:
                request_id = next(self._ids)
                request = {
                    "id": request_id,
                    "src": RPC_SOURCE,
                    "method": method,
                    "params": params or {},
                }
                payload = json.dumps(
                    request,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")

                _LOGGER.debug(
                    "Shelly BLE RPC %s -> %s (no-response)",
                    self.address,
                    request,
                )

                _, data_char, tx_ctl_char, _ = self._resolve_rpc_characteristics(client)

                await client.write_gatt_char(
                    tx_ctl_char,
                    struct.pack(">I", len(payload)),
                    response=True,
                )

                await asyncio.sleep(0.1)

                for offset in range(0, len(payload), BLE_CHUNK_SIZE):
                    await client.write_gatt_char(
                        data_char,
                        payload[offset : offset + BLE_CHUNK_SIZE],
                        response=True,
                    )

                # Give the Shelly a brief opportunity to execute the command
                # before we intentionally tear down the connection. We do not
                # read RX_CTL because deleting the bond can invalidate the
                # encrypted GATT session immediately.
                await asyncio.sleep(0.15)

            finally:
                # Destructive/no-response operations must never retain the
                # encrypted GATT session.
                await self._drop_leased_connection()

    async def get_status(self) -> dict:
        """Return status of all Shelly components in one RPC call."""
        return await self.call("Shelly.GetStatus")

    async def get_device_info(self) -> dict:
        """Return identifying device information."""
        return await self.call("Shelly.GetDeviceInfo")

    async def get_switch_status(self, switch_id: int = 0) -> dict:
        """Return one switch status."""
        return await self.call("Switch.GetStatus", {"id": switch_id})

    async def set_switch(self, switch_id: int, on: bool) -> dict:
        """Set switch output."""
        return await self.call("Switch.Set", {"id": switch_id, "on": on})

    async def reboot(self, delay_ms: int = 1000) -> dict:
        """Reboot the Shelly."""
        return await self.call("Shelly.Reboot", {"delay_ms": delay_ms})

    async def list_paired_devices(self) -> list[dict]:
        """Return BLE devices bonded to the Shelly."""
        result = await self.call("BLE.ListPairedDevices")
        return result if isinstance(result, list) else []

    async def delete_paired_device(self, address: str) -> None:
        """Delete one bonded BLE controller from the Shelly.

        Deleting the active controller bond can immediately invalidate the
        encrypted GATT session, so this RPC is deliberately sent without
        waiting for a response.
        """
        await self.call_no_response(
            "BLE.DeletePairedDevice",
            {"addr": address},
        )
