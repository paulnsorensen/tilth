#!/usr/bin/env python3
"""Provisioning + offline-capture orchestrator for the Tilth extraction
reuse comparison (curd 2).

Stdlib only. See benchmark/extraction/README.md for the full walkthrough.

Subcommands:
  provision  NETWORK. Warms the tree-sitter-language-pack runtime grammar
             cache. ast-grep-outline needs no provisioning: its grammars are
             statically linked, so this subcommand does nothing for it.
  capture    OFFLINE. Builds each candidate, runs its capture subcommand
             with a poisoned proxy environment (proves no live network
             access), validates every emitted record against the v1
             schemas, and writes an AC-4 measurement/evidence record under
             .generated/evidence/ (gitignored).
"""

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

EXTRACTION_ROOT = Path(__file__).resolve().parent
REPO_ROOT = EXTRACTION_ROOT.parent.parent
ROOT_LOCKFILE = REPO_ROOT / "Cargo.lock"
FIXTURE_MANIFEST = EXTRACTION_ROOT / "fixtures" / "manifest.json"
EVIDENCE_DIR = EXTRACTION_ROOT / ".generated" / "evidence"

# Grammar cache tree-sitter-language-pack warms on `provision`. Version-
# scoped per the pinned `=1.17.0` dependency (see candidate Cargo.toml).
TSL_PACK_CACHE_DIR = Path.home() / "Library" / "Caches" / "tree-sitter-language-pack" / "v1.17.0"

CAPTURE_REPS = 5

CANDIDATES = {
    "tree-sitter-language-pack": {
        "dir": EXTRACTION_ROOT / "candidates" / "tree-sitter-language-pack",
        "bin_name": "tsl-pack-candidate",
        "crate_name": "tree-sitter-language-pack",
        "needs_provision": True,
    },
    "ast-grep-outline": {
        "dir": EXTRACTION_ROOT / "candidates" / "ast-grep-outline",
        "bin_name": "ast-grep-outline-candidate",
        "crate_name": "ast-grep-outline",
        "needs_provision": False,
    },
}


# ---------------------------------------------------------------------------
# small stdlib helpers
# ---------------------------------------------------------------------------


def run(cmd, cwd=None, env=None, timeout=None):
    """Runs `cmd`, capturing text stdout/stderr. Never raises on nonzero exit;
    callers inspect `.returncode`."""
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def lockfile_package_version(lockfile_path, crate_name):
    """Hand-rolled Cargo.lock scan: returns the resolved version for
    `crate_name`'s `[[package]]` block, or None if absent."""
    current_name = None
    for line in Path(lockfile_path).read_text().splitlines():
        line = line.strip()
        if line.startswith("name = "):
            current_name = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("version = ") and current_name == crate_name:
            return line.split("=", 1)[1].strip().strip('"')
    return None


def poisoned_proxy_env():
    """Child env with bogus proxy vars: any live network access during
    measured capture fails loudly instead of silently succeeding."""
    env = os.environ.copy()
    bogus = "http://127.0.0.1:1"
    for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[key] = bogus
    return env


def fixture_hashes():
    """Copies fixture hashes from the frozen manifest; never recomputes."""
    manifest = json.loads(FIXTURE_MANIFEST.read_text())
    return [
        {"id": f["id"], "language": f["language"], "hash": f["hash"]}
        for f in manifest["fixtures"]
    ]


def fixture_ids():
    manifest = json.loads(FIXTURE_MANIFEST.read_text())
    return [f["id"] for f in manifest["fixtures"]]


def toolchain_versions():
    rustc = run(["rustc", "--version"])
    cargo = run(["cargo", "--version"])
    return {
        "rustc": rustc.stdout.strip() if rustc.returncode == 0 else f"error: {rustc.stderr.strip()}",
        "cargo": cargo.stdout.strip() if cargo.returncode == 0 else f"error: {cargo.stderr.strip()}",
    }


def baseline_commit():
    result = run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# hand-rolled v1 schema validation (no jsonschema dependency)
# ---------------------------------------------------------------------------


def validate_raw_entry(entry, path, errors):
    required = ("kind", "name", "start_line", "span_start_line", "end_line", "children")
    for key in required:
        if key not in entry:
            errors.append(f"{path}: entry missing required key '{key}'")
    if not isinstance(entry.get("kind"), str) or not entry.get("kind"):
        errors.append(f"{path}: 'kind' must be a non-empty string")
    for idx, child in enumerate(entry.get("children", []) or []):
        validate_raw_entry(child, f"{path}.children[{idx}]", errors)


def validate_raw_record(record):
    errors = []
    for key in ("version", "fixture_id", "language", "entries"):
        if key not in record:
            errors.append(f"raw record missing required key '{key}'")
    if record.get("version") != 1:
        errors.append(f"raw record version must be 1, got {record.get('version')!r}")
    for idx, entry in enumerate(record.get("entries", []) or []):
        validate_raw_entry(entry, f"entries[{idx}]", errors)
    return errors


CAPABILITY_STATUSES = {"supported", "unsupported", "error"}
NORMALIZED_CAPABILITIES = ("definitions", "nesting", "signatures", "imports", "edit_spans")


def validate_capability_result(result, path, errors):
    if "status" not in result:
        errors.append(f"{path}: capability result missing 'status'")
        return
    status = result["status"]
    if status not in CAPABILITY_STATUSES:
        errors.append(f"{path}: status {status!r} not in {sorted(CAPABILITY_STATUSES)}")
        return
    if status == "error" and not isinstance(result.get("value"), str):
        errors.append(f"{path}: status 'error' must carry a string reason in 'value'")


def validate_normalized_record(record):
    errors = []
    for key in ("version", "fixture_id") + NORMALIZED_CAPABILITIES:
        if key not in record:
            errors.append(f"normalized record missing required key '{key}'")
    if record.get("version") != 1:
        errors.append(f"normalized record version must be 1, got {record.get('version')!r}")
    for capability in NORMALIZED_CAPABILITIES:
        if capability in record:
            validate_capability_result(record[capability], capability, errors)
    return errors


# ---------------------------------------------------------------------------
# build + measurement
# ---------------------------------------------------------------------------


class Blocked(Exception):
    def __init__(self, prerequisite, reason):
        super().__init__(reason)
        self.prerequisite = prerequisite
        self.reason = reason


def binary_path(meta):
    return meta["dir"] / "target" / "release" / meta["bin_name"]


def clean_build(meta):
    """Runs `cargo clean` then a timed release build for one candidate.
    Returns (clean_build_seconds, executable_size_bytes)."""
    manifest_path = meta["dir"] / "Cargo.toml"
    cleaned = run(["cargo", "clean", "--manifest-path", str(manifest_path)])
    if cleaned.returncode != 0:
        raise Blocked("cargo clean", cleaned.stderr.strip())
    start = time.monotonic()
    built = run(["cargo", "build", "--release", "--manifest-path", str(manifest_path)])
    elapsed = time.monotonic() - start
    if built.returncode != 0:
        raise Blocked("cargo build --release", built.stderr.strip())
    exe = binary_path(meta)
    if not exe.exists():
        raise Blocked("cargo build --release", f"expected binary missing at {exe}")
    return elapsed, os.path.getsize(exe)


def provision_status(meta):
    if not meta["needs_provision"]:
        return "not_required: grammars are statically linked into the candidate binary"
    if TSL_PACK_CACHE_DIR.is_dir() and any(TSL_PACK_CACHE_DIR.iterdir()):
        return f"provisioned: grammar cache present at {TSL_PACK_CACHE_DIR}"
    return None


def offline_capture_command(meta, out_dir):
    return [str(binary_path(meta)), "capture", "--manifest", str(FIXTURE_MANIFEST), "--out", str(out_dir)]


def run_offline_capture(meta, out_dir):
    """Runs the candidate's offline capture subcommand once under a poisoned
    proxy environment. Returns elapsed seconds; raises Blocked on failure."""
    cmd = offline_capture_command(meta, out_dir)
    start = time.monotonic()
    result = run(cmd, env=poisoned_proxy_env())
    elapsed = time.monotonic() - start
    if result.returncode != 0:
        raise Blocked("offline capture", (result.stdout + result.stderr).strip())
    return elapsed


def validate_capture_output(out_dir):
    errors = []
    for fid in fixture_ids():
        raw_path = out_dir / "raw" / f"{fid}.json"
        normalized_path = out_dir / "normalized" / f"{fid}.json"
        if not raw_path.exists():
            errors.append(f"missing raw record for fixture '{fid}' at {raw_path}")
        else:
            errors.extend(validate_raw_record(json.loads(raw_path.read_text())))
        if not normalized_path.exists():
            errors.append(f"missing normalized record for fixture '{fid}' at {normalized_path}")
        else:
            errors.extend(validate_normalized_record(json.loads(normalized_path.read_text())))
    return errors


def measure_candidate(name, meta):
    """Builds AC-4 evidence for one candidate. Never fabricates a number:
    unmeasurable fields become `{"blocked": "<prerequisite>", "reason": ...}`.
    """
    record = {
        "candidate": name,
        "package": {"name": meta["crate_name"], "version": None},
        "grammar_or_asset_identity": None,
        "provisioning": None,
        "offline_capture_command": " ".join(offline_capture_command(meta, EXTRACTION_ROOT / ".generated" / "candidates" / name)),
        "clean_build_seconds": None,
        "executable_size_bytes": None,
        "extraction_timing": None,
        "validation": None,
    }

    lockfile = meta["dir"] / "Cargo.lock"
    record["package"]["version"] = lockfile_package_version(lockfile, meta["crate_name"])

    if name == "tree-sitter-language-pack":
        record["grammar_or_asset_identity"] = (
            f"runtime-downloaded grammar cache, version-scoped directory {TSL_PACK_CACHE_DIR}"
        )
    else:
        record["grammar_or_asset_identity"] = "statically linked into the candidate binary; no runtime cache"

    status = provision_status(meta)
    if status is None:
        record["provisioning"] = {
            "blocked": "provision",
            "reason": (
                f"grammar cache missing or empty at {TSL_PACK_CACHE_DIR}; "
                "run `python3 benchmark/extraction/capture.py provision` first (network required)"
            ),
        }
        record["clean_build_seconds"], record["executable_size_bytes"] = _try_clean_build(meta, record)
        record["extraction_timing"] = {
            "blocked": "provision",
            "reason": record["provisioning"]["reason"],
        }
        record["validation"] = {
            "blocked": "provision",
            "reason": "capture cannot run before provisioning; no records were produced",
        }
        return record

    record["provisioning"] = status

    clean_seconds, exe_size = _try_clean_build(meta, record)
    record["clean_build_seconds"] = clean_seconds
    record["executable_size_bytes"] = exe_size
    if isinstance(clean_seconds, dict):
        # Build itself failed: capture and validation cannot proceed either.
        record["extraction_timing"] = {"blocked": "cargo build --release", "reason": clean_seconds["reason"]}
        record["validation"] = {"blocked": "cargo build --release", "reason": clean_seconds["reason"]}
        return record

    out_dir = EXTRACTION_ROOT / ".generated" / "candidates" / name
    samples = []
    capture_error = None
    for _ in range(CAPTURE_REPS):
        try:
            samples.append(run_offline_capture(meta, out_dir))
        except Blocked as exc:
            capture_error = exc
            break

    if capture_error is not None:
        record["extraction_timing"] = {"blocked": capture_error.prerequisite, "reason": capture_error.reason}
        record["validation"] = {"blocked": capture_error.prerequisite, "reason": capture_error.reason}
        return record

    record["extraction_timing"] = {
        "unit": "seconds",
        "samples": samples,
        "count": len(samples),
        "median": statistics.median(samples),
        "range": {"min": min(samples), "max": max(samples)},
    }

    validation_errors = validate_capture_output(out_dir)
    record["validation"] = (
        {"status": "ok", "fixtures_checked": len(fixture_ids())}
        if not validation_errors
        else {"status": "failed", "errors": validation_errors}
    )
    return record


def _try_clean_build(meta, record):
    try:
        return clean_build(meta)
    except Blocked as exc:
        return {"blocked": exc.prerequisite, "reason": exc.reason}, {
            "blocked": exc.prerequisite,
            "reason": exc.reason,
        }


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def cmd_provision(args):
    print("ast-grep-outline: not required (grammars are statically linked into the candidate binary).")

    meta = CANDIDATES["tree-sitter-language-pack"]
    manifest_path = meta["dir"] / "Cargo.toml"
    print(f"tree-sitter-language-pack: building release binary (network: crates.io if uncached)...")
    built = run(["cargo", "build", "--release", "--manifest-path", str(manifest_path)])
    if built.returncode != 0:
        print(built.stderr, file=sys.stderr)
        print("tree-sitter-language-pack: provision blocked: release build failed", file=sys.stderr)
        return 1

    print("tree-sitter-language-pack: warming runtime grammar cache (NETWORK)...")
    result = run([str(binary_path(meta)), "provision"])
    print(result.stdout, end="")
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        print("tree-sitter-language-pack: provision failed", file=sys.stderr)
        return 1
    print(f"tree-sitter-language-pack: provisioned. Cache: {TSL_PACK_CACHE_DIR}")
    return 0


def cmd_capture(args):
    if not ROOT_LOCKFILE.exists():
        print(f"root Cargo.lock not found at {ROOT_LOCKFILE}", file=sys.stderr)
        return 1

    lockfile_before = sha256_file(ROOT_LOCKFILE)

    results = {}
    for name, meta in CANDIDATES.items():
        print(f"== {name} ==")
        results[name] = measure_candidate(name, meta)

    lockfile_after = sha256_file(ROOT_LOCKFILE)
    lockfile_unchanged = lockfile_before == lockfile_after
    if not lockfile_unchanged:
        print(
            "ERROR: root Cargo.lock changed during candidate builds; "
            "candidate builds must not perturb root dependency resolution.",
            file=sys.stderr,
        )

    evidence = {
        "version": 1,
        "baseline_commit": baseline_commit(),
        "toolchain": toolchain_versions(),
        "host": platform.platform(),
        "fixture_hashes": fixture_hashes(),
        "root_cargo_lock_self_check": {
            "sha256_before": lockfile_before,
            "sha256_after": lockfile_after,
            "unchanged": lockfile_unchanged,
        },
        "provisioning_vs_capture_separation": (
            "`provision` is a distinct, network-requiring, run-once subcommand; "
            "`capture` here runs each candidate's offline capture subcommand under a "
            "poisoned http_proxy/https_proxy/all_proxy environment, so any live network "
            "access during measured capture fails loudly instead of succeeding silently."
        ),
        "candidates": results,
    }

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    evidence_path = EVIDENCE_DIR / "ac4-measurement.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=False) + "\n")
    print(f"wrote AC-4 evidence: {evidence_path}")

    exit_code = 0
    for name, record in results.items():
        validation = record.get("validation") or {}
        if isinstance(validation, dict) and validation.get("blocked"):
            print(f"{name}: BLOCKED at '{validation['blocked']}': {validation['reason']}")
        elif isinstance(validation, dict) and validation.get("status") == "failed":
            print(f"{name}: validation FAILED: {validation['errors']}")
            exit_code = 1
        else:
            timing = record["extraction_timing"]
            print(
                f"{name}: captured+validated. median={timing['median']:.4f}s "
                f"range=[{timing['range']['min']:.4f},{timing['range']['max']:.4f}]s "
                f"exe={record['executable_size_bytes']}B"
            )

    if not lockfile_unchanged:
        exit_code = 1
    return exit_code


def build_parser():
    parser = argparse.ArgumentParser(
        prog="capture.py",
        description=(
            "Provisioning + offline-capture orchestrator for the Tilth extraction "
            "reuse comparison candidates."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    provision_parser = subparsers.add_parser(
        "provision",
        help="NETWORK: warm the tree-sitter-language-pack runtime grammar cache.",
    )
    provision_parser.set_defaults(func=cmd_provision)

    capture_parser = subparsers.add_parser(
        "capture",
        help="OFFLINE: build, capture, validate, and measure both candidates (AC-4 evidence).",
    )
    capture_parser.set_defaults(func=cmd_capture)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
