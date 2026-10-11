"""The live check script's own safety properties (it is never run against Daikin here)."""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "live_check.py"


@pytest.fixture(scope="module")
def live_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location("live_check", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_client_package_loads_without_home_assistant(live_check: ModuleType) -> None:
    api = sys.modules["daikinone_api"]
    assert api.API_HOST == "integrator-api.daikinskyport.com"
    assert live_check.api is api


def test_shape_never_contains_values(live_check: ModuleType) -> None:
    payload = [
        {
            "locationName": "SECRET-PLACE",
            "devices": [{"id": "SECRET-ID", "n": 5, "ok": True, "x": None, "t": 1.5}],
        }
    ]
    described = live_check.shape(payload)
    assert "SECRET" not in repr(described)
    assert described == [
        {
            "locationName": "str",
            "devices": [
                {"id": "str", "n": "int", "ok": "bool", "x": "NoneType", "t": "float"},
                "... 1 item(s)",
            ],
        },
        "... 1 item(s)",
    ]


def test_refuses_to_run_twice_within_three_minutes(
    live_check: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stamp = tmp_path / "stamp"
    monkeypatch.setattr(live_check, "STAMP", stamp)
    assert live_check.seconds_until_allowed() == 0

    stamp.write_text(str(time.time()))
    assert 175 < live_check.seconds_until_allowed() <= 180
    stamp.write_text(str(time.time() - 170))
    assert 5 < live_check.seconds_until_allowed() <= 10
    stamp.write_text(str(time.time() - 181))
    assert live_check.seconds_until_allowed() == 0
    stamp.write_text("garbage")
    assert live_check.seconds_until_allowed() == 0


async def test_missing_credentials_stop_before_any_request(
    live_check: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(live_check, "STAMP", tmp_path / "stamp")
    monkeypatch.setenv("DAIKIN_EMAIL", "someone@somewhere.invalid")
    monkeypatch.setenv("DAIKIN_API_KEY", "")
    monkeypatch.delenv("DAIKIN_INTEGRATOR_TOKEN", raising=False)
    assert await live_check.run() == 2
    output = capsys.readouterr().out
    assert "DAIKIN_API_KEY, DAIKIN_INTEGRATOR_TOKEN" in output
    assert "someone" not in output
    assert not (tmp_path / "stamp").exists()
