# Testing and dependency updates

The required Python CI job creates an empty virtual environment for each of
Python 3.11, 3.12 and 3.13. It installs the exact third-party versions and wheel
hashes in `requirements/ci.lock`, then builds this checkout offline using the
locked build tools. It includes both `dev` and `cloud` extras. The existing site
workflow separately checks JavaScript and wallet/onboarding behavior.

## Reproduce CI locally

Use uv 0.12.13, the same installer version as the workflow, and an installed
Python version from the matrix. Run these commands from the repository root
in a fresh shell. Each command must succeed before continuing:

```bash
uv venv .ci-venv --python python3.12 --no-python-downloads
uv pip sync --python .ci-venv/bin/python --require-hashes --only-binary :all: --default-index https://pypi.org/simple requirements/ci.lock
uv pip install --python .ci-venv/bin/python --no-deps --no-build-isolation --offline -e ".[dev,cloud]"
uv pip check --python .ci-venv/bin/python
.ci-venv/bin/python tools/check_ci_environment.py
.ci-venv/bin/python -m pytest -q --strict-markers --timeout=30 --junitxml=test-results.xml
```

The guard rejects missing or changed locked versions, unlisted installed
packages, missing hashes and manifest drift in core, cloud, development and
build requirements. A local version such as `1.0+unreviewed` cannot satisfy a
locked `1.0`. The hashed installation verifies downloaded artifacts; the guard
does not attest to their provenance after installation or to the local checkout.
The one exception to the third-party inventory is the locally built OddsRail
distribution. No global site packages or seeded installer packages belong in
this environment.

Cloud tests bind temporary loopback ports. Crash tests require POSIX and kill
only worker processes they create under pytest's temporary directory. No test
requires exchange credentials, wallet secrets or a funded account. Individual
tests time out after 30 seconds; the CI job has a 15-minute limit. JUnit results
are retained for 14 days, including on a failing job.

## Update dependencies deliberately

Change a declared requirement when needed, then regenerate the lock with the
fixed installer version:

```bash
uv pip compile pyproject.toml requirements/ci-tooling.in --extra dev --extra cloud --universal --python-version 3.11 --generate-hashes --no-annotate --output-file requirements/ci.lock --default-index https://pypi.org/simple
```

uv preserves existing compatible pins when the output file already exists.
Use `--upgrade-package <name>` only for an intended update. Review the lock diff
and test a fresh environment on every matrix version. Never repair a red job by
installing an extra dependency manually or removing a financial invariant.
The Polymarket SDK remains explicitly pinned to 0.6.0; delegated session-key
support requires a separate, reviewed SDK/adapter migration.

See the [uv lock documentation](https://docs.astral.sh/uv/pip/compile/) for the
compile/sync workflow and preservation of existing pins.

## What the new tests establish

| Layer | Evidence | Limit |
| --- | --- | --- |
| Process recovery | Real SIGKILL at five durable submission boundaries; two fresh recovery processes; lease fencing; actual SQLite rollback; cancellation attempts; no duplicate submission; retained funds | The independent exchange ledger is a durable fake; no real venue settlement |
| Combined replay | Real feed parser, HTTP metadata parser, planner, supervisor, engine and SQLite operating together through stale data, reconnects, missing metadata and fill/cancel races | Transport, financial venue and clock are controlled fixtures |
| Shared-budget replay | Two agents through 120 logical seconds and 240 completed order lifecycles; four agents compete for one account cap | This is deterministic replay, not a wall-clock load or capacity certification |
| Adapter contracts | Exact acknowledgment/hash and encoded amounts; bounded pagination; identity/economics checks; duplicate/out-of-order fill evidence | Pure validation helpers; no production signer or transport |
| Environment guard | Exact version mismatch, omitted hashes/dependencies, platform markers, unexpected packages and build/tooling drift | CI must still run on the actual target runner |

When a test fails, retain its exact command and result, reproduce the smallest
failing case, fix the behavior and rerun its affected group. Finish with the full
suite in the clean locked environment. Keep the regression assertion that
exposed the defect; do not silently turn an unexpected error into a passing
cleanup or skip.

## Before a funded trial

The adapter contract fixtures are synthetic examples based on documentation,
explicitly labeled as such. Current guide/reference examples disagree about
raw trade quantities and status spellings. Raw trade conversion is deliberately
unsupported until a version-specific authenticated read verifies those units.
Normalized fill quantities must already be explicit Decimal shares. A completed
pagination traversal or a cancel receipt must never establish final settlement.

Session-key enablement, a reviewed production signer/venue adapter, account-wide
funding and permission verification, authenticated reconciliation, deployment
controls and an explicitly authorized small funded trial remain launch gates.
Passing this suite does not enable live trading. See `docs/live-execution.md`.
