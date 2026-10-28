"""Just enough Rust lexing for the applier: test-``cfg`` item spans and the MCP instruction byte lock."""

import re
from pathlib import PurePosixPath

_CFG_ATTR = re.compile(r"#\s*(!?)\s*\[\s*(?:cfg|cfg_attr)\s*\(")
# Predicates that differ between the gate (debug, test) and the benchmark (release) builds.
_TEST_TOKEN = re.compile(r"\b(?:test|debug_assertions)\b")
_INCLUDE = re.compile(r"\b(include(?:_str|_bytes)?)\s*!\s*([(\[{])")
_USE = re.compile(r"\buse\b([^;]*);")
_INCLUDE_NAME = re.compile(r"\binclude(?:_str|_bytes)?\b")
_BARE_MACRO = re.compile(r"\b(?:include(?:_str|_bytes)?(?!\w)(?!\s*!)|cfg(?!\w)(?!\s*[!(]))")
_CFG_MACRO = re.compile(r"\bcfg\s*!\s*([(\[{])")
_PATH_ATTR = re.compile(r"#\s*!?\s*\[\s*path\s*=")
_MODULE_DECL = re.compile(r"\bmod\s+(\w+)\s*$")
_IDENT = re.compile(r"[A-Za-z0-9_]")
_FN = re.compile(r"\bfn\s+(\w+)")
_MOD_BLOCK = re.compile(r"\bmod\s+(\w+)\s*\{")
_MODIFIERS = re.compile(r"(?:\b(?:pub(?:\s*\([^)]*\))?|async|const|unsafe)\s+)*$")
_ATTRIBUTES = re.compile(r"(?:#\s*\[(?:[^\[\]]|\[[^\]]*\])*\]\s*)+$")
_TEST_ATTR = re.compile(r"\btest\b")
_FAILED_NAME = re.compile(r"\s{4}(\S+)")
_ATTRIBUTE = re.compile(r"#\s*\[((?:[^\[\]]|\[[^\]]*\])*)\]")
_TEST_ATTRIBUTE = re.compile(r"(?:\w+::)*test(?:\(.*\))?", re.S)
_GUARD_ATTRIBUTE = re.compile(r"(?:ignore|should_panic|cfg|cfg_attr)\b")
_ASSERTION = re.compile(r"\b(?:assert|assert_eq|assert_ne|panic)\s*!")
_ASSERT_CALL = re.compile(r"\b(assert|assert_eq|assert_ne)\s*!\s*([(\[{])")
_TRY = re.compile(r"[\w)\]}]\s*\?")
_LITERAL_ONLY = re.compile(r'"(?:[^"\\]|\\.)*"|\b\d[\w.]*|\btrue\b|\bfalse\b|[=!<>&|()\s-]')
_NESTED_ITEM = re.compile(r"\bfn\s+(?=[A-Za-z_])|\b(?:struct|enum|impl|trait|mod|use|macro_rules|const|type)\b"
                         r"|(?<!')\bstatic\b|#\s*\[\s*macro_export")
_WORD = re.compile(r"[A-Za-z_]\w*")
_RAW = re.compile(r'r(#*)"')
_STRING = r'("(?:[^"\\]|\\.)*")'
_COUNT = re.compile(r"SERVER_INSTRUCTIONS\.len\(\),\s*(\d+)")
_STARTS = re.compile(r"SERVER_INSTRUCTIONS\.starts_with\(\s*" + _STRING, re.S)
_ENDS = re.compile(r"SERVER_INSTRUCTIONS\.ends_with\(\s*" + _STRING, re.S)
BYTE_LOCK_FN = "fn server_instructions_byte_lock"


def _blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def _code_mask(text: str) -> str:
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
        elif char == "r" and (raw := _RAW.match(text, i)) and (
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


def _cfg_test_items(text: str) -> list[tuple[int, int, str]]:
    """``(first_line, last_line, head)`` of each item under a ``cfg`` or ``cfg_attr`` attribute naming ``test``
    or ``debug_assertions``.

    That covers ``cfg(test)``, ``cfg(any(.., test))``, ``cfg(all(test, ..))``, ``cfg(not(test))``, and
    ``cfg_attr(test, ..)``; the span starts at the attribute, so the attribute itself is part of the item.
    ``head`` is the item's code before ``{`` or ``;``. An inner ``#![cfg(..test..)]`` spans the whole file.
    """
    mask = _code_mask(text)
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
    return [(first, last) for first, last, _head in _cfg_test_items(text)]


def test_module_files(path: str, text: str) -> set[str]:
    """Paths of the files ``path`` declares as ``#[cfg(test)] mod name;``."""
    source = PurePosixPath(path)
    base = source.parent if source.name in {"mod.rs", "lib.rs", "main.rs"} else source.parent / source.stem
    files = set()
    for _first, _last, head in _cfg_test_items(text):
        if declared := _MODULE_DECL.search(head.split("]")[-1]):
            files |= {str(base / f"{declared[1]}.rs"), str(base / declared[1] / "mod.rs")}
    return files


def module_path(path: str) -> list[str]:
    """The module path of a ``src/`` file as cargo prints it in a test name: ``src/search/edit.rs`` is search::edit."""
    source = PurePosixPath(path)
    parts = list(source.parent.parts)
    if source.name not in {"mod.rs", "lib.rs", "main.rs"}:
        parts.append(source.stem)
    return parts[1:] if parts and parts[0] == "src" else parts


def test_functions(path: str, text: str) -> list[tuple[str, int, int]]:
    """``(test name, first_line, last_line)`` of each ``#[test]`` fn, named as cargo prints it (``a::b::name``).

    The span starts at the fn's first attribute and ends at its closing brace.
    The name joins the file's module path, the inline ``mod`` blocks around the fn, and the fn name.
    """
    mask = _code_mask(text)
    mods = [(match[1], match.start(), _match_brace(mask, match.end() - 1)) for match in _MOD_BLOCK.finditer(mask)]
    found = []
    for match in _FN.finditer(mask):
        head = mask[:match.start()]
        modifiers = _MODIFIERS.search(head)
        head = head[:modifiers.start()]
        attributes = _ATTRIBUTES.search(head)
        if attributes is None or not _TEST_ATTR.search(attributes[0]):
            continue
        terminator = re.compile(r"[{;]").search(mask, match.end())
        if terminator is None or terminator[0] == ";":
            continue
        end = _match_brace(mask, terminator.start())
        inline = [name for name, start, close in mods if start < match.start() < close]
        name = "::".join([*module_path(path), *inline, match[1]])
        found.append((name, _line(text, attributes.start()), _line(text, end)))
    return found


def failing_tests(output: str) -> list[str]:
    """The test names in cargo's ``failures:`` lists, in order and without repeats.

    The list is the indented names that follow a ``failures:`` line, up to a blank line or the ``test result:``
    line. A ``failures:`` line that opens the ``---- name stdout ----`` blocks has no names after it.
    A doc test prints a name with spaces (``src/x.rs - foo (line 3)``); it is skipped and the list goes on.
    """
    names: list[str] = []
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "failures:":
            continue
        for follower in lines[index + 1:]:
            if not follower.strip() or follower.startswith("test result:"):
                break
            listed = _FAILED_NAME.fullmatch(follower)
            if listed is not None and listed[1] not in names:
                names.append(listed[1])
    return names


def test_weakening(old: str, new: str) -> str | None:
    """Why the edited test fn ``new`` is weaker than ``old``, or None. Both texts run from the first attribute
    to the closing brace.

    ``new`` must carry exactly one test attribute (``#[test]``, ``#[tokio::test]``, ``#[<path>::test(..)]``), and
    no ``ignore``, ``should_panic``, ``cfg``, or ``cfg_attr`` attribute that ``old`` lacked. When ``old``'s body
    has an ``assert!``, ``assert_eq!``, ``assert_ne!``, ``panic!``, or ``?``, so must ``new``'s. The body check is
    deliberately coarse: it catches a gutted body, not one that moves its asserts into a helper.
    """
    before, after = _fn_parts(old), _fn_parts(new)
    if after is None:
        return "the test fn cannot be read"
    attributes, signature, body, _raw = after
    if sum(bool(_TEST_ATTRIBUTE.fullmatch(attribute)) for attribute in attributes) != 1:
        return "it must carry exactly one test attribute"
    kept = list(before[0]) if before else []
    for attribute in attributes:
        if attribute in kept:
            kept.remove(attribute)
        elif _GUARD_ATTRIBUTE.match(attribute):
            return f"it adds #[{attribute}], which can disable or invert the test"
    if before and _asserts(before[1], before[2]) and not _asserts(signature, body):
        return "its body no longer asserts anything"
    return None


def _fn_parts(text: str) -> tuple[list[str], str, str, str] | None:
    """``(attributes, signature, body, raw body)`` of the fn in ``text``; the first two and the body are code-masked."""
    mask = _code_mask(text)
    found = _FN.search(mask)
    terminator = mask.find("{", found.end()) if found else -1
    if found is None or terminator < 0:
        return None
    head = mask[:found.start()]
    head = head[:_MODIFIERS.search(head).start()]
    attributes = [re.sub(r"\s+", "", match[1]) for match in _ATTRIBUTE.finditer(head)]
    return attributes, mask[found.start():terminator], mask[terminator:], text[terminator:]


def _asserts(signature: str, body: str) -> bool:
    """True when ``body`` holds an assert macro, or a postfix ``?`` in a fn that returns ``Result``."""
    return bool(_ASSERTION.search(body) or ("Result" in signature and _TRY.search(body)))


def has_test_attribute(text: str) -> bool:
    """True when the fn in ``text`` carries a test attribute: ``#[test]``, ``#[tokio::test(..)]``, ``#[<path>::test]``."""
    parts = _fn_parts(text)
    return parts is not None and any(_TEST_ATTRIBUTE.fullmatch(attribute) for attribute in parts[0])


def identifiers(text: str) -> set[str]:
    """Every identifier in the code of ``text``, comments and strings left out."""
    return set(_WORD.findall(_code_mask(text)))


def _call_arguments(raw: str, mask: str, open_index: int) -> list[str]:
    close = _match_brace(mask, open_index, {"(": "()", "[": "[]", "{": "{}"}[mask[open_index]])
    arguments, depth, start = [], 0, open_index + 1
    for index in range(open_index + 1, close):
        if mask[index] in "([{":
            depth += 1
        elif mask[index] in ")]}":
            depth -= 1
        elif mask[index] == "," and depth == 0:
            arguments.append(raw[start:index])
            start = index + 1
    arguments.append(raw[start:close])
    return [argument.strip() for argument in arguments if argument.strip()]


def _trivial_assert(name: str, arguments: list[str]) -> bool:
    def constant(argument: str) -> bool:
        return not _LITERAL_ONLY.sub("", argument)

    if name == "assert":
        return not arguments or constant(arguments[0])
    if len(arguments) < 2:
        return True
    same = re.sub(r"\s+", "", arguments[0]) == re.sub(r"\s+", "", arguments[1])
    return same or (constant(arguments[0]) and constant(arguments[1]))


_ASSERTING_STUB = "#[test]\nfn stub() {\n    assert!(true);\n}"


def new_test_problem(new: str) -> str | None:
    """Why the new test fn ``new`` is not a real test, or None. The text runs from the first attribute to the
    closing brace.

    It holds the same checks as ``test_weakening`` against a plain asserting test: exactly one test attribute, no
    ``ignore``, ``should_panic``, ``cfg``, or ``cfg_attr``, and a body that asserts something. A postfix ``?``
    counts only in a fn that returns ``Result``. A body whose assert macros compare only literals, or two identical
    arguments, asserts nothing. The body may not declare an item (``fn``, ``struct``, ``enum``, ``impl``, ``trait``,
    ``mod``, ``use``, ``macro_rules!``, ``static``, ``const``, ``type``, ``#[macro_export]``): its names could leak
    into the module and change what the existing tests resolve to.
    """
    if problem := test_weakening(_ASSERTING_STUB, new):
        return problem
    _attributes, signature, body, raw = _fn_parts(new)
    if nested := _NESTED_ITEM.search(body, 1):
        item = " ".join(nested[0].split())
        return f"its body declares an item ({item}), which can shadow a name"
    calls = [(match[1], _call_arguments(raw, body, match.end() - 1)) for match in _ASSERT_CALL.finditer(body)]
    if calls and all(_trivial_assert(name, arguments) for name, arguments in calls) \
            and not re.search(r"\bpanic\s*!", body) and not ("Result" in signature and _TRY.search(body)):
        return "its asserts compare only literals or identical values"
    return None


def include_invocations(text: str) -> list[tuple[int, int, str, str]]:
    """``(first_line, last_line, macro, argument)`` of each ``include!``, ``include_str!``, or ``include_bytes!``.

    ``argument`` is the source text between the macro's delimiters, stripped.
    """
    mask = _code_mask(text)
    found = []
    for match in _INCLUDE.finditer(mask):
        close = _match_brace(mask, match.end() - 1, {"(": "()", "[": "[]", "{": "{}"}[match[2]])
        found.append((_line(text, match.start()), _line(text, close), match[1], text[match.end():close].strip()))
    return found


def include_aliases(text: str) -> list[tuple[int, int]]:
    """``(first_line, last_line)`` of each ``use`` that names ``include``, ``include_str``, or ``include_bytes``.

    Such a ``use`` can rename the macro and hide it from ``include_invocations``, so callers refuse it.
    It covers any path form and grouped imports.
    """
    mask = _code_mask(text)
    return [(_line(text, match.start()), _line(text, match.end()))
            for match in _USE.finditer(mask) if _INCLUDE_NAME.search(match[1])]


def bare_macro_names(text: str) -> list[int]:
    """Lines with ``include``, ``include_str``, ``include_bytes``, or ``cfg`` used as a plain name.

    A name not followed by ``!`` can reach a macro through a ``macro_rules!`` parameter (``$m!(..)``) and hide
    it from ``include_invocations`` and ``cfg_test_macros``. ``cfg`` before ``(`` is an attribute, which
    ``cfg_test_spans`` already guards.
    """
    mask = _code_mask(text)
    return [_line(text, match.start()) for match in _BARE_MACRO.finditer(mask)]


def cfg_test_macros(text: str) -> list[tuple[int, int]]:
    """``(first_line, last_line)`` of each ``cfg!(..)`` call that names ``test`` or ``debug_assertions``."""
    mask = _code_mask(text)
    found = []
    for match in _CFG_MACRO.finditer(mask):
        close = _match_brace(mask, match.end() - 1, {"(": "()", "[": "[]", "{": "{}"}[match[1]])
        if _TEST_TOKEN.search(mask, match.end(), close):
            found.append((_line(text, match.start()), _line(text, close)))
    return found


def path_attributes(text: str) -> list[int]:
    """The first line of each ``#[path = ..]`` or ``#![path = ..]`` attribute, which can point a module outside the tree."""
    mask = _code_mask(text)
    return [_line(text, match.start()) for match in _PATH_ATTR.finditer(mask)]


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
    mask = _code_mask(text)
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
    A whole-line exemption once let a candidate append ``return;`` after the literal and skip later assertions.
    """
    try:
        count, starts, ends = _byte_lock_matches(text)
    except ValueError:
        return text
    spans = [(count.span(1), "0"), (starts.span(1), '""'), (ends.span(1), '""')]
    for (begin, finish), placeholder in sorted(spans, reverse=True):
        text = text[:begin] + placeholder + text[finish:]
    return text


def _rust_literal(value: str) -> str:
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
        (starts.span(1), _rust_literal("\n".join(instructions.split("\n")[:lead_lines]))),
        (ends.span(1), _rust_literal(instructions.rsplit("\n", 1)[-1])),
    ]
    for (begin, finish), literal in sorted(replacements, reverse=True):
        text = text[:begin] + literal + text[finish:]
    return text
