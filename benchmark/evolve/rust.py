"""Just enough Rust lexing for the applier: ``#[cfg(test)]`` item spans and the MCP instruction byte lock."""

import re
from pathlib import PurePosixPath

_CFG_TEST = re.compile(r"#\[\s*cfg\s*\(\s*test\s*\)\s*\]")
_MODULE_DECL = re.compile(r"\bmod\s+(\w+)\s*$")
_IDENT = re.compile(r"[A-Za-z0-9_]")
_STRING = r'("(?:[^"\\]|\\.)*")'
_COUNT = re.compile(r"SERVER_INSTRUCTIONS\.len\(\),\s*(\d+)")
_STARTS = re.compile(r"SERVER_INSTRUCTIONS\.starts_with\(\s*" + _STRING, re.S)
_ENDS = re.compile(r"SERVER_INSTRUCTIONS\.ends_with\(\s*" + _STRING, re.S)
BYTE_LOCK_FN = "fn server_instructions_byte_lock"


def _blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def code_mask(text: str) -> str:
    """``text`` with comments, strings, and char literals blanked; newlines and code kept in place."""
    out: list[str] = []
    i, n = 0, len(text)

    def ident_before(index: int) -> bool:
        return index > 0 and bool(_IDENT.match(text[index - 1]))

    while i < n:
        char = text[i]
        if text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
        elif text.startswith("/*", i):
            depth, end = 1, i + 2
            while end < n and depth:
                if text.startswith("/*", end):
                    depth, end = depth + 1, end + 2
                elif text.startswith("*/", end):
                    depth, end = depth - 1, end + 2
                else:
                    end += 1
        elif char == "r" and (raw := re.match(r'r(#*)"', text[i:])) and (
                not ident_before(i) or (text[i - 1] == "b" and not ident_before(i - 1))):
            closing = '"' + raw[1]
            end = text.find(closing, i + len(raw[0]))
            end = n if end < 0 else end + len(closing)
        elif char == '"':
            end = i + 1
            while end < n and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            end += 1
        elif char == "'" and i + 1 < n and text[i + 1] == "\\":
            end = i + 3
            while end < n and text[end] != "'":
                end += 1
            end += 1
        elif char == "'" and i + 2 < n and text[i + 2] == "'":
            end = i + 3
        else:
            out.append(char)
            i += 1
            continue
        out.append(_blank(text[i:end]))
        i = end
    return "".join(out)


def _match_brace(mask: str, open_index: int) -> int:
    depth = 0
    for index in range(open_index, len(mask)):
        if mask[index] == "{":
            depth += 1
        elif mask[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return len(mask) - 1


def _line(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def cfg_test_items(text: str) -> list[tuple[int, int, str]]:
    """``(first_line, last_line, head)`` of each ``#[cfg(test)]`` item; ``head`` is its code before ``{`` or ``;``."""
    mask = code_mask(text)
    items = []
    for match in _CFG_TEST.finditer(mask):
        terminator = re.compile(r"[{;]").search(mask, match.end())
        if terminator is None:
            continue
        end = terminator.start() if terminator[0] == ";" else _match_brace(mask, terminator.start())
        items.append((_line(text, match.start()), _line(text, end), mask[match.end():terminator.start()].strip()))
    return items


def cfg_test_spans(text: str) -> list[tuple[int, int]]:
    return [(first, last) for first, last, _head in cfg_test_items(text)]


def test_module_files(path: str, text: str) -> set[str]:
    """Paths of the files ``path`` declares as ``#[cfg(test)] mod name;``."""
    source = PurePosixPath(path)
    base = source.parent if source.name in {"mod.rs", "lib.rs", "main.rs"} else source.parent / source.stem
    files = set()
    for _first, _last, head in cfg_test_items(text):
        if declared := _MODULE_DECL.search(head.split("]")[-1]):
            files |= {str(base / f"{declared[1]}.rs"), str(base / declared[1] / "mod.rs")}
    return files


def declaring_files(path: str) -> list[str]:
    """Files that could declare ``path`` as a module."""
    source = PurePosixPath(path)
    module_dir = source.parent.parent if source.name == "mod.rs" else source.parent
    if str(module_dir) == "src" or module_dir == PurePosixPath("."):
        return ["src/lib.rs", "src/main.rs"]
    return [str(module_dir / "mod.rs"), f"{module_dir}.rs"]


def _byte_lock_matches(text: str) -> list[re.Match]:
    mask = code_mask(text)
    start = mask.find(BYTE_LOCK_FN)
    if start < 0:
        raise ValueError(f"{BYTE_LOCK_FN} not found")
    end = _match_brace(mask, mask.index("{", start))
    matches = [pattern.search(text, start, end) for pattern in (_COUNT, _STARTS, _ENDS)]
    if not all(matches):
        raise ValueError(f"{BYTE_LOCK_FN} lacks its byte-count, starts_with, or ends_with literal")
    return matches


def byte_lock_lines(text: str) -> set[int]:
    """Lines holding the three byte-lock literals the applier rewrites; empty when there is no byte lock."""
    try:
        matches = _byte_lock_matches(text)
    except ValueError:
        return set()
    return {line for match in matches for line in range(_line(text, match.start(1)), _line(text, match.end(1)) + 1)}


def rust_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


def rewrite_byte_lock(text: str, instructions: str) -> str:
    """Point the byte-count, ``starts_with``, and ``ends_with`` literals at ``instructions``.

    ``starts_with`` keeps as many leading lines as its current literal spans.
    """
    count, starts, ends = _byte_lock_matches(text)
    escapes = re.findall(r"\\(.)", starts[1][1:-1])
    lead_lines = escapes.count("n") + 1
    replacements = [
        (count.span(1), str(len(instructions.encode("utf-8")))),
        (starts.span(1), rust_literal("\n".join(instructions.split("\n")[:lead_lines]))),
        (ends.span(1), rust_literal(instructions.rsplit("\n", 1)[-1])),
    ]
    for (begin, finish), literal in sorted(replacements, reverse=True):
        text = text[:begin] + literal + text[finish:]
    return text
