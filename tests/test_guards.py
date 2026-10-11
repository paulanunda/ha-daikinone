"""Static checks on the integration source: allowed hosts and import boundaries."""

from __future__ import annotations

import ast
import io
import json
import re
import sys
import tokenize
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
    r"(?<![\w.@-])\.?((?:[a-z0-9-]+\.)+(?:com|net|org|io|dev|app|cloud|co|us|jp|info|biz|ai|me))(?![\w-])",
    re.IGNORECASE,
)
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def _literal_text(source: str) -> str:
    """Return the string literals and comments of Python source.

    Bare host names are only looked for there, so that attribute access such
    as ``thermostat.info`` is not mistaken for a domain.
    """
    kinds = {tokenize.STRING, tokenize.COMMENT, tokenize.FSTRING_MIDDLE}
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return "\n".join(token.string for token in tokens if token.type in kinds)


def hosts_in(text: str, *, python: bool = False) -> set[str]:
    """Return every host name or IP address that appears in ``text``."""
    found = {match.group(1).lower().rpartition("@")[2] for match in URL_HOST.finditer(text)}
    found = {host.split(":")[0] for host in found}
    literal = _literal_text(text) if python else text
    found |= {match.group(1).lower() for match in BARE_HOST.finditer(literal)}
    found |= set(IPV4.findall(literal))
    return found


def test_host_scanner_catches_what_it_should() -> None:
    sample = """
url = "https://api.daikinskyport.com/users/auth/login"
other = 'wss://Example.ORG:8443/socket'
bare = "telemetry.example.io"
ip = "10.0.0.5"
# see docs.example.net for details
joined = f"{scheme}://{prefix}.example.dev/path"
fine = f"https://{API_HOST}/v1"
version = "1.2.3"
value = thermostat.info
"""
    assert hosts_in(sample, python=True) == {
        "api.daikinskyport.com",
        "example.org",
        "telemetry.example.io",
        "10.0.0.5",
        "docs.example.net",
        "example.dev",  # a host assembled inside an f-string is still seen
    }
    assert "thermostat.info" in hosts_in(sample)


def test_source_files_were_found() -> None:
    names = {path.name for path in SOURCE_FILES}
    assert {"client.py", "manifest.json", "__init__.py"} <= names


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_only_the_official_api_host_appears_in_source(path: Path) -> None:
    hosts = hosts_in(path.read_text(encoding="utf-8"), python=path.suffix == ".py")
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


# --------------------------------------------------------------- packaging


def test_manifest_declares_no_third_party_requirements() -> None:
    manifest = json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == "daikinone"
    assert manifest["requirements"] == []
    assert manifest["dependencies"] == []
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "cloud_polling"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])


# Modules that ship with Python or with every Home Assistant installation.
PROVIDED_BY_HOME_ASSISTANT = {"homeassistant", "aiohttp", "probatio"}


def test_integration_imports_only_what_home_assistant_guarantees() -> None:
    third_party: set[str] = set()
    for path in sorted(INTEGRATION.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top not in sys.stdlib_module_names:
                    third_party.add(top)
    assert third_party <= PROVIDED_BY_HOME_ASSISTANT


def test_hacs_layout() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["name"]
    assert [p.name for p in (ROOT / "custom_components").iterdir() if p.is_dir()] == ["daikinone"]
    assert (INTEGRATION / "translations" / "en.json").is_file()


def test_live_check_script_is_read_only() -> None:
    """The live check may fetch a token and read; it must never write."""
    source = (ROOT / "scripts" / "live_check.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert {"authenticate", "list_devices", "get_thermostat"} <= called
    assert not {name for name in called if name.startswith("set_")}
    assert not {"_request", "request", "put", "post", "patch", "delete"} & called
    strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "PUT" not in strings


def test_local_credentials_file_is_ignored_and_untracked() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert "mise.local.toml" in ignored
    assert not (ROOT / ".envrc").exists()
