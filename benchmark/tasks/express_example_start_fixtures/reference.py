"""Trusted reference transformation for the pinned Express fixture."""

import re
from pathlib import Path


START_JS = """'use strict'

/**
 * Shared entry-point helpers for the example apps.
 */

/**
 * True when `entry` is the module that node started.
 */

exports.isMain = function isMain (entry) {
  return require.main === entry
}

/**
 * True when examples should keep their console output quiet.
 */

exports.quiet = function quiet () {
  return process.env.NODE_ENV === 'test'
}

/**
 * Listen only when `entry` is the main module.
 */

/* istanbul ignore next */
exports.start = function start (entry, app, port) {
  if (!exports.isMain(entry)) return undefined

  var requested = process.env.PORT
  var listenPort = /^\\d+$/.test(requested || '')
    ? Number(requested)
    : (port === undefined ? 3000 : port)
  var server = app.listen(listenPort)

  server.on('listening', function () {
    console.log('Express started on port ' + server.address().port)
  })

  return server
}
"""

BLOCK = re.compile(
    r"(?P<indent>[ \t]*)(?:/\* istanbul ignore next \*/\n(?P=indent))?if \(!module\.parent\) \{\n"
    r"[ \t]*app\.listen\(3000\);\n"
    r"[ \t]*console\.log\('Express started on port 3000'\);\n"
    r"[ \t]*\}"
)
TRY_HINTS = (
    "app.listen(3000);\n"
    "console.log('listening on port 3000');\n"
    "console.log('try:');\n"
    "console.log('  GET /hello.txt');\n"
    "console.log('  GET /js/app.js');\n"
    "console.log('  GET /css/style.css');\n"
)
TRY_HINTS_NEW = (
    "start(module, app);\n\n"
    "if (isMain(module)) {\n"
    "  console.log('try:');\n"
    "  console.log('  GET /hello.txt');\n"
    "  console.log('  GET /js/app.js');\n"
    "  console.log('  GET /css/style.css');\n"
    "}\n"
)
QUIET_EDITS = {
    "examples/cookies/index.js": ("process.env.NODE_ENV !== 'test'", "!quiet()"),
    "examples/error-pages/index.js": ("process.env.NODE_ENV === 'test'", "quiet()"),
    "examples/route-map/index.js": ("process.env.NODE_ENV !== 'test'", "!quiet()"),
}
REQUIRE_EXPRESS = re.compile(r"^.*require\('\.\./\.\./?(?:lib/express)?'\).*$", re.M)


def example_files(target: Path) -> list[Path]:
    return sorted(p for p in (target / "examples").rglob("*.js") if "node_modules" not in p.parts)


def migrate(relative: str, text: str) -> str:
    text, blocks = BLOCK.subn(lambda m: m.group("indent") + "start(module, app);", text)
    text = text.replace("app.listen(3000);\nconsole.log('Express app started on port 3000');\n", "start(module, app);\n")
    text = text.replace(TRY_HINTS, TRY_HINTS_NEW)
    text = text.replace("!module.parent", "isMain(module)")
    if relative in QUIET_EDITS:
        old, new = QUIET_EDITS[relative]
        assert text.count(old) == 1, relative
        text = text.replace(old, new)
    names = [name for name in ("start", "isMain", "quiet") if re.search(rf"\b{name}\(", text)]
    if not names:
        return text
    match = REQUIRE_EXPRESS.search(text)
    assert match, relative
    helper = "../start"
    line = f"var {{ {', '.join(names)} }} = require('{helper}');"
    return text[: match.end()] + "\n" + line + text[match.end():]


def apply_reference(target: Path) -> None:
    (target / "examples" / "start.js").write_text(START_JS)
    for path in example_files(target):
        relative = path.relative_to(target).as_posix()
        if relative == "examples/start.js" or "/public/" in relative:
            continue
        before = path.read_text()
        after = migrate(relative, before)
        if after != before:
            path.write_text(after)
