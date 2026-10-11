"""Standalone client for the Daikin One Open API (no Home Assistant imports)."""

from __future__ import annotations

from .client import (
    API_HOST,
    BASE_URL,
    MAX_OPEN_REQUESTS,
    MIN_POLL_INTERVAL_SECONDS,
    WRITE_SETTLE_SECONDS,
    DaikinOneClient,
    create_ssl_context,
)
from .errors import (
    AuthenticationError,
    BadRequestError,
    ConnectionFailedError,
    DaikinOneError,
    DeviceOfflineError,
    MalformedResponseError,
    RateLimitError,
    ServerError,
)
from .models import (
    DeviceInfo,
    EquipmentStatus,
    FanCirculate,
    FanCirculateSpeed,
    FanState,
    Mode,
    ModeLimit,
    ThermostatState,
)
from .redact import mask_email, mask_id

__all__ = [
    "API_HOST",
    "BASE_URL",
    "MAX_OPEN_REQUESTS",
    "MIN_POLL_INTERVAL_SECONDS",
    "WRITE_SETTLE_SECONDS",
    "AuthenticationError",
    "BadRequestError",
    "ConnectionFailedError",
    "DaikinOneClient",
    "DaikinOneError",
    "DeviceInfo",
    "DeviceOfflineError",
    "EquipmentStatus",
    "FanCirculate",
    "FanCirculateSpeed",
    "FanState",
    "MalformedResponseError",
    "Mode",
    "ModeLimit",
    "RateLimitError",
    "ServerError",
    "ThermostatState",
    "create_ssl_context",
    "mask_email",
    "mask_id",
]
