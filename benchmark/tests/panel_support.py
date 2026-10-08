"""Shared scaffolding for the panel tests: fixture panels, stub admission and rows, hermetic run repos."""

import copy
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
import external
import external.data
import external.featurebench
import external.preflight
import external.swebench_ml
import external_support
import panels
import run
import tasks.base
import tasks.gin_render_context_tasks

FIXTURES = Path(__file__).parent / "fixtures" / "panels"
COMPLETE = FIXTURES / "complete.json"
FALLBACKS = FIXTURES / "fallbacks.json"
SYNTHETIC = FIXTURES / "synthetic"
GEPA_ROWS = FIXTURES / "gepa-v1-rows"
COMMITTED_PANEL = config.BENCHMARK_DIR / "panels" / "gepa-v1.json"

CHEAP = ("rg_search_dispatch", "rg_trait_implementors", "gin_servehttp_flow")
RENDER = "gin_edit_render_context"
FB_ALGORITHMS = "mwaskom__seaborn.7001ebe7.test_algorithms.1f0181c2.lv1"
FB_REGRESSION = "mwaskom__seaborn.7001ebe7.test_regression.ce8c62e2.lv1"
FB_NULLSPACE = "sympy__sympy.c1097516.test_nullspace.f14fc970.lv1"
FB_IDS = (FB_ALGORITHMS, FB_REGRESSION, FB_NULLSPACE)
GO_SLOT, GO_FALLBACK = "gin-gonic__gin-3741", "prometheus__prometheus-14861"
RUST_SLOT, RUST_FALLBACK = "sharkdp__bat-2650", "tokio-rs__tokio-6724"
SYNTHETIC_FB = "fixture__panelpy.00000000.test_total.00000000.lv1"
SYNTHETIC_REPO_LANGUAGES = {"fixture/panelgo": "go", "fixture/panelrs": "rust", "fixture/panelpy": "python"}
RUN_ARGS = ("--models", "sonnet5", "--modes", "plain", "--reps", "1", "--max-usd", "50")

# The stratified assignment of complete.json (seed 7), computed by hand from
# sha256("7:<id>") within each family: one local, three FeatureBench, Go then Rust.
EXPECTED_DEV = {RENDER, FB_ALGORITHMS, FB_NULLSPACE, GO_SLOT}
EXPECTED_TEST = {FB_REGRESSION, RUST_SLOT}


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def write(tmp_path: Path, data: dict, name: str = "panel.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data, indent=2))
    return path


def restratify(data: dict) -> dict:
    """``data`` with every non-cheap member's split set to ``panels.stratify``'s assignment."""
    data = copy.deepcopy(data)
    assignment = panels.stratify(data["members"], data["split_seed"])
    for member in data["members"]:
        if member["id"] in assignment:
            member["split"] = assignment[member["id"]]
    return data


def without(data: dict, *ids: str) -> dict:
    return {**data, "members": [member for member in data["members"] if member["id"] not in ids]}


def member(data: dict, member_id: str) -> dict:
    return next(entry for entry in data["members"] if entry["id"] == member_id)


def repo_of(instance_id: str) -> str:
    if external.featurebench.level_of(instance_id):
        return external.featurebench.repo_of(instance_id)
    return instance_id.rsplit("-", 1)[0].replace("__", "/", 1)


def row_source(instance_id: str) -> dict:
    """A redacted dataset row whose ``repo`` drives ``external.language_of``."""
    return {"instance_id": instance_id, "repo": repo_of(instance_id), "base_commit": "0" * 40}


def verdict(instance_id: str, admitted: bool = True, reason: str = "admitted") -> external.preflight.PreflightVerdict:
    return external.preflight.PreflightVerdict(
        instance_id=instance_id, dataset="stub", data_rev="stub", env_fingerprint="stub",
        admitted=admitted, reason=reason,
    )


def admit_all(instance_id: str) -> external.preflight.PreflightVerdict:
    return verdict(instance_id)


def refusing(*refused: str, reason: str = "tampered_resolved"):
    def admit(instance_id: str) -> external.preflight.PreflightVerdict:
        return verdict(instance_id, False, reason) if instance_id in refused else verdict(instance_id)
    return admit


def load(path: Path, store_path: Path, *, admit=admit_all, rows=row_source) -> panels.Panel:
    return panels.load_panel(path, store_path=store_path, admit=admit, row_source=rows)


def stamp_digest(data: dict) -> str:
    """The split digest computed independently of ``panels`` from a manifest's id lists."""
    lists = {split: sorted(entry["id"] for entry in data["members"] if entry["split"] == split)
             for split in ("cheap", "dev", "test")}
    return hashlib.sha256(json.dumps(lists, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def synthetic_rows() -> dict[str, dict]:
    return {row["instance_id"]: row for row in json.loads((SYNTHETIC / "rows.json").read_text())}


def stub_task(instance_id: str, revision: str, *, problem: str | None = None) -> external.ExternalTask:
    """A real ExternalTask built from a synthetic row, renamed to ``instance_id``."""
    rows = synthetic_rows()
    if external.featurebench.level_of(instance_id):
        row = {**rows[SYNTHETIC_FB], "instance_id": instance_id, "repo": repo_of(instance_id)}
        task_class = external.featurebench.FeatureBenchTask
    else:
        template = GO_SLOT if repo_of(instance_id) in {"gin-gonic/gin", "prometheus/prometheus"} else RUST_SLOT
        row = {**rows[template], "instance_id": instance_id, "repo": repo_of(instance_id)}
        task_class = external.swebench_ml.SweBenchTask
    if problem is not None:
        row["problem_statement"] = problem
    return task_class(row, revision)


def stub_loaders(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    loaded: list[str] = []

    def loader(instance_id: str, revision: str) -> external.ExternalTask:
        loaded.append(instance_id)
        return stub_task(instance_id, revision)

    monkeypatch.setattr(external.featurebench, "load", loader)
    monkeypatch.setattr(external.swebench_ml, "load", loader)
    return loaded


def _tiny_repo(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True)
    external_support.git("init", "-q", "-b", "main", cwd=path)
    external_support.write_tree(path, files)
    external_support.git("add", "-A", cwd=path)
    external_support.git("commit", "-q", "-m", "fixture", cwd=path)
    return external_support.git("rev-parse", "HEAD", cwd=path).strip()


def hermetic_repos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point every ``REPOS`` binding and the Gin render fixture at tiny temp git repos."""
    repos_dir = tmp_path / "repos"
    heads = {
        "ripgrep": _tiny_repo(repos_dir / "ripgrep", {"Cargo.toml": "[package]\nname = \"rg\"\n", "src/main.rs": "fn main() {}\n"}),
        "gin": _tiny_repo(repos_dir / "gin", {"go.mod": "module example.com/gin\n", "gin.go": "package gin\n"}),
    }
    for name in panels.LOCAL:
        # ``source`` is a cached property on shared task objects: pin the real one so
        # nothing caches the temp commit beyond this test.
        monkeypatch.setattr(run.TASKS[name], "source", run.TASKS[name].source)
    repos = {name: replace(repo, commit_sha=heads.get(name, repo.commit_sha)) for name, repo in config.REPOS.items()}
    monkeypatch.setattr(config, "REPOS_DIR", repos_dir)
    for module in (config, run, tasks.base):
        monkeypatch.setattr(module, "REPOS", repos)
    monkeypatch.setattr(tasks.gin_render_context_tasks, "FIXTURE", repos_dir / "gin")


@dataclass
class PanelRun:
    """``run.main --panel`` against hermetic repos, a stub admit, and a stub row source."""

    bench: object
    monkeypatch: pytest.MonkeyPatch
    tmp_path: Path
    loaded: list[str] = field(default_factory=list)

    def panel(self, data: dict, name: str = "panel.json") -> Path:
        return write(self.tmp_path, data, name)

    def main(self, path: Path, *extra: str) -> int:
        return self.bench.main("--panel", str(path), *extra, *RUN_ARGS)

    @property
    def called(self) -> list[str]:
        return [task for task, _mode, _rep in self.bench.calls]


def panel_run(bench, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PanelRun:
    hermetic_repos(monkeypatch, tmp_path)
    monkeypatch.setattr(run, "TASKS", dict(run.TASKS))
    monkeypatch.setattr(external.preflight, "admit", admit_all)
    monkeypatch.setattr(external.data, "cached_row", row_source)
    harness = PanelRun(bench=bench, monkeypatch=monkeypatch, tmp_path=tmp_path)
    harness.loaded = stub_loaders(monkeypatch)
    bench.runner(lambda _stream: 0.1)
    return harness


def seed_synthetic_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Cache the synthetic rows and their upstream mirrors in a temp harness data directory."""
    data_dir = tmp_path / "bench-data"
    upstream = tmp_path / "upstream"
    monkeypatch.setenv("TILTH_BENCH_DATA", str(data_dir))
    for repo, language in SYNTHETIC_REPO_LANGUAGES.items():
        monkeypatch.setitem(external.data.REPO_LANGUAGES, repo, language)
    for repo, files in read(SYNTHETIC / "projects.json").items():
        external_support.build_upstream(repo, files, upstream)
    for row in synthetic_rows().values():
        dataset = external.data.FEATUREBENCH if "repo_settings" in row else external.data.SWEBENCH_ML
        path = external.data.row_path(dataset, external.data.REVISIONS[dataset], row["instance_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row))
        mirror = external.data.mirror_path(row["repo"])
        if not mirror.exists():
            subprocess.run(["git", "clone", "-q", "--bare", str(upstream / f"{row['repo']}.git"), str(mirror)],
                           check=True, capture_output=True)


def synthetic_panel() -> dict:
    members = [
        *(member(read(COMPLETE), name) for name in (*CHEAP, RENDER)),
        {"id": SYNTHETIC_FB, "family": "featurebench", "language": "python", "split": "dev"},
        {"id": GO_SLOT, "family": "swebench_ml", "language": "go", "split": "dev"},
        {"id": RUST_SLOT, "family": "swebench_ml", "language": "rust", "split": "test"},
    ]
    return restratify({"name": "fixture-synthetic", "split_seed": 7, "members": members})
