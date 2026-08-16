from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ShellyBleRpcConfigEntry
from .entity import ShellyBleRpcEntity, discovered_switch_ids


@dataclass(frozen=True, kw_only=True)
class ShellyBleBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[dict, int], bool | None]


def _errors(status: dict, switch_id: int) -> list[str]:
    errors = status.get(f"switch:{switch_id}", {}).get("errors", [])
    return errors if isinstance(errors, list) else []


def _error_active(status: dict, switch_id: int, error_name: str) -> bool:
    return error_name in _errors(status, switch_id)


PROTECTION_SENSORS: tuple[ShellyBleBinaryDescription, ...] = (
    ShellyBleBinaryDescription(
        key="overcurrent",
        name="Overcurrent",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, i: _error_active(d, i, "overcurrent"),
    ),
    ShellyBleBinaryDescription(
        key="overheating",
        name="Overheating",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, i: _error_active(d, i, "overtemp"),
    ),
    ShellyBleBinaryDescription(
        key="overpowering",
        name="Overpowering",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, i: _error_active(d, i, "overpower"),
    ),
    ShellyBleBinaryDescription(
        key="overvoltage",
        name="Overvoltage",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, i: _error_active(d, i, "overvoltage"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ShellyBleRpcConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    status = entry.runtime_data.coordinator.data
    switch_ids = discovered_switch_ids(status)
    entities = []

    if "input:0" in status:
        entities.append(ShellyBleRpcInput0(entry))

    for index, switch_id in enumerate(switch_ids):
        for description in PROTECTION_SENSORS:
            entities.append(
                ShellyBleRpcProtectionSensor(
                    entry,
                    description,
                    switch_id=switch_id,
                    primary=(index == 0),
                )
            )

    entities.extend(
        [
            ShellyBleRpcCloudSensor(entry),
            ShellyBleRpcRestartRequiredSensor(entry),
        ]
    )

    async_add_entities(entities)


class ShellyBleRpcInput0(ShellyBleRpcEntity, BinarySensorEntity):
    _attr_name = "Input 0"
    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(self, entry: ShellyBleRpcConfigEntry) -> None:
        super().__init__(entry, entry.runtime_data.coordinator, "input_0")

    @property
    def is_on(self):
        value = self.coordinator.data.get("input:0", {}).get("state")
        return bool(value) if value is not None else None


class ShellyBleRpcProtectionSensor(ShellyBleRpcEntity, BinarySensorEntity):
    def __init__(
        self,
        entry: ShellyBleRpcConfigEntry,
        description: ShellyBleBinaryDescription,
        *,
        switch_id: int,
        primary: bool,
    ) -> None:
        self.entity_description = description
        self._switch_id = switch_id
        key = description.key if primary else f"switch_{switch_id}_{description.key}"
        super().__init__(entry, entry.runtime_data.coordinator, key)
        if not primary:
            self._attr_name = f"{description.name} Switch {switch_id}"

    @property
    def is_on(self):
        return bool(
            self.entity_description.value_fn(
                self.coordinator.data,
                self._switch_id,
            )
        )


class ShellyBleRpcCloudSensor(ShellyBleRpcEntity, BinarySensorEntity):
    _attr_name = "Cloud"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ShellyBleRpcConfigEntry) -> None:
        super().__init__(entry, entry.runtime_data.coordinator, "cloud")

    @property
    def is_on(self):
        value = self.coordinator.data.get("cloud", {}).get("connected")
        return bool(value) if value is not None else None


class ShellyBleRpcRestartRequiredSensor(ShellyBleRpcEntity, BinarySensorEntity):
    _attr_name = "Restart required"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ShellyBleRpcConfigEntry) -> None:
        super().__init__(
            entry,
            entry.runtime_data.coordinator,
            "restart_required",
        )

    @property
    def is_on(self):
        value = self.coordinator.data.get("sys", {}).get("restart_required")
        return bool(value) if value is not None else None
