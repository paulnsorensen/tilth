# bench-external-followups (pasted findings list from orchestrator)
1. Tampered rule too weak (gold minus last hunk) -> single-hunk removals, largest first, max 8; verdict cache version bump.
2. Agent-written pytest infra can change held-out outcomes -> restore conftest/ini/pyproject[tool.pytest] from base_commit.
3. Grading uses agent-writable <workdir>/.venv -> cached grading venv under $TILTH_BENCH_DATA.
4. External helper modules not in run key -> digest every .py in benchmark/external into identity_inputs.
