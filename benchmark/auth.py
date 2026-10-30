"""Subscription-only auth guard for every ``claude -p`` subprocess the benchmark starts.

A run must bill the Claude subscription, never an API key. It authenticates through
``CLAUDE_CODE_OAUTH_TOKEN`` when that is set, else through the host's ``claude /login`` session. Three
layers enforce it, because under ``claude -p`` an API key from any source outranks both:

1. ``preflight_claude_auth`` refuses before any paid call: a refused environment variable, a refused key
   in a settings file the call loads, or a ``claude auth status`` method other than the expected one
   (the positive proof): ``oauth_token`` with the token, ``claude.ai`` without it.
2. ``InitGate`` checks the ``apiKeySource`` of a live subprocess's init event, and that no assistant
   event arrives before it.
3. ``check_captured`` replays a captured stream through one gate for calls that do not stream live.

Refusal messages name a file and a key, never a value.
"""

import json
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from jsonl import tolerant_jsonl

OAUTH_TOKEN_KEY = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_METHOD = "oauth_token"
# ``claude auth status`` for a subscription ``/login`` session (claude 2.1.310, 2026-10-30).
LOGIN_METHOD = "claude.ai"
FIRST_PARTY = "firstParty"

# Credentials that outrank ``CLAUDE_CODE_OAUTH_TOKEN``, or select a non-subscription provider
# (https://code.claude.com/docs/en/authentication, "Authentication precedence" and
# "Anthropic profiles and federation credentials").
REFUSED_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
)

# The init event's ``apiKeySource`` under subscription auth. A live probe on 2026-10-16 with claude 2.1.301
# pinned it for ``CLAUDE_CODE_OAUTH_TOKEN``: ``claude auth status`` reported authMethod ``oauth_token`` and
# the init event reported apiKeySource ``none``. ``none`` only proves that no API key is in use.
ALLOWED_API_KEY_SOURCES = frozenset({"none"})

AUTH_STATUS_TIMEOUT_S = 60
DEFAULT_MANAGED_ROOT = Path("/")


class ClaudeAuthError(RuntimeError):
    """A Claude subprocess might bill an API key instead of the subscription."""


@dataclass(frozen=True)
class AuthTarget:
    """One ``claude`` call shape: where it runs, its environment, and whether it loads user settings."""

    cwd: Path
    env: Mapping[str, str]
    loads_user_settings: bool


def refuse_env(env: Mapping[str, str]) -> None:
    """Refuse an environment that sets a credential or provider switch that outranks the OAuth token."""
    for key in REFUSED_ENV_KEYS:
        if env.get(key):
            raise ClaudeAuthError(
                f"{key} is set; it outranks CLAUDE_CODE_OAUTH_TOKEN and bills the API or another "
                "provider instead of the subscription. Unset it before running Claude calls."
            )


def _settings_files(target: AuthTarget, managed_root: Path) -> list[Path]:
    managed = managed_root / "etc" / "claude-code"
    files = [
        managed / "managed-settings.json",
        *sorted((managed / "managed-settings.d").glob("*.json")),
        managed_root / "Library" / "Application Support" / "ClaudeCode" / "managed-settings.json",
    ]
    if target.loads_user_settings:
        config_dir = target.env.get("CLAUDE_CONFIG_DIR")
        home = Path(target.env.get("HOME") or Path.home())
        files.append((Path(config_dir) if config_dir else home / ".claude") / "settings.json")
        files += [target.cwd / ".claude" / "settings.json", target.cwd / ".claude" / "settings.local.json"]
    return files


def _scan_settings(path: Path) -> None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError as error:
        raise ClaudeAuthError(f"{path}: settings file is unreadable ({error.strerror or 'OSError'}); "
                              "it may set an API credential") from error
    except UnicodeDecodeError as error:
        raise ClaudeAuthError(f"{path}: settings file is not valid UTF-8; it may set an API credential") from error
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ClaudeAuthError(f"{path}: settings file is not valid JSON; it may set an API credential") from error
    if not isinstance(data, dict):
        raise ClaudeAuthError(f"{path}: settings file is not a JSON object; it may set an API credential")
    injected = data.get("env")
    if isinstance(injected, dict):
        for key in REFUSED_ENV_KEYS:
            if key in injected:
                raise ClaudeAuthError(f"{path}: settings env sets {key}, which outranks CLAUDE_CODE_OAUTH_TOKEN")
    if data.get("apiKeyHelper"):
        raise ClaudeAuthError(f"{path}: settings set apiKeyHelper, which outranks CLAUDE_CODE_OAUTH_TOKEN")


def expected_method(env: Mapping[str, str]) -> str:
    """The ``claude auth status`` method a subscription call must report under ``env``."""
    return TOKEN_METHOD if env.get(OAUTH_TOKEN_KEY) else LOGIN_METHOD


def _require_subscription_status(target: AuthTarget, spawn: Callable[..., subprocess.CompletedProcess]) -> None:
    try:
        completed = spawn(["claude", "auth", "status"], capture_output=True, text=True, cwd=str(target.cwd),
                          env=dict(target.env), timeout=AUTH_STATUS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as error:
        raise ClaudeAuthError(f"claude auth status could not run in {target.cwd}: {error}") from error
    method = provider = None
    try:
        payload = json.loads(completed.stdout or "")
        if isinstance(payload, dict):
            method, provider = payload.get("authMethod"), payload.get("apiProvider", FIRST_PARTY)
    except json.JSONDecodeError:
        pass
    expected = expected_method(target.env)
    source = OAUTH_TOKEN_KEY if expected == TOKEN_METHOD else "a claude /login subscription session"
    if completed.returncode != 0 or method != expected:
        raise ClaudeAuthError(
            f"claude auth status in {target.cwd} reports authMethod {method!r} (exit {completed.returncode}); "
            f"expected {expected!r} from {source}."
        )
    if provider != FIRST_PARTY:
        raise ClaudeAuthError(
            f"claude auth status in {target.cwd} reports apiProvider {provider!r}; expected {FIRST_PARTY!r}."
        )


# Targets whose ``claude auth status`` passed in this process. Only a pass is cached, so a changed
# environment or settings file is still caught: those checks run on every call.
_PASSED_STATUS: set[tuple] = set()


def _run_status(*args, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(*args, **kwargs)


def preflight_claude_auth(
    targets: Iterable[AuthTarget], *,
    spawn: Callable[..., subprocess.CompletedProcess] | None = None,
    managed_root: Path | None = None,
) -> None:
    """Refuse before any paid call unless every distinct target authenticates by the Claude subscription.

    Per target: no refused variable is set, no settings file the call loads sets a refused key or
    ``apiKeyHelper``, and ``claude auth status`` reports the first-party ``expected_method``: ``oauth_token``
    when ``CLAUDE_CODE_OAUTH_TOKEN`` is set, else ``claude.ai`` from ``/login``. ``claude auth status`` makes
    no model call. ``managed_root`` is the filesystem root that holds the managed settings.
    The default status probe runs once per distinct target per process; the other checks run on every call.
    """
    use_default_probe = spawn is None
    spawn = spawn or _run_status
    managed_root = DEFAULT_MANAGED_ROOT if managed_root is None else managed_root
    seen: set[tuple] = set()
    for target in targets:
        key = (str(target.cwd), tuple(sorted(target.env.items())), target.loads_user_settings)
        if key in seen:
            continue
        seen.add(key)
        refuse_env(target.env)
        for path in _settings_files(target, managed_root):
            _scan_settings(path)
        if use_default_probe and key in _PASSED_STATUS:
            continue
        _require_subscription_status(target, spawn)
        if use_default_probe:
            _PASSED_STATUS.add(key)


class InitGate:
    """Check the events of one ``claude`` subprocess as they arrive."""

    def __init__(self) -> None:
        self._seen_init = False

    def observe(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self._seen_init = True
            source = event.get("apiKeySource")
            if not isinstance(source, str) or source not in ALLOWED_API_KEY_SOURCES:
                raise ClaudeAuthError(
                    f"claude init event reports apiKeySource {source!r}; expected one of "
                    f"{sorted(ALLOWED_API_KEY_SOURCES)}."
                )
        elif kind == "assistant" and not self._seen_init:
            raise ClaudeAuthError("claude stream has an assistant event before any init event; "
                                  "its apiKeySource is unchecked.")


def check_captured(stdout: str) -> None:
    """Replay a captured stream through one ``InitGate``."""
    gate = InitGate()
    for event in tolerant_jsonl(stdout):
        gate.observe(event)
