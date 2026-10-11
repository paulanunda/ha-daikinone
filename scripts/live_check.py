"""Read-only check of the integration's API client against the real Daikin service.

It fetches a token, lists the thermostats, reads each one, and prints the
parsed state next to a comparison with the documented response shapes. It only
ever sends ``POST /v1/token`` and ``GET`` requests; nothing on a thermostat is
changed.

Credentials come from the environment (``DAIKIN_EMAIL``, ``DAIKIN_API_KEY``,
``DAIKIN_INTEGRATOR_TOKEN``), which mise loads from the gitignored
``mise.local.toml``. They are never printed. Identifiers are masked.

Run it with ``mise run live-check``. Daikin forbids polling more often than
once every three minutes, so the script refuses to run again sooner.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "custom_components" / "daikinone" / "api"
STAMP = ROOT / ".live-check-last-run"

ENV_EMAIL = "DAIKIN_EMAIL"
ENV_API_KEY = "DAIKIN_API_KEY"
ENV_INTEGRATOR_TOKEN = "DAIKIN_INTEGRATOR_TOKEN"  # noqa: S105 - a variable name

# Documented response field -> attribute of the parsed state.
ATTRIBUTE_FOR = {
    "equipmentStatus": "equipment_status",
    "mode": "mode",
    "modeLimit": "mode_limit",
    "modeEmHeatAvailable": "emergency_heat_available",
    "fan": "fan",
    "fanCirculate": "fan_circulate",
    "fanCirculateSpeed": "fan_circulate_speed",
    "heatSetpoint": "heat_setpoint",
    "coolSetpoint": "cool_setpoint",
    "setpointDelta": "setpoint_delta",
    "setpointMinimum": "setpoint_minimum",
    "setpointMaximum": "setpoint_maximum",
    "tempIndoor": "temp_indoor",
    "humIndoor": "hum_indoor",
    "tempOutdoor": "temp_outdoor",
    "humOutdoor": "hum_outdoor",
    "scheduleEnabled": "schedule_enabled",
    "geofencingEnabled": "geofencing_enabled",
}
TEMPERATURES = {
    "heat_setpoint",
    "cool_setpoint",
    "setpoint_minimum",
    "setpoint_maximum",
    "temp_indoor",
    "temp_outdoor",
}


def load_api() -> Any:
    """Import the client package by path, without importing Home Assistant."""
    spec = importlib.util.spec_from_file_location(
        "daikinone_api", API_DIR / "__init__.py", submodule_search_locations=[str(API_DIR)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot locate the API client package")
    module = importlib.util.module_from_spec(spec)
    sys.modules["daikinone_api"] = module
    spec.loader.exec_module(module)
    return module


api = load_api()


def shape(value: object) -> object:
    """Describe a JSON value by its keys and types only, never its contents."""
    if isinstance(value, dict):
        return {str(key): shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [shape(value[0]), f"... {len(value)} item(s)"] if value else []
    return type(value).__name__


def seconds_until_allowed() -> float:
    """Return how long to wait before the three minute limit allows another run."""
    try:
        last = float(STAMP.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return 0.0
    return max(0.0, last + float(api.MIN_POLL_INTERVAL_SECONDS) - time.time())


def describe(attribute: str, value: object) -> str:
    """Format one parsed value for printing."""
    if value is None:
        return "unknown"
    if attribute in TEMPERATURES and isinstance(value, float):
        return f"{value:.1f} C / {value * 9 / 5 + 32:.1f} F"
    if attribute == "setpoint_delta" and isinstance(value, float):
        return f"{value:.1f} C / {value * 9 / 5:.1f} F"
    name = getattr(value, "name", None)
    return str(name).lower() if name is not None else str(value)


def print_state(state: Any) -> None:
    """Print the parsed state of one thermostat."""
    for field, attribute in ATTRIBUTE_FOR.items():
        print(f"    {field:<22} {describe(attribute, getattr(state, attribute))}")


def print_differences(state: Any) -> int:
    """Compare one raw response with the documented fields. Return the number of differences."""
    raw: dict[str, object] = dict(state.raw)
    documented = set(ATTRIBUTE_FOR)
    differences = 0

    for name in sorted(documented - set(raw)):
        differences += 1
        print(f"    MISSING documented field: {name}")
    for name in sorted(set(raw) - documented):
        differences += 1
        print(f"    EXTRA undocumented field: {name} ({shape(raw[name])})")
    for name, attribute in ATTRIBUTE_FOR.items():
        if name in raw and getattr(state, attribute) is None:
            differences += 1
            value = raw[name]
            # Plain numbers and booleans are settings or readings, safe to show.
            shown = value if isinstance(value, (bool, int, float, type(None))) else shape(value)
            print(f"    UNUSABLE value for {name}: {shown!r} ({type(value).__name__})")

    print("    raw types: " + ", ".join(f"{k}={type(v).__name__}" for k, v in sorted(raw.items())))
    return differences


async def run() -> int:  # noqa: PLR0915 - a linear script
    """Run the check and return a process exit code."""
    missing = [n for n in (ENV_EMAIL, ENV_API_KEY, ENV_INTEGRATOR_TOKEN) if not os.environ.get(n)]
    if missing:
        print(f"Missing environment variable(s): {', '.join(missing)}")
        print("Add them to the [env] section of mise.local.toml and run: mise run live-check")
        return 2

    wait = seconds_until_allowed()
    if wait > 0:
        print(f"Daikin allows one poll every 3 minutes. Try again in {wait:.0f} seconds.")
        return 3
    STAMP.write_text(str(time.time()), encoding="utf-8")

    email = os.environ[ENV_EMAIL]
    recorded: list[tuple[str, int, object]] = []

    class RecordingClient(api.DaikinOneClient):  # type: ignore[misc, name-defined]
        """The real client, additionally noting each response for the shape report."""

        async def _send(self, method: str, path: str, **kwargs: Any) -> Any:
            status, payload, retry_after = await super()._send(method, path, **kwargs)
            recorded.append((kwargs["label"], status, payload))
            return status, payload, retry_after

    print(f"Host:    {api.API_HOST}")
    print(f"Account: {api.mask_email(email)}")
    differences = 0

    async with aiohttp.ClientSession() as session:
        client = RecordingClient(
            session,
            email=email,
            api_key=os.environ[ENV_API_KEY],
            integrator_token=os.environ[ENV_INTEGRATOR_TOKEN],
            ssl_context=api.create_ssl_context(),
        )
        try:
            await client.authenticate()
            _, status, token_payload = recorded[-1]
            print(f"\nPOST /v1/token -> HTTP {status}")
            if isinstance(token_payload, dict):
                print(f"  keys: {sorted(token_payload)}")
                print(f"  tokenType: {token_payload.get('tokenType')!r}")
                expires = token_payload.get("accessTokenExpiresIn")
                print(f"  accessTokenExpiresIn: {expires!r} ({type(expires).__name__})")
                expected = {"accessToken", "accessTokenExpiresIn", "tokenType"}
                if set(token_payload) != expected:
                    differences += 1
                    print(f"  DIFFERENT keys than documented: {sorted(expected)}")

            devices = await client.list_devices()
            _, status, list_payload = recorded[-1]
            print(f"\nGET /v1/devices -> HTTP {status}")
            print(f"  shape: {shape(list_payload)}")
            print(f"  thermostats found: {len(devices)}")

            for number, device in enumerate(devices, start=1):
                print(f"\nThermostat {number}: id {api.mask_id(device.id)}")
                print(f"    model {device.model!r}, firmware {device.firmware_version!r}")
                print(
                    f"    has name: {device.name is not None}, "
                    f"has location: {device.location_name is not None}"
                )
                try:
                    state = await client.get_thermostat(device.id)
                except api.DeviceOfflineError as err:
                    _, status, payload = recorded[-1]
                    print(f"  GET /v1/devices/<id> -> HTTP {status}: reported OFFLINE ({err})")
                    print(f"    shape: {shape(payload)}")
                    continue
                except api.DaikinOneError as err:
                    differences += 1
                    _, status, payload = recorded[-1]
                    print(f"  GET /v1/devices/<id> -> HTTP {status}: {type(err).__name__}: {err}")
                    print(f"    shape: {shape(payload)}")
                    continue
                _, status, _ = recorded[-1]
                print(f"  GET /v1/devices/<id> -> HTTP {status}")
                print_state(state)
                differences += print_differences(state)
        except api.DaikinOneError as err:
            label, status, payload = recorded[-1] if recorded else ("(nothing sent)", 0, None)
            print(f"\nFAILED at {label}: {type(err).__name__}: {err}")
            print(f"  HTTP {status}, body shape: {shape(payload)}")
            return 1

    sent = [label for label, _, _ in recorded]
    print(f"\nRequests sent: {len(sent)} ({', '.join(sent)})")
    print(
        "Result: responses match the documented shapes."
        if differences == 0
        else f"Result: {differences} difference(s) from the documented shapes, listed above."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
