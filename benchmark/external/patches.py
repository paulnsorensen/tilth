"""Unified-diff helpers: split by file, reverse, and build single-hunk tampered gold patches."""

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(,\d+)? \+(\d+)(,\d+)? @@(.*)$", re.DOTALL)
_DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
_DOC_STEMS = ("CHANGELOG", "CHANGES", "NEWS", "README", "AUTHORS")


@dataclass
class _Section:
    """One file's part of a patch: its header lines and its hunks (header line plus body)."""

    header: list[str]
    hunks: list[list[str]] = field(default_factory=list)

    @property
    def path(self) -> str:
        old = new = None
        for line in self.header:
            if line.startswith("--- "):
                old = line[4:].rstrip("\n")
            elif line.startswith("+++ "):
                new = line[4:].rstrip("\n")
        for candidate in (new, old):
            if candidate and candidate != "/dev/null":
                return candidate.split("/", 1)[1] if candidate[:2] in {"a/", "b/"} else candidate
        return self.header[0].rstrip("\n").split(" b/", 1)[-1]

    def text(self) -> str:
        return "".join(self.header) + "".join(line for hunk in self.hunks for line in hunk)


def _sections(patch: str) -> list[_Section]:
    sections: list[_Section] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            sections.append(_Section(header=[line]))
        elif not sections:
            continue
        elif line.startswith("@@"):
            sections[-1].hunks.append([line])
        elif sections[-1].hunks:
            sections[-1].hunks[-1].append(line)
        else:
            sections[-1].header.append(line)
    return sections


def split_files(patch: str) -> list[tuple[str, str]]:
    """Return ``(path, section text)`` for each file a patch touches, in patch order."""
    return [(section.path, section.text()) for section in _sections(patch)]


def touched_paths(patch: str) -> set[str]:
    """Every old and new path a patch names."""
    paths = set()
    for section in _sections(patch):
        for line in section.header:
            if line.startswith(("--- ", "+++ ")):
                target = line[4:].rstrip("\n")
                if target != "/dev/null":
                    paths.add(target.split("/", 1)[1] if target[:2] in {"a/", "b/"} else target)
    return paths


def _units(body: list[str]) -> list[list[str]]:
    """Group body lines so a ``\\ No newline`` marker stays with the line it annotates."""
    units: list[list[str]] = []
    for line in body:
        if line.startswith("\\") and units:
            units[-1].append(line)
        else:
            units.append([line])
    return units


def _reverse_body(body: list[str]) -> list[str]:
    swapped = [[{"+": "-", "-": "+"}.get(unit[0][:1], unit[0][:1]) + unit[0][1:], *unit[1:]]
               for unit in _units(body)]
    ordered: list[list[str]] = []
    run: list[list[str]] = []
    for unit in [*swapped, [" "]]:
        if unit[0][:1] in {"+", "-"}:
            run.append(unit)
            continue
        ordered += [u for u in run if u[0].startswith("-")] + [u for u in run if u[0].startswith("+")]
        run = []
        ordered.append(unit)
    return [line for unit in ordered[:-1] for line in unit]


def _swap_path(target: str, prefix: str) -> str:
    return target if target.startswith("/dev/null") else prefix + target[2:]


def _reverse_section(section: _Section) -> _Section:
    header: list[str] = []
    old = new = None
    for line in section.header:
        if line.startswith("new file mode "):
            header.append("deleted file mode " + line[len("new file mode "):])
        elif line.startswith("deleted file mode "):
            header.append("new file mode " + line[len("deleted file mode "):])
        elif line.startswith("index ") and ".." in line:
            ids, _, mode = line[len("index "):].partition(" ")
            before, _, after = ids.rstrip("\n").partition("..")
            header.append(f"index {after}..{before}" + (f" {mode}" if mode else "\n"))
        elif line.startswith("--- "):
            old = line[4:]
        elif line.startswith("+++ "):
            new = line[4:]
            header += ["--- " + _swap_path(new, "a/"), "+++ " + _swap_path(old or "/dev/null\n", "b/")]
        else:
            header.append(line)
    hunks = []
    for hunk in section.hunks:
        match = _HUNK_HEADER.match(hunk[0])
        head = (f"@@ -{match[3]}{match[4] or ''} +{match[1]}{match[2] or ''} @@{match[5]}" if match else hunk[0])
        hunks.append([head, *_reverse_body(hunk[1:])])
    return _Section(header=header, hunks=hunks)


def reverse(patch: str) -> str:
    """The patch that undoes ``patch``."""
    return "".join(_reverse_section(section).text() for section in _sections(patch))


def _is_doc(path: str) -> bool:
    name = PurePosixPath(path)
    return name.suffix.lower() in _DOC_SUFFIXES or name.name.upper().startswith(_DOC_STEMS)


def _without_added_lines(hunk: list[str]) -> list[str] | None:
    """The hunk minus its added lines, or None when nothing would change."""
    units = [unit for unit in _units(hunk[1:]) if not unit[0].startswith("+")]
    if not any(unit[0].startswith("-") for unit in units):
        return None
    match = _HUNK_HEADER.match(hunk[0])
    new_count = sum(1 for unit in units if unit[0].startswith(" "))
    head = f"@@ -{match[1]}{match[2] or ''} +{match[3]},{new_count} @@{match[5]}" if match else hunk[0]
    return [head, *(line for unit in units for line in unit)]


def _changed_lines(hunk: list[str]) -> int:
    return sum(1 for line in hunk[1:] if line[:1] in {"+", "-"})


def _replace_hunk(sections: list[_Section], index: int, position: int, hunk: list[str] | None) -> str:
    """The patch text with one hunk replaced, or removed when ``hunk`` is None."""
    variant = [_Section(section.header, list(section.hunks)) for section in sections]
    if hunk is None:
        del variant[index].hunks[position]
    else:
        variant[index].hunks[position] = hunk
    return "".join(section.text() for section in variant if section.hunks)


def hunk_removals(patch: str, max_hunks: int) -> list[tuple[int, str, str]]:
    """Single-hunk tampered variants of ``patch``: ``(hunk index, path, patch without that hunk)``.

    The index counts hunks in patch order. Source hunks are tried (documentation
    hunks only when there is no other), largest first, at most ``max_hunks`` of
    them. A patch with one such hunk yields it minus its added lines instead,
    since removing it would only repeat the empty patch.
    """
    sections = _sections(patch)
    hunks = [(index, position) for index, section in enumerate(sections)
             for position in range(len(section.hunks))]
    number = {hunk: count for count, hunk in enumerate(hunks)}
    source = [hunk for hunk in hunks if not _is_doc(sections[hunk[0]].path)] or hunks
    if len(source) == 1:
        [(index, position)] = source
        stripped = _without_added_lines(sections[index].hunks[position])
        return [(number[source[0]], sections[index].path, _replace_hunk(sections, index, position, stripped))]
    ranked = sorted(source, key=lambda hunk: (-_changed_lines(sections[hunk[0]].hunks[hunk[1]]), number[hunk]))
    return [(number[(index, position)], sections[index].path, _replace_hunk(sections, index, position, None))
            for index, position in ranked[:max_hunks]]
