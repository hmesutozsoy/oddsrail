"""Check that CI is using exactly the reviewed lock and declared cloud extras.

This check does not install anything, contact an index, or inspect credentials.
Run after syncing requirements/ci.lock and installing the local project with
--no-deps --no-build-isolation. Dependency updates must regenerate the lock.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
import re
import sys
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]
HASH = re.compile(r"\s+--hash=sha256:[0-9a-f]{64}(?=\s|$)")


def locked_requirements(text: str) -> dict[str, Requirement]:
    selected = {}
    for line in text.replace("\\\n", " ").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not HASH.search(line):
            raise ValueError("Every locked package must have an SHA-256 hash")
        req = Requirement(HASH.sub("", line).strip())
        pins = list(req.specifier)
        if req.url or len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version:
            raise ValueError("Every locked package must have an exact version")
        if req.marker and not req.marker.evaluate():
            continue
        name = canonicalize_name(req.name)
        if name in selected:
            raise ValueError("Overlapping lock entries")
        selected[name] = req
    if not selected:
        raise ValueError("The environment lock is empty")
    return selected


def check(root: Path = ROOT) -> list[str]:
    errors = []
    if sys.prefix == sys.base_prefix:
        errors.append("Use an isolated virtual environment")
    locked = locked_requirements((root / "requirements/ci.lock").read_text())
    installed = {canonicalize_name(dist.metadata["Name"]): dist.version for dist in metadata.distributions()}
    for name, req in locked.items():
        # Specifier membership permits unreviewed local builds (1.0+local
        # satisfies ==1.0). A lock requires the exact version, including local
        # identifiers, rather than dependency-range compatibility.
        pinned = next(iter(req.specifier)).version
        if name not in installed or Version(installed[name]) != Version(pinned):
            errors.append(f"Lock mismatch: {name}")
    # The project is installed from this checkout, separately from hashed
    # third-party artifacts. No other preinstalled packages are permitted.
    for name in installed.keys() - locked.keys() - {"oddsrail"}:
        errors.append(f"Unlisted package: {name}")
    if "oddsrail" not in installed:
        errors.append("Install the local OddsRail project")
    manifest = tomllib.loads((root / "pyproject.toml").read_text())
    config = manifest["project"]
    declared = list(config["dependencies"])
    for group in ("dev", "cloud"):
        declared.extend(config["optional-dependencies"][group])
    declared.extend(manifest["build-system"]["requires"])
    # ci-tooling.in deliberately contains only PEP 508 requirements and
    # comments. Index overrides, nested files and installer flags are invalid.
    declared.extend(line.strip() for line in (root / "requirements/ci-tooling.in").read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith("#"))
    for raw in declared:
        req = Requirement(raw)
        if req.marker and not any(req.marker.evaluate({"extra": extra}) for extra in ("", "dev", "cloud")):
            continue
        name = canonicalize_name(req.name)
        if name not in installed or installed[name] not in req.specifier or name not in locked:
            errors.append(f"Declared dependency missing from tested lock: {name}")
    return sorted(set(errors))


def main() -> int:
    try:
        errors = check()
    except (ValueError, OSError) as exc:
        print(f"Invalid test environment: {exc}")
        return 1
    for error in errors:
        print(error)
    if errors:
        return 1
    print("Exact locked dependencies, dev/cloud extras and build tools verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
