"""Trusted reference transformation for the pinned FastAPI fixture."""

import re
from pathlib import Path

from ..fastapi_security_migration_tasks import _restore_tests

BASE_ADDITIONS = '''import warnings

from fastapi.openapi.models import SecurityBase as SecurityBaseModel


class SecurityBase:
    model: SecurityBaseModel
    scheme_name: str
    raise_on_missing: bool

    @property
    def auto_error(self) -> bool:
        return self.raise_on_missing

    @auto_error.setter
    def auto_error(self, value: bool) -> None:
        self.raise_on_missing = value


def resolve_raise_on_missing(
    raise_on_missing: bool | None, auto_error: bool | None
) -> bool:
    if auto_error is None:
        return True if raise_on_missing is None else raise_on_missing
    if raise_on_missing is not None:
        raise TypeError(
            "Pass only `raise_on_missing`, `auto_error` is its deprecated alias."
        )
    warnings.warn(
        "`auto_error` is deprecated, use `raise_on_missing` instead.",
        DeprecationWarning,
        stacklevel=3,
    )
    return auto_error
'''

DEPRECATED_BLOCK = '''        auto_error: Annotated[
            bool | None,
            Doc(
                """
                Deprecated, use `raise_on_missing` instead. Passing it emits a
                `DeprecationWarning`.
                """
            ),
        ] = None,
'''

ANNOTATED = re.compile(
    r"        auto_error: Annotated\[\n            bool,\n(?P<doc>.*?)        \] = True,\n", re.DOTALL
)


def _annotated(match: re.Match[str]) -> str:
    doc = match.group("doc").replace("`auto_error`", "`raise_on_missing`")
    return (
        "        raise_on_missing: Annotated[\n            bool | None,\n"
        + doc
        + "        ] = None,\n"
        + DEPRECATED_BLOCK
    )


def _migrate(text: str, relative: str) -> str:
    text = text.replace(
        "from fastapi.security.base import SecurityBase\n",
        "from fastapi.security.base import SecurityBase, resolve_raise_on_missing\n",
        1,
    )
    if relative.endswith("api_key.py"):
        for old, new in (
            ("        auto_error: bool,\n", "        raise_on_missing: bool,\n"),
            ("        self.auto_error = auto_error\n", "        self.raise_on_missing = raise_on_missing\n"),
        ):
            assert text.count(old) == 1, (relative, old)
            text = text.replace(old, new)
    text, blocks = ANNOTATED.subn(_annotated, text)
    plain = "        auto_error: bool = True,\n"
    plain_count = text.count(plain)
    text = text.replace(plain, "        raise_on_missing: bool | None = None,\n        auto_error: bool | None = None,\n")
    text, forwards = re.subn(
        r"auto_error=auto_error,",
        "raise_on_missing=resolve_raise_on_missing(raise_on_missing, auto_error),",
        text,
    )
    text, assigns = re.subn(
        r"self\.auto_error = auto_error\n",
        "self.raise_on_missing = resolve_raise_on_missing(raise_on_missing, auto_error)\n",
        text,
    )
    text, checks = re.subn(r"if self\.auto_error:", "if self.raise_on_missing:", text)
    assert blocks + plain_count >= 1 and checks >= 1, relative
    assert "`auto_error` is set" not in text, relative
    return text


def apply_reference(target: Path) -> None:
    for name in ("api_key", "http", "oauth2", "open_id_connect_url"):
        relative = f"fastapi/security/{name}.py"
        path = target / relative
        path.write_text(_migrate(path.read_text(), relative))
    (target / "fastapi/security/base.py").write_text(BASE_ADDITIONS)
    _restore_tests(target, held_out=False)
