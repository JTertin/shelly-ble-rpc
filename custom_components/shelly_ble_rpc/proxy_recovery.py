"""Refresh discovery on the active Bluetooth backend without removing bonds."""

from __future__ import annotations

import asyncio
import logging

from bleak import BleakClient

from .const import GATT_CACHE_TIMEOUT

_LOGGER = logging.getLogger(__name__)


async def async_clear_gatt_cache(client: BleakClient, address: str) -> bool:
    """Clear only a cache implemented by the connected backend itself.

    ESPHome's native method clears both process and proxy caches without
    unpairing. Do not use the HA wrapper's generic clear_cache fallback: on
    BlueZ it can call RemoveDevice, which also removes a bond. The backend
    must still be connected so it can contact its serving proxy.
    """
    backend = getattr(client, "_backend", None)
    clear_cache = getattr(backend, "clear_cache", None)
    if clear_cache is None:
        _LOGGER.warning("The Bluetooth backend for %s cannot clear GATT cache", address)
        return False
    try:
        cleared = await asyncio.wait_for(clear_cache(), timeout=GATT_CACHE_TIMEOUT)
    except Exception as err:
        _LOGGER.warning("Unable to clear GATT cache for %s: %s", address, err)
        return False
    if not cleared:
        _LOGGER.warning(
            "The Bluetooth backend did not confirm cache clearing for %s", address
        )
        return False
    _LOGGER.info(
        "Cleared GATT cache for %s; reconnecting to rediscover RPC handles", address
    )
    return True
