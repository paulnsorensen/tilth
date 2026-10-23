# express_edit_example_start evidence

Fixture: Express 5.2.1 at `1140301f6a0ed5a05bc1ef38d48294f75a49580c` (`/tmp/tilth_bench/repos/express`).
The grader copies the fixture to a temp directory and never edits the shared clone.

## Migration

Replace the deprecated `module.parent` guard and the hard-coded `app.listen(3000)` in the `examples/` apps with a shared `examples/start.js` (`isMain`, `quiet`, `start`).
The Express `lib/` has too few internal-API sites for a broad migration, so the examples are the only place that meets the bar.

## Reference size

Command: `python3 benchmark/tasks/express_example_start_fixtures/preflight.py`

```
reference size: 27 files, 63 edit sites, +111/-137 lines
```

27 files are 26 changed example files plus the new `examples/start.js`.
Edit sites are `difflib` non-equal opcodes per file.
The baseline `test/acceptance/` suite passes 108 tests on the unmodified fixture.
With the reference applied, acceptance plus the held-out file pass (164 passing).

## Controls

Same command, same run:

```
original: rejected No source changes
reference: accepted Trusted and candidate tests pass
new local helper and test: accepted Trusted and candidate tests pass
helper without migrating examples: rejected Trusted tests failed
two guarded examples not migrated (ejs, web-service): rejected Trusted tests failed
unguarded examples not migrated (static-files, session/redis): rejected Trusted tests failed
route-map and cookies left on NODE_ENV: rejected Trusted tests failed
mvc verbose flag inverted: rejected Trusted tests failed
helper ignores PORT: rejected Trusted tests failed
static-files prints hints when required: rejected Trusted tests failed
existing test edited: rejected Unsafe changed path: test/app.js
```

Every rejection is a real test failure or path rejection.
The preflight fails if a negative control ends in a grader timeout.

## Gates

- `pytest benchmark/tests -q` (the `just check-benchmark` command, run with a venv that has `benchmark/requirements-dev.txt`): gate exit=0, 1674 passed, 11 skipped, after updating the task-count assertion in `test_task_corpus.py` (58 to 59).
