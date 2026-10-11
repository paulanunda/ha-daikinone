"""An in-memory stand-in for the Daikin One Open API.

It implements the documented contract from docs/daikin-open-api/contract.json:
the endpoints, required headers, bearer tokens, and write semantics. Tests can
queue one-off responses or exceptions for any route to simulate failures.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

CONTRACT_PATH = Path(__file__).parent.parent / "docs" / "daikin-open-api" / "contract.json"
CONTRACT: dict[str, Any] = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

BASE_URL: str = CONTRACT["transport"]["base_url"]

EMAIL = "Owner.Name@Example.com"
API_KEY = "test-api-key-AAAA1111"
INTEGRATOR_TOKEN = "test-integrator-token-BBBB2222"
ACCESS_TOKEN_PREFIX = "test-access-token-CCCC"

MAIN_ID = "11111111-aaaa-4bbb-8ccc-000000000001"
UPSTAIRS_ID = "22222222-aaaa-4bbb-8ccc-000000000002"


def example_state(**overrides: object) -> dict[str, Any]:
    """Return the documented example thermostat response, with overrides applied."""
    state: dict[str, Any] = copy.deepcopy(CONTRACT["endpoints"]["get_device"]["response_example"])
    # The documented example uses equipmentStatus 0, which is not a documented
    # enum value. Default to idle so tests start from a fully valid state.
    state["equipmentStatus"] = 5
    state.update(overrides)
    return state


@dataclass
class Reply:
    """A canned HTTP response."""

    status: int = 200
    body: object = None
    headers: dict[str, str] = field(default_factory=dict)
    raw: bytes | None = None

    def payload(self) -> bytes:
        if self.raw is not None:
            return self.raw
        return b"" if self.body is None else json.dumps(self.body).encode()


@dataclass
class Call:
    """One request the fake received."""

    method: str
    path: str
    headers: dict[str, str]
    json: Any
    kwargs: dict[str, Any]


Outcome = Reply | BaseException | Callable[[Call], "Reply"]


class _Response:
    def __init__(self, reply: Reply) -> None:
        self.status = reply.status
        self.headers = reply.headers
        self._payload = reply.payload()

    async def read(self) -> bytes:
        return self._payload


class _RequestContext:
    def __init__(self, server: FakeDaikin, call: Call) -> None:
        self._server = server
        self._call = call

    async def __aenter__(self) -> _Response:
        server = self._server
        server.in_flight += 1
        server.max_in_flight = max(server.max_in_flight, server.in_flight)
        try:
            if server.hold is not None:
                await server.hold.wait()
            return _Response(server.handle(self._call))
        except BaseException:
            server.in_flight -= 1
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._server.in_flight -= 1


class FakeSession:
    """Just enough of aiohttp.ClientSession for the client under test."""

    def __init__(self, server: FakeDaikin) -> None:
        self._server = server

    def request(self, method: str, url: str, **kwargs: Any) -> _RequestContext:
        assert url.startswith(BASE_URL + "/"), f"request to unexpected URL: {url}"
        call = Call(
            method=method,
            path=url[len(BASE_URL) :],
            headers=dict(kwargs.get("headers") or {}),
            json=copy.deepcopy(kwargs.get("json")),
            kwargs=kwargs,
        )
        self._server.calls.append(call)
        return _RequestContext(self._server, call)


class FakeDaikin:
    """Stateful fake of the Daikin cloud for one account with two thermostats."""

    def __init__(self) -> None:
        self.email = EMAIL
        self.api_key = API_KEY
        self.integrator_token = INTEGRATOR_TOKEN
        self.expires_in: object = 3600
        self.locations: list[dict[str, Any]] = [
            {
                "locationName": "Home",
                "devices": [
                    {
                        "id": MAIN_ID,
                        "name": "Main Floor",
                        "model": "ONETOUCH",
                        "firmwareVersion": "3.2.19",
                    },
                    {
                        "id": UPSTAIRS_ID,
                        "name": "Upstairs",
                        "model": "ONETOUCH",
                        "firmwareVersion": "3.2.19",
                    },
                ],
            }
        ]
        self.states: dict[str, Any] = {
            MAIN_ID: example_state(),
            UPSTAIRS_ID: example_state(tempIndoor=23.0, humIndoor=45),
        }
        self.calls: list[Call] = []
        self.valid_tokens: set[str] = set()
        self.tokens_issued = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.hold: asyncio.Event | None = None
        self._queued: dict[tuple[str, str], deque[Outcome]] = defaultdict(deque)
        self._always: dict[tuple[str, str], Outcome] = {}

    # ------------------------------------------------------------- test hooks

    def session(self) -> Any:
        return FakeSession(self)

    def queue(self, method: str, path: str, *outcomes: Outcome) -> None:
        """Answer the next matching requests with these outcomes, in order."""
        self._queued[(method, path)].extend(outcomes)

    def always(self, method: str, path: str, outcome: Outcome | None) -> None:
        """Answer every matching request with this outcome until cleared with None."""
        if outcome is None:
            self._always.pop((method, path), None)
        else:
            self._always[(method, path)] = outcome

    def expire_tokens(self) -> None:
        self.valid_tokens.clear()

    def calls_to(self, method: str, path: str) -> list[Call]:
        return [c for c in self.calls if c.method == method and c.path == path]

    def count(self, method: str | None = None) -> int:
        return sum(1 for c in self.calls if method is None or c.method == method)

    # ---------------------------------------------------------------- routing

    def handle(self, call: Call) -> Reply:
        key = (call.method, call.path)
        outcome: Outcome | None = None
        if self._queued[key]:
            outcome = self._queued[key].popleft()
        elif key in self._always:
            outcome = self._always[key]
        if outcome is not None:
            if isinstance(outcome, BaseException):
                raise outcome
            if isinstance(outcome, Reply):
                return outcome
            return outcome(call)
        return self._route(call)

    def _route(self, call: Call) -> Reply:
        if call.headers.get("Content-Type") != "application/json":
            return Reply(415, {"messages": "Unsupported Media Type"})
        if call.headers.get("x-api-key") != self.api_key:
            return Reply(403, {"messages": "Forbidden"})

        if call.path == "/v1/token":
            if call.method != "POST":
                return Reply(400, {"messages": "Invalid request body"})
            assert "Authorization" not in call.headers
            body = call.json or {}
            if set(body) != {"email", "integratorToken"}:
                return Reply(400, {"messages": "Invalid request body"})
            if body["email"] != self.email or body["integratorToken"] != self.integrator_token:
                return Reply(400, {"messages": "NotAuthorizedException"})
            self.tokens_issued += 1
            token = f"{ACCESS_TOKEN_PREFIX}{self.tokens_issued}"
            self.valid_tokens.add(token)
            return Reply(
                200,
                {
                    "accessToken": token,
                    "accessTokenExpiresIn": self.expires_in,
                    "tokenType": "Bearer",
                },
            )

        auth = call.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer ") :] not in self.valid_tokens:
            return Reply(401, {"messages": "NotAuthorizedException"})

        if call.path == "/v1/devices" and call.method == "GET":
            return Reply(200, copy.deepcopy(self.locations))

        parts = call.path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["v1", "devices"]:
            device_id = parts[2]
            if device_id not in self.states:
                return Reply(400, {"messages": "Invalid request body"})
            state = self.states[device_id]
            if len(parts) == 3 and call.method == "GET":
                return Reply(200, copy.deepcopy(state))
            if len(parts) == 4 and call.method == "PUT":
                return self._write(state, parts[3], call.json)
        return Reply(400, {"messages": "Invalid request body"})

    @staticmethod
    def _write(state: dict[str, Any], endpoint: str, body: Any) -> Reply:
        invalid = Reply(400, {"messages": "Invalid request body"})
        if not isinstance(body, dict):
            return invalid
        if endpoint == "msp":
            if set(body) != {"mode", "heatSetpoint", "coolSetpoint"}:
                return invalid
            heat, cool = body["heatSetpoint"], body["coolSetpoint"]
            low, high = state["setpointMinimum"], state["setpointMaximum"]
            if type(body["mode"]) is not int or body["mode"] not in (0, 1, 2, 3, 4):
                return invalid
            if not (low <= heat <= high and low <= cool <= high):
                return invalid
            if cool - heat < state["setpointDelta"] - 1e-9:
                return invalid
            state.update(body)
            # "Successful requests ... will turn off schedule and away state."
            state["scheduleEnabled"] = False
        elif endpoint == "schedule":
            if set(body) != {"scheduleEnabled"} or type(body["scheduleEnabled"]) is not bool:
                return invalid
            state.update(body)
        elif endpoint == "fan":
            if set(body) != {"fanCirculate", "fanCirculateSpeed"}:
                return invalid
            if body["fanCirculate"] not in (0, 1, 2) or body["fanCirculateSpeed"] not in (0, 1, 2):
                return invalid
            state.update(body)
        else:
            return invalid
        return Reply(200, {"message": "Success"})
