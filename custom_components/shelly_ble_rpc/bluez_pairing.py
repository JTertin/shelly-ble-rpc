import asyncio
import logging
from typing import Any

from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_method

_LOGGER = logging.getLogger(__name__)

BLUEZ_SERVICE = "org.bluez"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
DEVICE_IFACE = "org.bluez.Device1"

AGENT_PATH = "/com/homeassistant/shelly_ble_rpc/agent"
AGENT_CAPABILITY = "NoInputNoOutput"


class ShellyBlueZUnavailable(Exception):
    """Raised when the system D-Bus or the BlueZ service is not accessible.

    This is normal for Home Assistant installations that rely exclusively on
    ESPHome Bluetooth proxies, on containers without a system D-Bus socket,
    and on adapters which are temporarily absent.
    """


class ShellyBlePairingError(Exception):
    """Raised when explicit BlueZ pairing fails."""


async def async_bluez_available() -> bool:
    """Return True only when BlueZ is reachable over the system D-Bus."""
    try:
        bus = await _async_open_bluez_bus()
    except ShellyBlueZUnavailable:
        return False

    bus.disconnect()
    return True


async def _async_open_bluez_bus() -> MessageBus:
    """Open the system D-Bus and verify that BlueZ is present on it.

    Raises ShellyBlueZUnavailable when the bus or the BlueZ service cannot be
    used. Callers only need to handle one exception type instead of raw
    dbus_fast errors.
    """
    bus: MessageBus | None = None
    try:
        bus = MessageBus(bus_type=BusType.SYSTEM)
        await bus.connect()
        await bus.introspect(BLUEZ_SERVICE, "/")
    except Exception as err:
        if bus is not None:
            try:
                bus.disconnect()
            except Exception:
                pass
        raise ShellyBlueZUnavailable(
            "BlueZ is not available over the system D-Bus; the Shelly must be "
            "reached through Home Assistant's Bluetooth integration, such as "
            "an ESPHome Bluetooth proxy: "
            f"{type(err).__name__}: {err}"
        ) from err

    return bus


class ShellyNoInputNoOutputAgent(ServiceInterface):
    """Minimal BlueZ Agent1 implementation for BLE Just Works pairing."""

    def __init__(self) -> None:
        super().__init__("org.bluez.Agent1")

    @dbus_method()
    def Release(self) -> None:
        """BlueZ has released the agent."""
        _LOGGER.debug("BlueZ released Shelly BLE pairing agent")

    @dbus_method()
    def RequestPinCode(self, device: "o") -> "s":
        """PIN code is not expected for LE Just Works."""
        _LOGGER.debug("BlueZ requested PIN for %s; returning 000000", device)
        return "000000"

    @dbus_method()
    def DisplayPinCode(self, device: "o", pincode: "s") -> None:
        """Log PIN-code display requests."""
        _LOGGER.debug("BlueZ DisplayPinCode for %s: %s", device, pincode)

    @dbus_method()
    def RequestPasskey(self, device: "o") -> "u":
        """Passkey is not expected for NoInputNoOutput."""
        _LOGGER.debug("BlueZ requested passkey for %s; returning 0", device)
        return 0

    @dbus_method()
    def DisplayPasskey(
        self,
        device: "o",
        passkey: "u",
        entered: "q",
    ) -> None:
        """Log passkey-display requests."""
        _LOGGER.debug(
            "BlueZ DisplayPasskey for %s: %s (%s entered)",
            device,
            passkey,
            entered,
        )

    @dbus_method()
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:
        """Automatically accept Just Works confirmation."""
        _LOGGER.info(
            "BlueZ requested pairing confirmation for %s (passkey=%06d); accepting",
            device,
            passkey,
        )

    @dbus_method()
    def RequestAuthorization(self, device: "o") -> None:
        """Automatically authorize the pairing."""
        _LOGGER.info(
            "BlueZ requested pairing authorization for %s; accepting",
            device,
        )

    @dbus_method()
    def AuthorizeService(self, device: "o", uuid: "s") -> None:
        """Authorize services for the bonded Shelly."""
        _LOGGER.debug(
            "BlueZ requested service authorization for %s UUID %s; accepting",
            device,
            uuid,
        )

    @dbus_method()
    def Cancel(self) -> None:
        """BlueZ canceled the current agent request."""
        _LOGGER.warning("BlueZ canceled the Shelly BLE pairing request")


def _variant_value(value: Any) -> Any:
    """Return raw value from a dbus-fast Variant when needed."""
    return getattr(value, "value", value)


async def async_get_pairing_state(
    address: str,
) -> tuple[bool, bool, bool]:
    """Return (device_present, paired, bonded) from BlueZ Device1 state.

    This function is read-only and never changes pairing/bond state.
    """
    normalized = address.upper()
    bus = await _async_open_bluez_bus()

    try:
        root_intro = await bus.introspect(BLUEZ_SERVICE, "/")
        root_obj = bus.get_proxy_object(BLUEZ_SERVICE, "/", root_intro)
        object_manager = root_obj.get_interface(OBJECT_MANAGER_IFACE)
        managed = await object_manager.call_get_managed_objects()

        for _path, interfaces in managed.items():
            props = interfaces.get(DEVICE_IFACE)
            if props is None:
                continue

            found_address = _variant_value(props.get("Address"))
            if found_address and str(found_address).upper() == normalized:
                paired = bool(_variant_value(props.get("Paired", False)))
                bonded = bool(_variant_value(props.get("Bonded", False)))
                return True, paired, bonded

        return False, False, False
    finally:
        bus.disconnect()


async def async_pair_device(address: str, timeout: float = 30.0) -> bool:
    """Explicitly pair/bond a BLE device using a NoInputNoOutput BlueZ agent.

    Returns True when the device is confirmed paired/bonded.
    """
    normalized = address.upper()
    bus = await _async_open_bluez_bus()

    agent = ShellyNoInputNoOutputAgent()
    agent_registered = False

    try:
        bus.export(AGENT_PATH, agent)

        # Find the BlueZ object path for this advertised BLE address.
        root_intro = await bus.introspect(BLUEZ_SERVICE, "/")
        root_obj = bus.get_proxy_object(BLUEZ_SERVICE, "/", root_intro)
        object_manager = root_obj.get_interface(OBJECT_MANAGER_IFACE)
        managed = await object_manager.call_get_managed_objects()

        device_path = None
        paired = False
        bonded = False

        for path, interfaces in managed.items():
            props = interfaces.get(DEVICE_IFACE)
            if props is None:
                continue
            found_address = _variant_value(props.get("Address"))
            if found_address and str(found_address).upper() == normalized:
                device_path = path
                paired = bool(_variant_value(props.get("Paired", False)))
                bonded = bool(_variant_value(props.get("Bonded", False)))
                break

        if device_path is None:
            raise ShellyBlePairingError(
                f"BlueZ does not currently have a Device1 object for {normalized}. "
                "Wait until Home Assistant has discovered the Shelly and try again."
            )

        _LOGGER.info(
            "BlueZ pairing target %s -> %s (Paired=%s, Bonded=%s)",
            normalized,
            device_path,
            paired,
            bonded,
        )

        if paired or bonded:
            _LOGGER.info(
                "%s is already paired/bonded in BlueZ; skipping Device1.Pair()",
                normalized,
            )
            return True

        bluez_intro = await bus.introspect(BLUEZ_SERVICE, "/org/bluez")
        bluez_obj = bus.get_proxy_object(
            BLUEZ_SERVICE,
            "/org/bluez",
            bluez_intro,
        )
        agent_manager = bluez_obj.get_interface(AGENT_MANAGER_IFACE)

        _LOGGER.debug(
            "Registering BlueZ agent %s with capability %s",
            AGENT_PATH,
            AGENT_CAPABILITY,
        )
        await agent_manager.call_register_agent(
            AGENT_PATH,
            AGENT_CAPABILITY,
        )
        agent_registered = True

        await agent_manager.call_request_default_agent(AGENT_PATH)
        _LOGGER.info(
            "Registered temporary BlueZ NoInputNoOutput agent for Shelly pairing"
        )

        device_intro = await bus.introspect(BLUEZ_SERVICE, device_path)
        device_obj = bus.get_proxy_object(
            BLUEZ_SERVICE,
            device_path,
            device_intro,
        )
        device_iface = device_obj.get_interface(DEVICE_IFACE)

        _LOGGER.info("Calling BlueZ Device1.Pair() for %s", normalized)
        await asyncio.wait_for(
            device_iface.call_pair(),
            timeout=timeout,
        )
        _LOGGER.info("BlueZ Device1.Pair() returned successfully for %s", normalized)

        # Re-read Device1 properties to verify BlueZ persisted the bond.
        props_iface = device_obj.get_interface("org.freedesktop.DBus.Properties")
        paired_v = await props_iface.call_get(DEVICE_IFACE, "Paired")
        bonded_v = await props_iface.call_get(DEVICE_IFACE, "Bonded")
        paired = bool(_variant_value(paired_v))
        bonded = bool(_variant_value(bonded_v))

        _LOGGER.info(
            "Pairing verification for %s: Paired=%s, Bonded=%s",
            normalized,
            paired,
            bonded,
        )

        if not (paired or bonded):
            raise ShellyBlePairingError(
                f"BlueZ Pair() completed for {normalized}, but Paired/Bonded "
                "did not become true"
            )

        return True

    except asyncio.TimeoutError as err:
        raise ShellyBlePairingError(
            f"BlueZ Device1.Pair() timed out after {timeout:.0f}s for {normalized}"
        ) from err
    except ShellyBlePairingError:
        raise
    except Exception as err:
        dbus_name = getattr(err, "dbus_error", None)
        details = getattr(err, "dbus_error_details", None)
        suffix = ""
        if dbus_name:
            suffix += f" [{dbus_name}]"
        if details:
            suffix += f" {details}"
        raise ShellyBlePairingError(
            f"Explicit BlueZ pairing failed for {normalized}: "
            f"{type(err).__name__}: {err}{suffix}"
        ) from err
    finally:
        if agent_registered:
            try:
                bluez_intro = await bus.introspect(BLUEZ_SERVICE, "/org/bluez")
                bluez_obj = bus.get_proxy_object(
                    BLUEZ_SERVICE,
                    "/org/bluez",
                    bluez_intro,
                )
                agent_manager = bluez_obj.get_interface(AGENT_MANAGER_IFACE)
                await agent_manager.call_unregister_agent(AGENT_PATH)
                _LOGGER.debug("Unregistered temporary BlueZ Shelly pairing agent")
            except Exception as err:
                _LOGGER.debug(
                    "Unable to unregister temporary BlueZ pairing agent: %s",
                    err,
                )
        try:
            bus.unexport(AGENT_PATH)
        except Exception:
            pass
        bus.disconnect()
async def async_get_adapter_address_for_device(address: str) -> str:
    """Return the local BlueZ adapter address that owns a remote Device1."""
    normalized = address.upper()
    bus = await _async_open_bluez_bus()
    try:
        root_intro = await bus.introspect(BLUEZ_SERVICE, "/")
        root_obj = bus.get_proxy_object(BLUEZ_SERVICE, "/", root_intro)
        object_manager = root_obj.get_interface(OBJECT_MANAGER_IFACE)
        managed = await object_manager.call_get_managed_objects()

        device_path = None
        adapter_path = None
        for path, interfaces in managed.items():
            props = interfaces.get(DEVICE_IFACE)
            if props is None:
                continue
            found_address = _variant_value(props.get("Address"))
            if found_address and str(found_address).upper() == normalized:
                device_path = path
                adapter_path = _variant_value(props.get("Adapter"))
                break

        if device_path is None or not adapter_path:
            raise ShellyBlePairingError(
                f"BlueZ does not currently have a Device1 object for {normalized}"
            )

        adapter_props = managed.get(str(adapter_path), {}).get(
            "org.bluez.Adapter1"
        )
        if not adapter_props:
            raise ShellyBlePairingError(
                f"Unable to read BlueZ adapter properties for {adapter_path}"
            )

        adapter_address = _variant_value(adapter_props.get("Address"))
        if not adapter_address:
            raise ShellyBlePairingError(
                f"BlueZ adapter {adapter_path} has no Address property"
            )

        return str(adapter_address).upper()
    finally:
        bus.disconnect()


async def async_remove_bluez_device(address: str) -> None:
    """Remove a remote device and its cached bond from BlueZ."""
    normalized = address.upper()
    bus = await _async_open_bluez_bus()
    try:
        root_intro = await bus.introspect(BLUEZ_SERVICE, "/")
        root_obj = bus.get_proxy_object(BLUEZ_SERVICE, "/", root_intro)
        object_manager = root_obj.get_interface(OBJECT_MANAGER_IFACE)
        managed = await object_manager.call_get_managed_objects()

        device_path = None
        adapter_path = None
        for path, interfaces in managed.items():
            props = interfaces.get(DEVICE_IFACE)
            if props is None:
                continue
            found_address = _variant_value(props.get("Address"))
            if found_address and str(found_address).upper() == normalized:
                device_path = path
                adapter_path = _variant_value(props.get("Adapter"))
                break

        if device_path is None or not adapter_path:
            _LOGGER.info(
                "BlueZ device %s is already absent; nothing to remove",
                normalized,
            )
            return

        adapter_intro = await bus.introspect(BLUEZ_SERVICE, str(adapter_path))
        adapter_obj = bus.get_proxy_object(
            BLUEZ_SERVICE,
            str(adapter_path),
            adapter_intro,
        )
        adapter_iface = adapter_obj.get_interface("org.bluez.Adapter1")

        _LOGGER.info(
            "Removing Shelly %s from BlueZ adapter %s",
            normalized,
            adapter_path,
        )
        await adapter_iface.call_remove_device(device_path)
        _LOGGER.info("Removed Shelly %s from BlueZ", normalized)
    finally:
        bus.disconnect()
