"""Load the transport without starting Home Assistant or importing its entry point."""

import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def rpc(monkeypatch):
    root = Path(__file__).resolve().parents[1] / "custom_components/shelly_ble_rpc"
    package = types.ModuleType("shelly_rpc_test")
    package.__path__ = [str(root)]
    monkeypatch.setitem(sys.modules, "shelly_rpc_test", package)
    for name in (
        "homeassistant",
        "homeassistant.components",
        "homeassistant.components.bluetooth",
        "homeassistant.core",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    core = sys.modules["homeassistant.core"]
    core.HomeAssistant = object
    bluetooth = sys.modules["homeassistant.components.bluetooth"]
    bluetooth.async_ble_device_from_address = lambda *args, **kwargs: SimpleNamespace(
        name="Shelly"
    )
    for name in ("const", "proxy_recovery", "bluez_pairing", "rpc"):
        monkeypatch.delitem(sys.modules, f"shelly_rpc_test.{name}", raising=False)
    module = importlib.import_module("shelly_rpc_test.rpc")
    monkeypatch.setattr(module, "TX_CONTROL_DELAY_SECONDS", 0)
    return module
