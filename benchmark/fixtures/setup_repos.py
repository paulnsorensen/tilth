#!/usr/bin/env python3
"""Clone and pin real-world repositories for benchmarking."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Add parent directories to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import REPOS, REPOS_DIR


def setup_repo(repo_config) -> None:
    """Clone a repo and checkout the pinned commit."""
    repo_path = repo_config.path

    if repo_path.exists():
        # Verify correct commit
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        current_sha = result.stdout.strip()
        if current_sha == repo_config.commit_sha:
            print(f"  {repo_config.name}: already at {repo_config.commit_sha[:8]}")
            install_deps(repo_config)
            return

        print(f"  {repo_config.name}: at {current_sha[:8]}, need {repo_config.commit_sha[:8]}, re-cloning...")
        shutil.rmtree(repo_path)

    print(f"  {repo_config.name}: cloning from {repo_config.url}...")
    subprocess.run(
        ["git", "clone", "--no-checkout", repo_config.url, str(repo_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", repo_config.commit_sha],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    print(f"  {repo_config.name}: checked out {repo_config.commit_sha[:8]}")

    install_deps(repo_config)


def install_deps(repo_config) -> None:
    """Install per-repo dev dependencies needed by task test_commands.

    Only express needs this: `npx mocha` in a bare checkout hangs on an
    interactive install prompt, which times out every edit rep. Go (`go test`)
    and Python (`uv run pytest`) fetch their own dependencies.
    """
    if repo_config.name != "express":
        return
    if (repo_config.path / "node_modules").exists():
        return
    env = dict(os.environ)
    # A broken (nonexistent) SSL_CERT_FILE poisons node's CA store and fails
    # every registry fetch; drop it like uv does rather than fail the setup.
    cert = env.get("SSL_CERT_FILE")
    if cert and not Path(cert).exists():
        env.pop("SSL_CERT_FILE")
    print(f"  {repo_config.name}: npm install (dev deps for mocha)...")
    try:
        subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund"],
            cwd=str(repo_config.path),
            check=True,
            capture_output=True,
            env=env,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        print(stderr)
        raise
    print(f"  {repo_config.name}: dev deps installed")


def setup_all(repo_names: Optional[list[str]] = None) -> None:
    """Clone all (or specified) repos."""
    REPOS_DIR.mkdir(parents=True, exist_ok=True)

    targets = repo_names or list(REPOS.keys())
    for name in targets:
        if name not in REPOS:
            print(f"  WARNING: unknown repo '{name}', skipping")
            continue
        setup_repo(REPOS[name])


def main():
    parser = argparse.ArgumentParser(
        description="Clone and pin real-world repositories for benchmarking",
    )
    parser.add_argument(
        "--repos",
        default="all",
        help="Comma-separated repo names or 'all' (default: all)",
    )

    args = parser.parse_args()

    print("Setting up benchmark repositories...")

    if args.repos.lower() == "all":
        setup_all()
    else:
        names = [r.strip() for r in args.repos.split(",") if r.strip()]
        setup_all(names)

    print("Done.")


if __name__ == "__main__":
    main()
