"""Exceptions raised by the Daikin One Open API client.

Messages never contain credentials, tokens, response bodies, or the account
email. They carry only the HTTP status and a classification.
"""

from __future__ import annotations


class DaikinOneError(Exception):
    """Base class for every error the client raises."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AuthenticationError(DaikinOneError):
    """Daikin rejected the email, integrator token, API key, or access token."""


class RateLimitError(DaikinOneError):
    """Daikin answered HTTP 429, or the client is still backing off from one.

    ``retry_after`` is the number of seconds to wait before the next request.
    """

    def __init__(self, message: str, *, retry_after: float, status: int | None = 429) -> None:
        super().__init__(message, status=status)
        self.retry_after = retry_after


class DeviceOfflineError(DaikinOneError):
    """Daikin reported ``DeviceOfflineException`` for the thermostat."""


class BadRequestError(DaikinOneError):
    """Daikin rejected the request itself (HTTP 4xx other than auth and rate limit)."""


class ServerError(DaikinOneError):
    """Daikin answered HTTP 5xx or a status the documentation does not list."""


class ConnectionFailedError(DaikinOneError):
    """The request did not complete: timeout, DNS, TLS, or connection failure."""


class MalformedResponseError(DaikinOneError):
    """Daikin answered successfully but the body does not have the documented shape."""
