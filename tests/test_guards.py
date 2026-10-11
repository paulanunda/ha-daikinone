"""Static checks on the integration source: allowed hosts and import boundaries."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.fake_daikin import CONTRACT

ROOT = Path(__file__).parent.parent
INTEGRATION = ROOT / "custom_components" / "daikinone"
API_PACKAGE = INTEGRATION / "api"
ALLOWED_HOST: str = CONTRACT["transport"]["host"]

SOURCE_FILES = sorted(
    path
    for path in [*INTEGRATION.rglob("*"), *(ROOT / "scripts").rglob("*")]
    if path.is_file() and path.suffix in {".py", ".json", ".yaml", ".yml", ".toml"}
)

# Anything that looks like a URL authority or a bare DNS name.
URL_HOST = re.compile(r"[a-z][a-z0-9+.-]*://([^/\s\"'<>{}]+)", re.IGNORECASE)
BARE_HOST = re.compile(
    r"(?<![\w.@-])((?:[a-z0-9-]+\.)+(?:com|net|org|io|dev|app|cloud|co|us|jp|info|biz|ai|me))(?![\w-])",
    re.IGNORECASE,
)
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def hosts_in(text: str) -> set[str]:
    found = {match.group(1).lower().rpartition("@")[2] for match in URL_HOST.finditer(text)}
    found = {host.split(":")[0] for host in found}
    found |= {match.group(1).lower() for match in BARE_HOST.finditer(text)}
    found |= set(IPV4.findall(text))
    return found


def test_host_scanner_catches_what_it_should() -> None:
    sample = """
        url = "https://api.daikinskyport.com/users/auth/login"
        other = 'wss://Example.ORG:8443/socket'
        bare = "telemetry.example.io"
        ip = "10.0.0.5"
        fine = f"https://{API_HOST}/v1"
        version = "1.2.3"
    """
    assert hosts_in(sample) == {
        "api.daikinskyport.com",
        "example.org",
        "telemetry.example.io",
        "10.0.0.5",
    }


def test_source_files_were_found() -> None:
    names = {path.name for path in SOURCE_FILES}
    assert {"client.py", "manifest.json", "__init__.py"} <= names


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_only_the_official_api_host_appears_in_source(path: Path) -> None:
    hosts = hosts_in(path.read_text(encoding="utf-8"))
    assert hosts <= {ALLOWED_HOST}, f"unexpected host(s) in {path.name}: {hosts - {ALLOWED_HOST}}"


def test_the_official_host_is_defined_exactly_once() -> None:
    defining = [
        path.relative_to(ROOT).as_posix()
        for path in SOURCE_FILES
        if ALLOWED_HOST in path.read_text(encoding="utf-8")
    ]
    assert defining == ["custom_components/daikinone/api/client.py"]


API_FILES = sorted(API_PACKAGE.rglob("*.py"))

# The standard library plus aiohttp, which Home Assistant core always ships.
ALLOWED_API_IMPORTS = {
    "__future__",
    "aiohttp",
    "asyncio",
    "collections",
    "dataclasses",
    "datetime",
    "email",
    "enum",
    "json",
    "logging",
    "math",
    "ssl",
    "time",
    "typing",
    "urllib",
}


@pytest.mark.parametrize("path", API_FILES, ids=lambda p: p.name)
def test_api_package_imports_nothing_from_home_assistant(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative imports must stay inside the api package.
                assert node.level == 1, f"{path.name} imports from outside the api package"
                continue
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            assert module.split(".")[0] in ALLOWED_API_IMPORTS, f"{path.name} imports {module}"


def test_session_request_is_called_in_exactly_one_place() -> None:
    """Every HTTP exchange goes through DaikinOneClient._send."""
    hits = []
    for path in sorted(INTEGRATION.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {"request", "get", "post", "put", "patch", "delete", "ws_connect"}
                and isinstance(node.func.value, ast.Attribute)
                and "session" in node.func.value.attr
            ):
                hits.append((path.name, node.func.attr))
    assert hits == [("client.py", "request")]
