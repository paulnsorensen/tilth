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
pytest process (within the 4-worker cap). It runs the trusted tree, then the candidate tree.

## Interpreter

The system Python 3.14 lacks `python-multipart`. Put a copy on `PYTHONPATH`
(the grader keeps an existing `PYTHONPATH`) or set `FASTAPI_BENCH_PYTHON`.
The probe requires `multipart` to import.

## Preflight

Command, under `flock /tmp/tilth-gepa-v2-tests.lock`:
`PYTHONPATH=/tmp/fa_deps /usr/bin/python3 benchmark/tasks/fastapi_security_migration_fixtures/preflight.py` (exit 0).

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
```

Incomplete-migration controls: oauth2, openid, one class, one test module.
Behavior-breaking controls: no `TypeError`, wrong `stacklevel`, no attribute alias, inverted default.
The baseline (unchanged) tree passes the pinned tests but fails the held-out tests,
so "No source changes" is rejected before any test runs.
