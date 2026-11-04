"""Refuse a paid run when systemd-tmpfiles can age out the benchmark tree under /tmp.

Stock Ubuntu ships the rule ``q /tmp 1777 root root 10d``. ``systemd-tmpfiles-clean`` then deletes
files older than 10 days under /tmp but keeps their directories. Cargo build-script outputs and
repository clones under /tmp/tilth_bench lose files while cargo fingerprints stay, so a grader
build fails and the cell records a wrong answer. An ``x`` line excludes a path and its contents
from age cleanup (tmpfiles.d(5)). The check reads the host config and changes no run key.
"""

import fnmatch
import shlex
import shutil
import subprocess
from pathlib import PurePosixPath


class TmpAgingError(RuntimeError):
    """systemd-tmpfiles can delete files under the benchmark tree."""


def excludes(config_text: str, path: PurePosixPath) -> bool:
    """Return True when an ``x`` line in tmpfiles.d ``config_text`` excludes ``path`` from age cleanup.

    An ``x`` line excludes its path and the contents below it, so a pattern that matches ``path``
    or an ancestor covers ``path``. ``X`` excludes a directory but not its contents, so it does not
    count. An ``x!`` line applies only at boot, so it does not count. Patterns match one path
    component at a time, as fnmatch(3) does with FNM_PATHNAME.

    An ``x`` line counts only when every other active line for the same path is also ``x``.
    systemd-tmpfiles applies the first line for a path and drops a later conflicting line
    (tmpfiles.d(5)), and a same-path line with an age cleans the contents even next to an ``x`` line.
    """
    parts = PurePosixPath(path).parts
    types: dict[tuple[str, ...], set[str]] = {}
    for line in config_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            fields = shlex.split(line)
        except ValueError:
            continue
        if len(fields) < 2 or "!" in fields[0]:
            continue
        types.setdefault(PurePosixPath(fields[1]).parts, set()).add(fields[0][:1])
    return any(
        kinds == {"x"} and len(pattern) <= len(parts) and all(map(fnmatch.fnmatchcase, parts, pattern))
        for pattern, kinds in types.items()
    )


def _cat_config() -> str | None:
    """Return the merged tmpfiles.d config, or None when the host has no systemd-tmpfiles."""
    binary = shutil.which("systemd-tmpfiles")
    if binary is None:
        return None
    try:
        result = subprocess.run([binary, "--cat-config"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TmpAgingError(f"could not run {binary} --cat-config: {error}") from error
    if result.returncode != 0:
        raise TmpAgingError(
            f"{binary} --cat-config exited {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout


def require_exclusion(bench_root: PurePosixPath) -> None:
    """Raise TmpAgingError when systemd-tmpfiles can age out files under ``bench_root``.

    Skip the check when ``bench_root`` is not under /tmp or the host has no systemd-tmpfiles.
    """
    root = PurePosixPath(bench_root)
    if PurePosixPath("/tmp") not in root.parents:
        return
    config_text = _cat_config()
    if config_text is None or excludes(config_text, root):
        return
    raise TmpAgingError(
        f"systemd-tmpfiles can delete files older than its /tmp age limit under {root}, "
        f"which breaks cached builds and repo clones; no tmpfiles.d 'x' rule covers it "
        f"(an 'x' rule does not count when a rule of another type names the same path). "
        f"Install the exclusion once: echo 'x {root}' | sudo tee /etc/tmpfiles.d/tilth-bench.conf"
    )
