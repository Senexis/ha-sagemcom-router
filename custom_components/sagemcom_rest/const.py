"""Constants for the Sagemcom Router integration."""

from datetime import timedelta

DOMAIN = "sagemcom_rest"

CONF_VERIFY_SSL = "verify_ssl"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_HOST = "192.168.1.254"
DEFAULT_USERNAME = "beheer"
DEFAULT_SCAN_INTERVAL = 30  # seconds
MIN_SCAN_INTERVAL = 10
SLOW_INTERVAL = timedelta(minutes=5)

# Radio identifiers used by /api/v2/wireless/<id>
BANDS: dict[str, str] = {"2.4": "24", "5": "5", "6": "6"}
GUEST_BANDS: dict[str, str] = {"2.4": "guest24", "5": "guest5", "6": "guest6"}

ROUTER_SUBENTRY_TYPE = "router"
CLIENTS_SUBENTRY_TYPE = "clients"
