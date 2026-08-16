from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ShellyBleRpcConfigEntry
from .entity import ShellyBleRpcEntity, discovered_switch_ids


@dataclass(frozen=True, kw_only=True)
class ShellyBleSensorDescription(SensorEntityDescription):
    """Describe a Shelly BLE RPC sensor."""
    value_fn: Callable[[dict, int], Any]


def _switch(status: dict, switch_id: int) -> dict:
    return status.get(f"switch:{switch_id}", {})


def _switch_value(status: dict, switch_id: int, key: str):
    return _switch(status, switch_id).get(key)


def _energy_kwh(status: dict, switch_id: int, key: str):
    container = _switch(status, switch_id).get(key)
    if not isinstance(container, dict):
        return None
    total_wh = container.get("total")
    if total_wh is None:
        return None
    return round(float(total_wh) / 1000.0, 6)


def _temperature_c(status: dict, switch_id: int):
    temp = _switch(status, switch_id).get("temperature")
    if not isinstance(temp, dict):
        return None
    return temp.get("tC")


def _boot_time(status: dict, switch_id: int):
    sys = status.get("sys", {})
    unix_now = sys.get("unixtime")
    uptime = sys.get("uptime")
    if unix_now is None or uptime is None:
        return None
    try:
        return datetime.fromtimestamp(
            float(unix_now) - float(uptime),
            tz=timezone.utc,
        )
    except (TypeError, ValueError, OSError):
        return None


METERING_SENSORS: tuple[ShellyBleSensorDescription, ...] = (
    ShellyBleSensorDescription(
        key="power",
        name="Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=1,
        value_fn=lambda d, i: _switch_value(d, i, "apower"),
    ),
    ShellyBleSensorDescription(
        key="voltage",
        name="Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
        value_fn=lambda d, i: _switch_value(d, i, "voltage"),
    ),
    ShellyBleSensorDescription(
        key="frequency",
        name="Frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        suggested_display_precision=1,
        value_fn=lambda d, i: _switch_value(d, i, "freq"),
    ),
    ShellyBleSensorDescription(
        key="current",
        name="Current",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=3,
        value_fn=lambda d, i: _switch_value(d, i, "current"),
    ),
    ShellyBleSensorDescription(
        key="energy",
        name="Energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda d, i: _energy_kwh(d, i, "aenergy"),
    ),
    ShellyBleSensorDescription(
        key="energy_returned",
        name="Energy returned",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda d, i: _energy_kwh(d, i, "ret_aenergy"),
    ),
    ShellyBleSensorDescription(
        key="temperature",
        name="Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=1,
        value_fn=_temperature_c,
    ),
)

DEVICE_SENSORS: tuple[ShellyBleSensorDescription, ...] = (
    ShellyBleSensorDescription(
        key="signal_strength",
        name="Signal strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, i: d.get("_ble_rssi"),
    ),
    ShellyBleSensorDescription(
        key="uptime",
        name="Uptime",
        device_class=SensorDeviceClass.UPTIME,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_boot_time,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ShellyBleRpcConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors discovered from Shelly.GetStatus."""
    switch_ids = discovered_switch_ids(entry.runtime_data.coordinator.data)
    entities = []

    for index, switch_id in enumerate(switch_ids):
        for description in METERING_SENSORS:
            # Avoid creating unsupported metering entities on a switch component
            # that does not expose the relevant field/container at all.
            sample = description.value_fn(
                entry.runtime_data.coordinator.data,
                switch_id,
            )
            if sample is None:
                continue

            entities.append(
                ShellyBleRpcSensor(
                    entry,
                    description,
                    switch_id=switch_id,
                    primary_switch=(index == 0),
                )
            )

    # Device-wide diagnostics are created once. Use first switch id only as a
    # harmless argument for shared value functions.
    first_switch_id = switch_ids[0] if switch_ids else 0
    for description in DEVICE_SENSORS:
        entities.append(
            ShellyBleRpcSensor(
                entry,
                description,
                switch_id=first_switch_id,
                primary_switch=True,
                device_wide=True,
            )
        )

    async_add_entities(entities)


class ShellyBleRpcSensor(ShellyBleRpcEntity, SensorEntity):
    """Sensor backed by shared Shelly.GetStatus data."""

    entity_description: ShellyBleSensorDescription

    def __init__(
        self,
        entry: ShellyBleRpcConfigEntry,
        description: ShellyBleSensorDescription,
        *,
        switch_id: int,
        primary_switch: bool,
        device_wide: bool = False,
    ) -> None:
        self.entity_description = description
        self._switch_id = switch_id

        if device_wide or primary_switch:
            entity_key = description.key
        else:
            entity_key = f"switch_{switch_id}_{description.key}"

        super().__init__(
            entry,
            entry.runtime_data.coordinator,
            entity_key,
        )

        # Preserve simple names for a single/primary switch. Additional switch
        # components get explicit suffixes.
        if not device_wide and not primary_switch:
            self._attr_name = f"{description.name} Switch {switch_id}"

    @property
    def native_value(self):
        return self.entity_description.value_fn(
            self.coordinator.data,
            self._switch_id,
        )
