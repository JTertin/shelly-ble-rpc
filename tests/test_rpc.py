"""Regression coverage for stale proxy handles and non-destructive recovery."""

import asyncio
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ADDRESS = "A0:85:E3:BD:B0:86"


class GattClient:
    def __init__(
        self, rpc, *, stale=False, header_error=None, payload_error=None, ready_delay=0
    ):
        self._backend = self
        self.is_connected = True
        self.stale = stale
        self.header_error = header_error
        self.payload_error = payload_error
        self.ready_delay = ready_delay
        self.writes = []
        self.pair = AsyncMock(return_value=None)
        self.clear_cache = AsyncMock(return_value=True)
        self.request = bytearray()
        self.response = b""
        handles = (3, 9, 6) if stale else (8, 14, 11)
        uuids = (
            rpc.RPC_CHAR_DATA_UUID,
            rpc.RPC_CHAR_TX_CTL_UUID,
            rpc.RPC_CHAR_RX_CTL_UUID,
        )
        self.data, self.tx, self.rx = [
            SimpleNamespace(uuid=u, handle=h)
            for u, h in zip(uuids, handles, strict=True)
        ]
        service = SimpleNamespace(
            uuid=rpc.SHELLY_GATT_SERVICE_UUID,
            handle=1 if stale else 6,
            characteristics=[self.data, self.tx, self.rx],
        )
        self.services = SimpleNamespace(services={service.handle: service})

    async def disconnect(self):
        self.is_connected = False

    async def read_gatt_char(self, char):
        if char is self.rx:
            if self.stale:
                return bytes.fromhex("5f44495f4356535f4350525f534f6d5f")
            if self.response and self.ready_delay:
                self.ready_delay -= 1
                return bytes(4)
            return struct.pack(">I", len(self.response))
        chunk, self.response = self.response[:20], self.response[20:]
        return chunk

    async def write_gatt_char(self, char, value, response):
        assert response is True
        self.writes.append((char.handle, bytes(value)))
        if char is self.tx:
            if self.header_error:
                raise self.header_error
            self.expected = struct.unpack(">I", value)[0]
            self.request.clear()
        else:
            if self.payload_error:
                raise self.payload_error
            self.request.extend(value)
            if len(self.request) == self.expected:
                request = json.loads(self.request)
                self.response = json.dumps(
                    {"id": request["id"], "result": {"ok": True}}
                ).encode()


def transport(rpc, monkeypatch, clients, **kwargs):
    connector = AsyncMock(side_effect=clients)
    monkeypatch.setattr(rpc, "establish_connection", connector)
    hass = SimpleNamespace(
        async_create_task=lambda coroutine, name: asyncio.create_task(
            coroutine, name=name
        )
    )
    return rpc.ShellyBleRpcClient(hass, ADDRESS, **kwargs), connector


@pytest.mark.asyncio
async def test_stale_map_is_detected_before_writing_and_only_active_cache_is_cleared(
    rpc, monkeypatch
):
    old, fresh = GattClient(rpc, stale=True), GattClient(rpc)
    client, connect = transport(rpc, monkeypatch, [old, fresh])
    try:
        assert await client.get_status() == {"ok": True}
        assert old.writes == []
        old.clear_cache.assert_awaited_once()
        fresh.clear_cache.assert_not_awaited()
        assert not old.is_connected
        assert [
            call.kwargs["use_services_cache"] for call in connect.await_args_list
        ] == [True, False]
        assert fresh.writes[0][0] == 14
        assert all(handle == 8 for handle, _ in fresh.writes[1:])
    finally:
        await client.async_shutdown()


@pytest.mark.asyncio
async def test_failed_header_can_refresh_cache_once(rpc, monkeypatch):
    old = GattClient(
        rpc, header_error=RuntimeError("handle=9 error=13 Invalid attribute length")
    )
    fresh = GattClient(rpc)
    client, _ = transport(rpc, monkeypatch, [old, fresh])
    try:
        assert await client.get_status() == {"ok": True}
        assert len(old.writes) == 1
        old.clear_cache.assert_awaited_once()
    finally:
        await client.async_shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("error=13"), TimeoutError("radio timeout")]
)
async def test_never_replays_after_payload_may_have_been_sent(
    rpc, monkeypatch, failure
):
    backend = GattClient(rpc, payload_error=failure)
    client, connect = transport(rpc, monkeypatch, [backend])
    with pytest.raises((RuntimeError, rpc.ShellyBleRpcError)):
        await client.set_switch(0, True)
    assert connect.await_count == 1
    backend.clear_cache.assert_not_awaited()
    assert not backend.is_connected


@pytest.mark.asyncio
async def test_transport_header_error_does_not_trigger_cache_recovery(rpc, monkeypatch):
    backend = GattClient(rpc, header_error=RuntimeError("connection lost"))
    client, connect = transport(rpc, monkeypatch, [backend])
    with pytest.raises(RuntimeError, match="connection lost"):
        await client.get_status()
    assert connect.await_count == 1
    backend.clear_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_stale_map_is_bounded(rpc, monkeypatch):
    old, still_old = GattClient(rpc, stale=True), GattClient(rpc, stale=True)
    client, connect = transport(rpc, monkeypatch, [old, still_old])
    with pytest.raises(rpc.ShellyBleRpcStaleCache):
        await client.get_status()
    assert connect.await_count == 2
    still_old.clear_cache.assert_not_awaited()
    assert not old.is_connected and not still_old.is_connected


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [False, RuntimeError("cache clear refused")])
async def test_cache_failure_does_not_claim_success_or_reconnect(
    rpc, monkeypatch, result
):
    old = GattClient(rpc, stale=True)
    old.clear_cache = AsyncMock(
        **(
            {"side_effect": result}
            if isinstance(result, Exception)
            else {"return_value": result}
        )
    )
    client, connect = transport(rpc, monkeypatch, [old])
    with pytest.raises(rpc.ShellyBleRpcError, match="Unable to refresh GATT cache"):
        await client.get_status()
    assert connect.await_count == 1
    assert not old.is_connected


@pytest.mark.asyncio
async def test_zero_rx_length_waits_for_response_and_lease_is_reused(rpc, monkeypatch):
    backend = GattClient(rpc, ready_delay=2)
    client, connect = transport(rpc, monkeypatch, [backend])
    try:
        assert await client.get_status() == {"ok": True}
        assert await client.get_device_info() == {"ok": True}
        assert connect.await_count == 1
        backend.pair.assert_awaited_once()
        backend.clear_cache.assert_not_awaited()
    finally:
        await client.async_shutdown()


@pytest.mark.asyncio
async def test_pairing_budget_is_separate_from_rpc_budget(rpc, monkeypatch):
    backend = GattClient(rpc)

    async def slow_pair():
        await asyncio.sleep(0.05)

    backend.pair = slow_pair
    monkeypatch.setattr(rpc, "RPC_TIMEOUT", 0.02)
    client, _ = transport(rpc, monkeypatch, [backend])
    try:
        assert await client.get_status() == {"ok": True}
    finally:
        await client.async_shutdown()


@pytest.mark.asyncio
async def test_pairing_false_is_respected(rpc, monkeypatch):
    backend = GattClient(rpc)
    client, _ = transport(rpc, monkeypatch, [backend], pair_on_connect=False)
    try:
        assert await client.get_status() == {"ok": True}
        backend.pair.assert_not_awaited()
    finally:
        await client.async_shutdown()


@pytest.mark.asyncio
async def test_no_response_path_recovers_before_payload_and_disconnects(
    rpc, monkeypatch
):
    old, fresh = GattClient(rpc, stale=True), GattClient(rpc)
    client, _ = transport(rpc, monkeypatch, [old, fresh])
    await client.call_no_response(
        "BLE.DeletePairedDevice", {"addr": "AA:BB:CC:DD:EE:FF,p"}
    )
    assert old.writes == []
    assert json.loads(fresh.request)["method"] == "BLE.DeletePairedDevice"
    assert not fresh.is_connected
    assert client._leased_client is None


@pytest.mark.asyncio
async def test_cancelled_operation_releases_connection(rpc, monkeypatch):
    backend = GattClient(rpc)
    entered = asyncio.Event()

    async def blocked_write(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    backend.write_gatt_char = blocked_write
    client, _ = transport(rpc, monkeypatch, [backend])
    task = asyncio.create_task(client.get_status())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not backend.is_connected


@pytest.mark.asyncio
async def test_never_calls_wrapper_cache_fallback_that_can_remove_bluez_bond(
    rpc, monkeypatch
):
    old = GattClient(rpc, stale=True)
    old._backend = SimpleNamespace()  # Native backend has no cache-clear API.
    client, connect = transport(rpc, monkeypatch, [old])
    with pytest.raises(rpc.ShellyBleRpcError, match="Unable to refresh GATT cache"):
        await client.get_status()
    old.clear_cache.assert_not_awaited()
    assert connect.await_count == 1
    assert not old.is_connected


@pytest.mark.asyncio
async def test_pairing_failure_releases_connection_without_cache_clear(
    rpc, monkeypatch
):
    backend = GattClient(rpc)
    backend.pair.return_value = False
    client, _ = transport(rpc, monkeypatch, [backend])
    with pytest.raises(rpc.ShellyBleRpcError, match="Bluetooth pairing failed"):
        await client.get_status()
    backend.clear_cache.assert_not_awaited()
    assert not backend.is_connected


@pytest.mark.asyncio
async def test_bond_validation_temporarily_enables_pairing_and_restores_flag(rpc):
    client = SimpleNamespace(
        pair_on_connect=False, get_status=AsyncMock(return_value={})
    )

    async def get_status():
        assert client.pair_on_connect
        return {}

    client.get_status = get_status
    await rpc.async_bond_via_connection(client, ADDRESS, entry_title="Shelly")
    assert client.pair_on_connect is False


@pytest.mark.asyncio
async def test_incomplete_service_map_is_refreshed(rpc, monkeypatch):
    old, fresh = GattClient(rpc), GattClient(rpc)
    old.services.services.clear()
    client, _ = transport(rpc, monkeypatch, [old, fresh])
    try:
        assert await client.get_status() == {"ok": True}
        assert old.writes == []
        old.clear_cache.assert_awaited_once()
    finally:
        await client.async_shutdown()


@pytest.mark.asyncio
async def test_cancelled_cache_refresh_disconnects_without_reconnecting(
    rpc, monkeypatch
):
    old = GattClient(rpc, stale=True)
    entered = asyncio.Event()

    async def blocked_clear():
        entered.set()
        await asyncio.Event().wait()

    old.clear_cache = blocked_clear
    client, connect = transport(rpc, monkeypatch, [old])
    task = asyncio.create_task(client.get_status())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not old.is_connected
    assert connect.await_count == 1
