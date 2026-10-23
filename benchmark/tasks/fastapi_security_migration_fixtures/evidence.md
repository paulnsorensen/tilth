# Evidence: fastapi_edit_security_raise_on_missing

Pinned fixture: FastAPI 6fa573ce (`/tmp/tilth_bench/repos/fastapi`). No model ran.

## Migration

Rename the `auto_error` keyword to `raise_on_missing` on all 11 security schemes
(`APIKeyQuery/Header/Cookie`, `HTTPBase/Basic/Bearer/Digest`, `OAuth2`,
`OAuth2PasswordBearer`, `OAuth2AuthorizationCodeBearer`, `OpenIdConnect`).
The old keyword stays as a deprecated alias.

A single sed cannot do this. Each class needs a different signature and `Doc` text,
the old keyword needs a shared resolver, and `HTTPBase` uses a plain `bool`
annotation. The `base.py` property alias, the `stacklevel`, and the `TypeError`
when both keywords are given are judgment sites.

## Reference size

`20 files, 76 edit sites, +203/-84 lines` (4 security modules, `base.py`, 15 test modules).
The bar was at least 10 files and 40 sites.

## Grader

Rejects paths outside `fastapi/security/*.py` and the original affected tests.
Copies the permitted source into a fresh copy of the fixture. Restores the pinned
tests with a mechanical rename, injects the held-out tests, and runs 41 modules in one
pytest process (within the 4-worker cap). It runs the trusted tree, then the candidate tree,
under one 30-minute deadline. The held-out file also checks that each documented scheme's
`raise_on_missing` `Doc` names the new keyword.

## Interpreter

The grader runs `uv run pytest -q -p no:cacheprovider <modules>` in the trusted and
candidate trees, like the sibling FastAPI tasks. `uv` resolves dependencies from the
fixture's own `pyproject.toml` and `uv.lock`, which the fresh copy keeps (only `.git`
and `__pycache__` are dropped). `uv` builds the project environment from its wheel
cache, so a repeat grade installs 234 packages in about 100 ms without re-resolving.
No interpreter probe, `PYTHONPATH`, or `FASTAPI_BENCH_PYTHON` is involved.

## Preflight

Command, under `flock /tmp/tilth-gepa-v2-tests.lock`:
`PATH=/tmp/tilth-bench-venv/bin:/usr/bin:/bin:<linuxbrew>:<uv> python3 benchmark/tasks/fastapi_security_migration_fixtures/preflight.py` (exit 0).
No `PYTHONPATH` or `FASTAPI_BENCH_PYTHON` override. The bench venv has no FastAPI dependencies, so `uv` supplies them.

```
original: rejected No source changes
reference size: 20 files, 76 edit sites, +203/-84 lines
reference: accepted Trusted and candidate tests pass
new local helper and test: accepted Trusted and candidate tests pass
unapproved changed path: rejected Unsafe changed path: fastapi/utils.py
one existing test still passes auto_error: rejected Candidate tests failed: pytest 39 modules
oauth2.py left on auto_error: rejected Trusted tests failed: pytest 41 modules
open_id_connect_url.py left on auto_error: rejected Trusted tests failed: pytest 41 modules
HTTPBase ignores its keyword: rejected Trusted tests failed: pytest 41 modules
both keywords accepted: rejected Trusted tests failed: pytest 41 modules
warning points inside FastAPI: rejected Trusted tests failed: pytest 41 modules
auto_error attribute removed: rejected Trusted tests failed: pytest 41 modules
default flipped to optional: rejected Trusted tests failed: pytest 41 modules
api_key.py docs still name auto_error: rejected Trusted tests failed: pytest 41 modules
```

Incomplete-migration controls: oauth2, openid, one class, one test module, stale parameter docs.
Behavior-breaking controls: no `TypeError`, wrong `stacklevel`, no attribute alias, inverted default.
The unchanged tree is rejected by the "No source changes" guard before any test runs.
