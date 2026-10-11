"""Every translation key the code uses exists in the English strings."""

from __future__ import annotations

import json
import re
import string
from pathlib import Path
from typing import Any

import pytest

INTEGRATION = Path(__file__).parent.parent / "custom_components" / "daikinone"
STRINGS: dict[str, Any] = json.loads((INTEGRATION / "translations" / "en.json").read_text("utf-8"))
SOURCE = {path.name: path.read_text("utf-8") for path in sorted(INTEGRATION.glob("*.py"))}
ALL_SOURCE = "\n".join(SOURCE.values())

EXCEPTION_KEY = re.compile(
    r"""(?:translation_key=|_error\(\s*|_invalid\(\s*|SetpointError\(\s*)"([a-z_]+)\""""
)
ENTITY_KEY = re.compile(r"""(?:_attr_translation_key\s*=\s*|translation_key=|key=)"([a-z_]+)\"""")
FLOW_KEY = re.compile(r"""(?:return\s+|errors\["base"\]\s*=\s*|reason=)"([a-z_]+)\"""")


def placeholders(message: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(message) if name}


def test_exception_keys_used_in_code_are_translated() -> None:
    sources = "\n".join(SOURCE[name] for name in ("coordinator.py", "setpoints.py", "climate.py"))
    used = set(EXCEPTION_KEY.findall(sources))
    assert {"state_unknown", "setpoint_no_room", "rate_limited", "mode_not_supported"} <= used
    assert used <= set(STRINGS["exceptions"])


def test_no_unused_exception_translations() -> None:
    for key in STRINGS["exceptions"]:
        assert f'"{key}"' in ALL_SOURCE, f"exception translation {key} is not used"


@pytest.mark.parametrize("key", sorted(STRINGS["exceptions"]))
def test_exception_messages_use_only_known_placeholders(key: str) -> None:
    allowed = {"name", "seconds", "reason", "minimum", "maximum", "delta", "unit", "mode"}
    assert placeholders(STRINGS["exceptions"][key]["message"]) <= allowed


def test_setpoint_messages_have_the_placeholders_the_code_supplies() -> None:
    limits = {"name", "minimum", "maximum", "delta", "unit"}
    for key in ("setpoint_out_of_range", "setpoints_too_close", "setpoint_no_room"):
        assert placeholders(STRINGS["exceptions"][key]["message"]) <= limits
    for key in ("state_unknown", "limits_unknown", "thermostat_unavailable"):
        assert placeholders(STRINGS["exceptions"][key]["message"]) == {"name"}
    assert placeholders(STRINGS["exceptions"]["rate_limited"]["message"]) == {"seconds"}
    assert placeholders(STRINGS["exceptions"]["cannot_connect"]["message"]) == {"reason"}
    assert placeholders(STRINGS["exceptions"]["auth_failed"]["message"]) == set()


def test_config_flow_errors_and_aborts_are_translated() -> None:
    used = set(FLOW_KEY.findall(SOURCE["config_flow.py"]))
    errors = {"invalid_auth", "cannot_connect", "rate_limited", "unknown"}
    assert errors <= used
    assert errors == set(STRINGS["config"]["error"])
    assert "account_mismatch" in used
    assert {
        "already_configured",
        "reauth_successful",
        "reconfigure_successful",
        "account_mismatch",
    } == set(STRINGS["config"]["abort"])
    assert set(STRINGS["config"]["step"]) == {"user", "reauth_confirm", "reconfigure"}


def test_entity_translation_keys_exist() -> None:
    for platform, keys in STRINGS["entity"].items():
        source = SOURCE.get(f"{platform}.py")
        if source is None:
            continue
        used = set(ENTITY_KEY.findall(source))
        assert set(keys) <= used, f"{platform}: translations without an entity"
