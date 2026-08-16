from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ShellyBleRpcConfigEntry
from .const import CONF_ADDRESS, CONF_NAME, DOMAIN
from .coordinator import ShellyBleRpcCoordinator


class ShellyBleRpcEntity(CoordinatorEntity[ShellyBleRpcCoordinator]):
    """Base entity for one Shelly BLE RPC device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ShellyBleRpcConfigEntry,
        coordinator: ShellyBleRpcCoordinator,
        entity_key: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry

        address = entry.data[CONF_ADDRESS]
        compact_address = address.replace(":", "").lower()
        self._attr_unique_id = f"{compact_address}_{entity_key}"

        info = entry.runtime_data.device_info
        model = info.get("app") or info.get("model") or "Gen4 BLE RPC device"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={("bluetooth", address)},
            name=entry.data[CONF_NAME],
            manufacturer="Shelly",
            model=str(model),
            sw_version=info.get("ver"),
            serial_number=info.get("id"),
        )

    @property
    def available(self) -> bool:
        """Entity is available when the coordinator has valid device status."""
        return self.coordinator.last_update_success


def discovered_switch_ids(status: dict) -> list[int]:
    """Return numeric Shelly switch component IDs from Shelly.GetStatus."""
    ids: list[int] = []
    for key in status:
        if not key.startswith("switch:"):
            continue
        try:
            ids.append(int(key.split(":", 1)[1]))
        except (TypeError, ValueError):
            continue
    return sorted(set(ids))
