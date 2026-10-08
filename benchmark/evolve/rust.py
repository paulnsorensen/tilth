"""Just enough Rust lexing for the applier: test-``cfg`` item spans and the MCP instruction byte lock."""

import re
from pathlib import PurePosixPath

_CFG_ATTR = re.compile(r"#(!?)\[\s*(?:cfg|cfg_attr)\s*\(")
_TEST_TOKEN = re.compile(r"\btest\b")
_INCLUDE = re.compile(r"\b(include(?:_str|_bytes)?)!\s*([(\[{])")
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


def _match_brace(mask: str, open_index: int, pair: str = "{}") -> int:
    depth = 0
    for index in range(open_index, len(mask)):
        if mask[index] == pair[0]:
            depth += 1
        elif mask[index] == pair[1]:
            depth -= 1
            if depth == 0:
                return index
    return len(mask) - 1


def _line(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def cfg_test_items(text: str) -> list[tuple[int, int, str]]:
    """``(first_line, last_line, head)`` of each item under a ``cfg`` or ``cfg_attr`` attribute naming ``test``.

    That covers ``cfg(test)``, ``cfg(any(.., test))``, ``cfg(all(test, ..))``, ``cfg(not(test))``, and
    ``cfg_attr(test, ..)``; the span starts at the attribute, so the attribute itself is part of the item.
    ``head`` is the item's code before ``{`` or ``;``. An inner ``#![cfg(..test..)]`` spans the whole file.
    """
    mask = code_mask(text)
    items = []
    for match in _CFG_ATTR.finditer(mask):
        close = _match_brace(mask, mask.index("[", match.start()), "[]")
        if not _TEST_TOKEN.search(mask, match.end(), close):
            continue
        if match[1]:
            items.append((_line(text, match.start()), _line(text, max(len(text) - 1, 0)), ""))
            continue
        terminator = re.compile(r"[{;]").search(mask, close + 1)
        if terminator is None:
            continue
        end = terminator.start() if terminator[0] == ";" else _match_brace(mask, terminator.start())
        items.append((_line(text, match.start()), _line(text, end), mask[close + 1:terminator.start()].strip()))
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


def include_invocations(text: str) -> list[tuple[int, int, str, str]]:
    """``(first_line, last_line, macro, argument)`` of each ``include!``, ``include_str!``, or ``include_bytes!``.

    ``argument`` is the source text between the macro's delimiters, stripped.
    """
    mask = code_mask(text)
    found = []
    for match in _INCLUDE.finditer(mask):
        close = _match_brace(mask, match.end() - 1, {"(": "()", "[": "[]", "{": "{}"}[match[2]])
        found.append((_line(text, match.start()), _line(text, close), match[1], text[match.end():close].strip()))
    return found


def string_literal(source: str) -> str | None:
    """The value of a plain or raw Rust string literal that is the whole of ``source``; None otherwise."""
    if raw := re.fullmatch(r'r(#*)"(.*)"\1', source, re.S):
        return raw[2]
    if plain := re.fullmatch(r'"((?:[^"\\]|\\.)*)"', source, re.S):
        return re.sub(r"\\(.)", lambda escape: {"n": "\n", "t": "\t", "r": "\r"}.get(escape[1], escape[1]),
                      plain[1])
    return None


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


def blank_byte_lock(text: str) -> str:
    """``text`` with only the three byte-lock literals the applier rewrites replaced by fixed placeholders.

    Everything else, including the rest of each literal's line, is kept byte for byte;
    ``text`` comes back unchanged when it has no byte lock.
    """
    try:
        count, starts, ends = _byte_lock_matches(text)
    except ValueError:
        return text
    spans = [(count.span(1), "0"), (starts.span(1), '""'), (ends.span(1), '""')]
    for (begin, finish), placeholder in sorted(spans, reverse=True):
        text = text[:begin] + placeholder + text[finish:]
    return text


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
