DOMAIN = "shelly_ble_rpc"
PLATFORMS = ["switch", "sensor", "binary_sensor", "button"]

CONF_ADDRESS = "address"
CONF_NAME = "name"
CONF_SWITCH_ID = "switch_id"
CONF_PAIR = "pair"

DEFAULT_NAME = "Shelly BLE Relay"
DEFAULT_SWITCH_ID = 0
DEFAULT_SCAN_INTERVAL_SECONDS = 30
CONNECTION_LEASE_SECONDS = 5.0

SHELLY_GATT_SERVICE_UUID = "5f6d4f53-5f52-5043-5f53-56435f49445f"
RPC_CHAR_DATA_UUID = "5f6d4f53-5f52-5043-5f64-6174615f5f5f"
RPC_CHAR_TX_CTL_UUID = "5f6d4f53-5f52-5043-5f74-785f63746c5f"
RPC_CHAR_RX_CTL_UUID = "5f6d4f53-5f52-5043-5f72-785f63746c5f"

RPC_SOURCE = "ha_shelly_ble"
BLE_CHUNK_SIZE = 20
# RPC_TIMEOUT must absorb one write per data chunk plus one read per response
# chunk; each of those is a round-trip through Home Assistant's Bluetooth
# stack, which includes ESPHome Bluetooth proxy hops.
RPC_TIMEOUT = 20

# Delay after the TX control write so the Shelly has time to prepare the
# incoming length before the data chunks arrive. Proxied GATT operations
# (ESPHome Bluetooth proxy) add extra round-trips, so this is deliberately
# not the minimum value that could work on a local adapter.
TX_CONTROL_DELAY_SECONDS = 0.2

# Setup retries request pairing through the active Bluetooth backend and
# validate a read-only RPC while the Shelly's pairing window remains open.
CONNECTION_PAIRING_ATTEMPTS = 4
CONNECTION_PAIRING_RETRY_SECONDS = 5.0

# Security setup and cache recovery run outside the per-RPC deadline.
PAIRING_TIMEOUT = 30
GATT_CACHE_TIMEOUT = 10

CONF_PAIRING_REQUIRED = "pairing_required"
