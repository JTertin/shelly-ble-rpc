from __future__ import annotations

import asyncio
import json
import logging
import re
import struct
from collections.abc import Awaitable, Callable
from itertools import count
from typing import Any

from bleak import BleakClient
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .bluez_pairing import (
    async_bluez_available,
    async_pair_device,
)
from .const import (
    BLE_CHUNK_SIZE,
    CONNECTION_LEASE_SECONDS,
    CONNECTION_PAIRING_ATTEMPTS,
    CONNECTION_PAIRING_RETRY_SECONDS,
    PAIRING_TIMEOUT,
    RPC_CHAR_DATA_UUID,
    RPC_CHAR_RX_CTL_UUID,
    RPC_CHAR_TX_CTL_UUID,
    RPC_SOURCE,
    RPC_TIMEOUT,
    SHELLY_GATT_SERVICE_UUID,
    TX_CONTROL_DELAY_SECONDS,
)
from .proxy_recovery import async_clear_gatt_cache

_LOGGER = logging.getLogger(__name__)


def is_write_rejected_error(err: BaseException) -> bool:
    """Detect invalid attribute length, which can indicate stale GATT handles."""
    text = str(err).lower()
    return bool(
        re.search(r"error=13(?!\d)", text)
        or "invalid attribute length" in text
        or "invalid attribute value length" in text
        or re.search(r"error \(13\)", text)
    )


_RPC_WRITE_REJECTED_HINT = (
    "invalid attribute length (ATT 0x0D) may indicate stale GATT handles, "
    "especially after a firmware update. The integration will clear the "
    "active connection's GATT cache and rediscover the RPC service once. "
    "Existing Bluetooth bonds are preserved."
)


class ShellyBleRpcError(Exception):
    """Base error for Shelly BLE RPC."""


class ShellyBleRpcUnavailable(ShellyBleRpcError):
    """Device is unavailable over Bluetooth."""


class ShellyBleRpcPairingUnavailable(ShellyBleRpcError):
    """Pairing cannot be initiated by Home Assistant in this environment.

    Typical when no BlueZ adapter is available and the Shelly is reached
    through an ESPHome Bluetooth proxy, which performs Bluetooth bonding
    itself while the Shelly is accepting pairing.
    """


class ShellyBleRpcStaleCache(ShellyBleRpcError):
    """Cached GATT handles failed validation before any RPC payload was sent."""


async def async_bond_via_connection(
    client: ShellyBleRpcClient,
    address: str,
    *,
    entry_title: str,
) -> None:
    """Request security on the selected connection and validate a real RPC."""
    previous_pairing = client.pair_on_connect
    client.pair_on_connect = True
    try:
        await _async_bond_via_connection_attempts(client, address, entry_title)
    finally:
        client.pair_on_connect = previous_pairing


async def _async_bond_via_connection_attempts(
    client: ShellyBleRpcClient,
    address: str,
    entry_title: str,
) -> None:
    """Run the connection-validated bonding attempt loop."""
    last_err: BaseException | None = None

    for attempt in range(1, CONNECTION_PAIRING_ATTEMPTS + 1):
        try:
            await client.get_status()
        except Exception as err:
            last_err = err
            _LOGGER.info(
                "Connection-validated bonding attempt %d/%d for '%s' (%s) "
                "failed: %s: %s",
                attempt,
                CONNECTION_PAIRING_ATTEMPTS,
                entry_title,
                address,
                type(err).__name__,
                err,
            )
            if attempt < CONNECTION_PAIRING_ATTEMPTS:
                await asyncio.sleep(CONNECTION_PAIRING_RETRY_SECONDS)
        else:
            _LOGGER.info(
                "Bluetooth bond validated over an RPC transaction for '%s' (%s)",
                entry_title,
                address,
            )
            return

    raise ShellyBleRpcUnavailable(
        "Unable to complete Bluetooth bonding for "
        f"{entry_title} ({address}). Put the Shelly into Bluetooth pairing "
        "mode and confirm it is within reliable range of the active "
        "Bluetooth adapter or ESPHome Bluetooth proxy, then Retry pairing "
        "or Reload the integration."
    ) from last_err


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
            raise ShellyBleRpcStaleCache(
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

        _LOGGER.debug(
            "Resolved Shelly RPC characteristics for %s: service handle "
            "%s, data handle %s, tx_ctl handle %s, rx_ctl handle %s",
            self.address,
            service.handle,
            chars[self._normalize_uuid(RPC_CHAR_DATA_UUID)].handle,
            chars[self._normalize_uuid(RPC_CHAR_TX_CTL_UUID)].handle,
            chars[self._normalize_uuid(RPC_CHAR_RX_CTL_UUID)].handle,
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
        """Create the client; pairing requests never delete existing bonds."""
        self.hass = hass
        self.address = address
        self.pair_on_connect = pair_on_connect
        self._ids = count(1)
        self._lock = asyncio.Lock()
        self._leased_client: BleakClient | None = None
        self._lease_task: asyncio.Task | None = None
        self._rejected_write_hint_logged = False

    def _log_rejected_write(self, err: BaseException) -> None:
        """Explain invalid attribute length once, then log compactly."""
        if not is_write_rejected_error(err):
            return
        if self._rejected_write_hint_logged:
            _LOGGER.debug(
                "BLE RPC write to %s rejected; GATT cache may be stale: %s: %s",
                self.address,
                type(err).__name__,
                err,
            )
            return
        self._rejected_write_hint_logged = True
        _LOGGER.info(
            "BLE RPC write to %s rejected: %s", self.address, _RPC_WRITE_REJECTED_HINT
        )

    async def _validate_rpc_service(self, client: BleakClient) -> None:
        """Check the read-only control value before writing to cached handles.

        RX control must be exactly four bytes, even when no response is ready.
        A stale proxy cache was observed mapping it to the service declaration
        instead. A UUID match in the cached service collection is not enough.
        """
        _, _, _, rx_ctl = self._resolve_rpc_characteristics(client)
        value = await client.read_gatt_char(rx_ctl)
        if len(value) != 4:
            raise ShellyBleRpcStaleCache(
                f"RX control at handle {rx_ctl.handle} returned {len(value)} "
                "bytes instead of 4; the GATT service cache may be stale"
            )

    async def _connect(self, *, use_services_cache: bool = True) -> BleakClient:
        """Connect and request security separately from the RPC deadline."""
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
                BleakClientWithServiceCache,
                ble_device,
                ble_device.name or self.address,
                max_attempts=3,
                use_services_cache=use_services_cache,
                pair=False,
                timeout=30.0,
            )
        except Exception as err:
            _LOGGER.warning(
                "Normal BLE connection to %s failed without altering the bond: %s: %s",
                self.address,
                type(err).__name__,
                err,
            )
            raise ShellyBleRpcUnavailable(
                f"Unable to connect to {self.address}: {err}"
            ) from err

        try:
            if self.pair_on_connect:
                result = await asyncio.wait_for(client.pair(), timeout=PAIRING_TIMEOUT)
                # Older backends return bool; newer Bleak versions return None.
                if result is False:
                    raise ShellyBleRpcError(
                        f"Bluetooth pairing failed for {self.address}"
                    )
            return client
        except BaseException:
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

    async def _get_rpc_client(self, *, use_services_cache: bool = True) -> BleakClient:
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

        client = await self._connect(use_services_cache=use_services_cache)
        self._leased_client = client
        await asyncio.wait_for(self._validate_rpc_service(client), timeout=RPC_TIMEOUT)
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

            if not await async_bluez_available():
                # Without a local BlueZ adapter, Home Assistant cannot run a
                # client-side pairing agent. The ESPHome Bluetooth proxy bonds
                # with the Shelly by itself while the Shelly is accepting
                # Bluetooth pairing, so the correct action is a plain connect.
                raise ShellyBleRpcPairingUnavailable(
                    "Bluetooth pairing cannot be initiated because no local "
                    f"BlueZ adapter is accessible for {self.address}. When the "
                    "Shelly is reached through an ESPHome Bluetooth proxy, "
                    "put the Shelly into Bluetooth pairing mode and let the "
                    "proxy connect: the proxy performs the bond itself."
                )

            _LOGGER.info(
                "Starting explicit BlueZ pairing for %s",
                self.address,
            )
            await async_pair_device(self.address, timeout=30.0)

    async def _execute(self, operation: Callable[[BleakClient], Awaitable[Any]]) -> Any:
        """Retry once only when stale handles are found before payload writes.

        Pairing, cache clearing, and reconnection have separate deadlines. Never
        replay a request after any payload bytes may have reached the device.
        The caller holds the per-device lock throughout recovery.
        """
        for attempt in range(2):
            try:
                client = await self._get_rpc_client(use_services_cache=attempt == 0)
                return await asyncio.wait_for(operation(client), timeout=RPC_TIMEOUT)
            except ShellyBleRpcStaleCache:
                try:
                    client = self._leased_client
                    if attempt or client is None:
                        raise
                    if not await async_clear_gatt_cache(client, self.address):
                        raise ShellyBleRpcError(
                            f"Unable to refresh GATT cache for {self.address}; "
                            "reload the Bluetooth adapter or ESPHome proxy entry"
                        )
                finally:
                    await self._drop_leased_connection()
            except TimeoutError as err:
                await self._drop_leased_connection()
                raise ShellyBleRpcError(
                    f"BLE RPC operation timed out for {self.address}"
                ) from err
            except BaseException:
                await self._drop_leased_connection()
                raise
        raise ShellyBleRpcError("GATT cache recovery did not complete")

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict:
        """Execute one RPC, refreshing stale GATT discovery once if necessary."""
        async with self._lock:
            result = await self._execute(
                lambda client: self._call_connected(client, method, params or {})
            )
            client = self._leased_client
            assert client is not None
            self._renew_lease(client)
            return result

    async def _write_payload(self, client: BleakClient, payload: bytes) -> None:
        """Write a frame, classifying only a failed header as safe to retry."""
        _, data_char, tx_ctl_char, _ = self._resolve_rpc_characteristics(client)
        try:
            await client.write_gatt_char(
                tx_ctl_char, struct.pack(">I", len(payload)), response=True
            )
        except Exception as err:
            if is_write_rejected_error(err):
                self._log_rejected_write(err)
                raise ShellyBleRpcStaleCache(
                    "TX control rejected a four-byte length; GATT handles may be stale"
                ) from err
            raise

        await asyncio.sleep(TX_CONTROL_DELAY_SECONDS)
        for offset in range(0, len(payload), BLE_CHUNK_SIZE):
            await client.write_gatt_char(
                data_char, payload[offset : offset + BLE_CHUNK_SIZE], response=True
            )

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

        _, data_char, _, rx_ctl_char = self._resolve_rpc_characteristics(client)

        await self._write_payload(client, payload)

        # A valid zero length means the response is not ready yet.
        while True:
            rx_ctl = await client.read_gatt_char(rx_ctl_char)
            if len(rx_ctl) != 4:
                raise ShellyBleRpcError(f"Invalid RX control response: {rx_ctl.hex()}")
            response_length = struct.unpack(">I", bytes(rx_ctl))[0]
            if response_length > 65536:
                raise ShellyBleRpcError(
                    f"Invalid RPC response length: {response_length}"
                )
            if response_length:
                break
            await asyncio.sleep(0.1)

        response = bytearray()
        while len(response) < response_length:
            chunk = await client.read_gatt_char(data_char)
            if not chunk:
                raise ShellyBleRpcError("Shelly returned an empty RPC data chunk")
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
            raise ShellyBleRpcError(f"RPC {method} failed: {decoded['error']}")

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
            try:
                await self._execute(
                    lambda client: self._send_no_response(client, method, params or {})
                )
            finally:
                await self._drop_leased_connection()

    async def _send_no_response(
        self, client: BleakClient, method: str, params: dict[str, Any]
    ) -> None:
        request = {
            "id": next(self._ids),
            "src": RPC_SOURCE,
            "method": method,
            "params": params,
        }
        payload = json.dumps(
            request, separators=(",", ":"), ensure_ascii=False
        ).encode()
        _LOGGER.debug("Shelly BLE RPC %s -> %s (no-response)", self.address, request)
        await self._write_payload(client, payload)
        await asyncio.sleep(0.15)

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
