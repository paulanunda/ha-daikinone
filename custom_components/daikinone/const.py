"""Constants for the Daikin One integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from .api import MIN_POLL_INTERVAL_SECONDS, WRITE_SETTLE_SECONDS

DOMAIN: Final = "daikinone"
MANUFACTURER: Final = "Daikin"

CONF_INTEGRATOR_TOKEN: Final = "integrator_token"  # noqa: S105 - a config key, not a secret

# Polling interval option, in minutes. Daikin forbids polling faster than once
# every three minutes, so that is both the default and the hard floor.
CONF_SCAN_INTERVAL_MINUTES: Final = "scan_interval_minutes"
MIN_SCAN_INTERVAL_MINUTES: Final = MIN_POLL_INTERVAL_SECONDS // 60
MAX_SCAN_INTERVAL_MINUTES: Final = 60
DEFAULT_SCAN_INTERVAL_MINUTES: Final = MIN_SCAN_INTERVAL_MINUTES

# How often the list of thermostats on the account is fetched again.
DEVICE_LIST_INTERVAL: Final = timedelta(hours=1)

# Daikin asks for at least 15 seconds before a change is read back.
VERIFY_DELAY: Final = timedelta(seconds=WRITE_SETTLE_SECONDS)
