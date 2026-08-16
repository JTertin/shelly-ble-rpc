from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ShellyBleRpcConfigEntry
from .entity import ShellyBleRpcEntity, discovered_switch_ids
from .rpc import ShellyBleRpcError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ShellyBleRpcConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up all Shelly switch components discovered over BLE RPC."""
    switch_ids = discovered_switch_ids(entry.runtime_data.coordinator.data)

    async_add_entities(
        [
            ShellyBleRpcSwitch(
                entry,
                switch_id,
                primary=(index == 0),
            )
            for index, switch_id in enumerate(switch_ids)
        ]
    )


class ShellyBleRpcSwitch(ShellyBleRpcEntity, SwitchEntity):
    """Shelly switch component controlled over BLE RPC."""

    def __init__(
        self,
        entry: ShellyBleRpcConfigEntry,
        switch_id: int,
        primary: bool,
    ) -> None:
        self._switch_id = switch_id
        self._primary = primary

        super().__init__(
            entry,
            entry.runtime_data.coordinator,
            f"switch_{switch_id}",
        )

        # Preserve the existing single-switch presentation: the first/only
        # relay uses the device name. Additional relays are named explicitly.
        self._attr_name = None if primary else f"Switch {switch_id}"

    @property
    def is_on(self) -> bool | None:
        """Return relay state."""
        status = self.coordinator.data.get(f"switch:{self._switch_id}", {})
        output = status.get("output")
        return bool(output) if output is not None else None

    async def async_turn_on(self, **kwargs) -> None:
        """Turn relay on."""
        try:
            await self._entry.runtime_data.client.set_switch(
                self._switch_id,
                True,
            )
        except ShellyBleRpcError as err:
            self.coordinator.async_set_update_error(err)
            raise
        await self.coordinator.async_refresh_immediate()

    async def async_turn_off(self, **kwargs) -> None:
        """Turn relay off."""
        try:
            await self._entry.runtime_data.client.set_switch(
                self._switch_id,
                False,
            )
        except ShellyBleRpcError as err:
            self.coordinator.async_set_update_error(err)
            raise
        await self.coordinator.async_refresh_immediate()
