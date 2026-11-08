from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ADDRESS,
    PLATFORMS,
    CONF_PAIRING_REQUIRED,
)
from .coordinator import ShellyBleRpcCoordinator
from .bluez_pairing import (
    ShellyBlueZUnavailable,
    async_get_pairing_state,
)
from .rpc import (
    ShellyBleRpcClient,
    ShellyBleRpcPairingUnavailable,
    ShellyBleRpcUnavailable,
    async_bond_via_connection,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class ShellyBleRpcRuntimeData:
    """Runtime objects shared by Shelly BLE RPC platforms."""

    client: ShellyBleRpcClient
    coordinator: ShellyBleRpcCoordinator
    device_info: dict
    unpaired: bool = False


type ShellyBleRpcConfigEntry = ConfigEntry[ShellyBleRpcRuntimeData]


async def async_migrate_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Migrate older Shelly BLE RPC config entries to the current schema."""
    _LOGGER.info(
        "Migrating Shelly BLE RPC config entry '%s' from version %s.%s",
        entry.title,
        entry.version,
        entry.minor_version,
    )

    if entry.version > 2:
        _LOGGER.error(
            "Cannot migrate Shelly BLE RPC config entry '%s' from future "
            "version %s",
            entry.title,
            entry.version,
        )
        return False

    new_data = dict(entry.data)

    if entry.version == 1:
        # v1 entries stored two setup-time implementation details:
        #
        #   switch_id
        #   pair
        #
        # v2 discovers switch components automatically from Shelly.GetStatus
        # and always manages pairing automatically, so these fields are no
        # longer required.
        #
        # They are safe to remove because neither value represents user data
        # needed to identify the device.
        new_data.pop("switch_id", None)
        new_data.pop("pair", None)

        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            version=2,
            minor_version=1,
        )

    elif entry.version == 2 and entry.minor_version < 1:
        # Normalize any early v2 entries which may still contain the legacy
        # fields from testing builds.
        new_data.pop("switch_id", None)
        new_data.pop("pair", None)

        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            version=2,
            minor_version=1,
        )

    _LOGGER.info(
        "Shelly BLE RPC config entry '%s' migration complete: version %s.%s",
        entry.title,
        entry.version,
        entry.minor_version,
    )
    return True


def _is_gatt_security_state_error(err: BaseException) -> bool:
    """Return True only for the known post-unpair GATT 0x0E failure.

    This is intentionally narrow so ordinary Bluetooth failures never trigger
    pairing recovery.
    """
    seen: set[int] = set()
    current: BaseException | None = err

    while current is not None and id(current) not in seen:
        seen.add(id(current))

        message = str(current).lower()
        if (
            "unlikely error" in message
            or "gatt protocol error: unlikely error" in message
            or "bleakgattprotocolerrorcode.unlikely_error" in message
        ):
            return True

        next_err = current.__cause__ or current.__context__
        current = next_err if isinstance(next_err, BaseException) else None

    return False


async def _async_pair_and_validate(
    client: ShellyBleRpcClient,
    coordinator: ShellyBleRpcCoordinator,
    address: str,
    *,
    entry_title: str,
    bluez_available: bool,
    bluez_device_present: bool,
) -> None:
    """Complete Bluetooth pairing using the path that can reach the Shelly.

    Connection-validated bonding is attempted first: it works through an
    ESPHome Bluetooth proxy, which bonds with the Shelly during its own
    connect attempt while the Shelly accepts Bluetooth pairing, and it can
    also complete whenever the local connection path already has a usable
    bond. Explicit BlueZ agent pairing is only attempted afterwards, when a
    local adapter has actually observed the Shelly, and is optional.
    """
    last_err: BaseException | None = None

    try:
        await async_bond_via_connection(
            client,
            address,
            entry_title=entry_title,
        )
        await coordinator.async_config_entry_first_refresh()
        return
    except Exception as err:
        last_err = err
        _LOGGER.info(
            "Bluetooth bonding through the connection path for '%s' did not "
            "complete: %s: %s",
            entry_title,
            type(err).__name__,
            err,
        )

    if last_err is None:
        # Unreachable: every failure path in the block above binds an
        # exception before control flow continues here.
        return

    if not bluez_available:
        # Without BlueZ the connection path is the only available path.
        raise last_err

    if not bluez_device_present:
        _LOGGER.info(
            "BlueZ has not observed '%s' (%s); explicit BlueZ pairing is "
            "not possible for this device right now",
            entry_title,
            address,
        )
        raise last_err

    _LOGGER.info(
        "Falling back to explicit BlueZ pairing for '%s' (%s)",
        entry_title,
        address,
    )

    try:
        await client.pair_explicitly()
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        _LOGGER.info(
            "Explicit BlueZ fallback pairing for '%s' did not complete; "
            "leaving pairing_required set for a later Reload",
            entry_title,
        )
        raise
    else:
        _LOGGER.info(
            "Explicit BlueZ fallback pairing completed for '%s'; the "
            "pairing_required flag can be cleared",
            entry_title,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ShellyBleRpcConfigEntry,
) -> bool:
    """Set up Shelly BLE RPC from a config entry."""
    address = entry.data[CONF_ADDRESS]

    client = ShellyBleRpcClient(
        hass=hass,
        address=address,
        pair_on_connect=True,
    )

    coordinator = ShellyBleRpcCoordinator(
        hass=hass,
        client=client,
        address=address,
        name=entry.title,
    )

    # Determine BlueZ's actual pairing state before the first RPC.
    #
    # This is read-only. We do not delete or alter any bond here.
    #
    # When no system D-Bus/BlueZ stack is available (for example installations
    # that rely exclusively on ESPHome Bluetooth proxies), bonding is performed
    # by the proxy itself while the Shelly accepts Bluetooth pairing, so the
    # BlueZ state is treated as unknown rather than as a setup failure.
    try:
        device_present, bluez_paired, bluez_bonded = (
            await async_get_pairing_state(address)
        )
        bluez_available = True
    except ShellyBlueZUnavailable as err:
        bluez_available = False
        device_present = bluez_paired = bluez_bonded = False
        _LOGGER.info(
            "No local BlueZ adapter for '%s' (%s); bonding will be handled "
            "by the ESPHome Bluetooth proxy path: %s",
            entry.title,
            address,
            err,
        )

    _LOGGER.info(
        "BlueZ state for '%s' (%s): available=%s, present=%s, Paired=%s, "
        "Bonded=%s, pairing_required=%s",
        entry.title,
        address,
        bluez_available,
        device_present,
        bluez_paired,
        bluez_bonded,
        entry.data.get(CONF_PAIRING_REQUIRED, False),
    )

    explicit_pair_required = bool(
        entry.data.get(CONF_PAIRING_REQUIRED, False)
        or (
            bluez_available
            and device_present
            and not bluez_paired
            and not bluez_bonded
        )
    )

    if explicit_pair_required:
        # A configured entry with an actual BlueZ Device1 object that is
        # explicitly unpaired/unbonded genuinely requires pairing. This also
        # covers legacy entries unpaired before pairing_required was persisted.
        if not entry.data.get(CONF_PAIRING_REQUIRED, False):
            new_data = dict(entry.data)
            new_data[CONF_PAIRING_REQUIRED] = True
            hass.config_entries.async_update_entry(entry, data=new_data)
            _LOGGER.info(
                "Configured Shelly '%s' is present in BlueZ but unpaired/"
                "unbonded; marked pairing_required",
                entry.title,
            )

        _LOGGER.info(
            "Config entry '%s' requires Bluetooth pairing; completing the "
            "bond using the Bluetooth path that can reach the Shelly",
            entry.title,
        )

        try:
            await _async_pair_and_validate(
                client,
                coordinator,
                address,
                entry_title=entry.title,
                bluez_available=bluez_available,
                bluez_device_present=device_present,
            )
        except ShellyBleRpcPairingUnavailable:
            new_data = dict(entry.data)
            new_data[CONF_PAIRING_REQUIRED] = True
            hass.config_entries.async_update_entry(entry, data=new_data)
            raise
        except Exception:
            _LOGGER.info(
                "Bluetooth bonding for '%s' did not complete; leaving "
                "pairing_required set for a later Reload",
                entry.title,
            )
            raise
        else:
            new_data = dict(entry.data)
            new_data.pop(CONF_PAIRING_REQUIRED, None)
            hass.config_entries.async_update_entry(entry, data=new_data)
            _LOGGER.info(
                "Bluetooth bonding completed for '%s'; cleared "
                "pairing_required flag",
                entry.title,
            )
    else:
        # If BlueZ has no Device1 object yet, do not infer that the bond is
        # stale. Home Assistant discovery may simply not have recreated the
        # object yet, and setups that only use ESPHome Bluetooth proxies have
        # no Device1 objects at all. Likewise, ordinary timeouts remain
        # non-destructive.
        #
        # Keep the narrowly targeted GATT 0x0E legacy fallback as an additional
        # signal for asymmetric security state when BlueZ still reports a bond.
        try:
            await coordinator.async_config_entry_first_refresh()
        except Exception as err:
            if not _is_gatt_security_state_error(err):
                raise

            new_data = dict(entry.data)
            new_data[CONF_PAIRING_REQUIRED] = True
            hass.config_entries.async_update_entry(entry, data=new_data)

            if bluez_available and device_present:
                _LOGGER.warning(
                    "Initial RPC for '%s' reached the Shelly but failed with "
                    "GATT 0x0E/Unlikely Error; attempting one explicit "
                    "Bluetooth pairing operation for asymmetric bond recovery",
                    entry.title,
                )

                try:
                    await client.pair_explicitly()
                    await coordinator.async_config_entry_first_refresh()
                except Exception:
                    _LOGGER.info(
                        "Explicit pairing recovery for '%s' did not complete; "
                        "pairing_required remains set for a later Reload",
                        entry.title,
                    )
                    raise
                else:
                    new_data = dict(entry.data)
                    new_data.pop(CONF_PAIRING_REQUIRED, None)
                    hass.config_entries.async_update_entry(entry, data=new_data)
                    _LOGGER.info(
                        "Explicit Bluetooth pairing recovery completed for "
                        "'%s'; cleared pairing_required flag",
                        entry.title,
                    )
            else:
                _LOGGER.warning(
                    "Initial RPC for '%s' reached the Shelly but failed with "
                    "GATT 0x0E/Unlikely Error; %s, so the connection path is "
                    "given retries to bond while the Shelly is in pairing "
                    "mode",
                    entry.title,
                    "no local BlueZ adapter is available"
                    if not bluez_available
                    else "BlueZ has not observed the Shelly",
                )

                try:
                    await async_bond_via_connection(
                        client,
                        address,
                        entry_title=entry.title,
                    )
                    await coordinator.async_config_entry_first_refresh()
                except Exception:
                    _LOGGER.info(
                        "Bluetooth bonding recovery for '%s' did not "
                        "complete; pairing_required remains set for a later "
                        "Reload",
                        entry.title,
                    )
                    raise
                else:
                    new_data = dict(entry.data)
                    new_data.pop(CONF_PAIRING_REQUIRED, None)
                    hass.config_entries.async_update_entry(entry, data=new_data)
                    _LOGGER.info(
                        "Bluetooth bonding recovery completed for '%s'; "
                        "cleared pairing_required flag",
                        entry.title,
                    )

    # Device info is mostly static. Read it once for proper device registry
    # model/firmware metadata. A failure here should not throw away a working
    # status connection.
    try:
        device_info = await client.get_device_info()
    except Exception as err:
        _LOGGER.warning("Unable to read Shelly device information: %s", err)
        device_info = {}

    entry.runtime_data = ShellyBleRpcRuntimeData(
        client=client,
        coordinator=coordinator,
        device_info=device_info,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ShellyBleRpcConfigEntry,
) -> bool:
    """Unload a config entry and close any active BLE connection lease."""
    unloaded = await hass.config_entries.async_unload_platforms(
        entry,
        PLATFORMS,
    )

    if unloaded:
        await entry.runtime_data.client.async_shutdown()

    return unloaded
