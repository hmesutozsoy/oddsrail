"""Exercise the real CI guard against small, explicitly synthetic inventories.

These are parser/manifest regression tests, not a substitute for installing the
real lock in a clean environment. Nothing here invokes an installer or network.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from packaging import markers
import pytest


ROOT = Path(__file__).resolve().parents[1]
HASH = " --hash=sha256:" + "a" * 64


@pytest.fixture
def guard():
    spec = importlib.util.spec_from_file_location("oddsrail_ci_guard_test", ROOT / "tools/check_ci_environment.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def inventory(tmp_path, monkeypatch, guard):
    requirements = tmp_path / "requirements"
    requirements.mkdir()
    versions = {"httpx": "0.28.1", "polymarket-client": "0.6.0", "pytest": "8.3.5",
                "websockets": "15.0.1", "certifi": "2026.7.22", "hatchling": "1.28.0",
                "editables": "0.5"}
    (requirements / "ci.lock").write_text("\n".join(f"{name}=={version}{HASH}" for name, version in versions.items()) + "\n")
    (requirements / "ci-tooling.in").write_text("hatchling\neditables\n")
    (tmp_path / "pyproject.toml").write_text('''
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[project]
name = "oddsrail"
version = "0.18.0"
dependencies = ["httpx>=0.28", "polymarket-client==0.6.0"]
[project.optional-dependencies]
dev = ["pytest>=8"]
cloud = ["websockets>=15,<16", "certifi>=2024"]
''')
    versions["oddsrail"] = "0.18.0"
    monkeypatch.setattr(guard, "metadata", SimpleNamespace(distributions=lambda: [
        SimpleNamespace(metadata={"Name": name}, version=version) for name, version in versions.items()
    ]))
    # This models the predicate, without pretending the test actually created
    # or verified a virtualenv or modifying the running interpreter's globals.
    monkeypatch.setattr(guard, "sys", SimpleNamespace(prefix="/fixture/.venv", base_prefix="/fixture/python"))
    assert guard.check(tmp_path) == []
    return tmp_path, versions


def test_lock_refuses_a_package_without_a_hash(guard):
    with pytest.raises(ValueError, match="SHA-256 hash"):
        guard.locked_requirements("httpx==0.28.1\n")


def test_lock_refuses_ranges_missing_versions_and_wildcard_pins(guard):
    for requirement in ("httpx>=0.28", "httpx", "httpx==0.28.*"):
        with pytest.raises(ValueError, match="exact version"):
            guard.locked_requirements(requirement + HASH)


def test_installed_version_must_match_the_exact_lock_including_local_builds(guard, inventory):
    root, versions = inventory
    for unreviewed in ("0.29.0", "0.28.1+unreviewed"):
        versions["httpx"] = unreviewed
        assert "Lock mismatch: httpx" in guard.check(root)


def test_new_cloud_dependency_cannot_be_omitted_from_the_tested_lock(guard, inventory):
    root, versions = inventory
    del versions["websockets"]
    lock = root / "requirements/ci.lock"
    lock.write_text("\n".join(line for line in lock.read_text().splitlines() if not line.startswith("websockets==")) + "\n")
    assert "Declared dependency missing from tested lock: websockets" in guard.check(root)


def test_windows_only_lock_entry_is_excluded_on_linux(guard, inventory, monkeypatch):
    root, versions = inventory
    environment = markers.default_environment() | {"sys_platform": "linux", "os_name": "posix"}
    monkeypatch.setattr(markers, "default_environment", lambda: environment.copy())
    lock = root / "requirements/ci.lock"
    lock.write_text(lock.read_text() + "colorama==0.4.6 ; sys_platform == 'win32'" + HASH + "\n")
    assert guard.check(root) == []
    # An excluded marker is not a blanket exemption for unrelated installed
    # packages; the Linux environment must still match its selected inventory.
    versions["colorama"] = "0.4.6"
    assert "Unlisted package: colorama" in guard.check(root)


def test_unexpected_preinstalled_package_is_rejected(guard, inventory):
    root, versions = inventory
    versions["unreviewed-helper"] = "1.0"
    assert "Unlisted package: unreviewed-helper" in guard.check(root)


def test_tightened_build_requirement_requires_an_updated_lock(guard, inventory):
    root, _ = inventory
    manifest = root / "pyproject.toml"
    manifest.write_text(manifest.read_text().replace('requires = ["hatchling"]', 'requires = ["hatchling>=2"]'))
    # The installed package still exactly matches the old lock. The build
    # manifest has changed, so checking only the lock inventory would miss it.
    errors = guard.check(root)
    assert "Lock mismatch: hatchling" not in errors
    assert "Declared dependency missing from tested lock: hatchling" in errors


def test_new_ci_tooling_dependency_requires_an_updated_lock(guard, inventory):
    root, _ = inventory
    tooling = root / "requirements/ci-tooling.in"
    tooling.write_text(tooling.read_text() + "\n# Newly needed build hook\nnew-build-hook>=1\n")
    assert "Declared dependency missing from tested lock: new-build-hook" in guard.check(root)
