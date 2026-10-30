"""Subscription-only auth guard: env, settings, `claude auth status`, and init-event checks, all offline."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import auth
from auth import AuthTarget, ClaudeAuthError, InitGate, check_captured, preflight_claude_auth, refuse_env

SECRET = "sk-ant-SECRETVALUE-9f3a"
GOOD_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-fixture"}
TOKEN_STATUS = '{"authMethod": "oauth_token", "apiProvider": "firstParty"}'


class Spawn:
    def __init__(self, stdout: str = TOKEN_STATUS, returncode: int = 0) -> None:
        self.stdout, self.returncode, self.calls = stdout, returncode, []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


def target(tmp_path: Path, env: dict | None = None, *, user: bool = True, name: str = "cell") -> AuthTarget:
    cwd = tmp_path / name
    cwd.mkdir(exist_ok=True)
    return AuthTarget(cwd=cwd, env=GOOD_ENV if env is None else env, loads_user_settings=user)


def run(tmp_path: Path, tgt: AuthTarget, spawn: Spawn | None = None) -> Spawn:
    spawn = spawn or Spawn()
    preflight_claude_auth([tgt], spawn=spawn, managed_root=tmp_path / "root")
    return spawn


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


LOGIN = json.dumps({"authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"})


# AC-15
@pytest.mark.parametrize("env", [{}, {"CLAUDE_CODE_OAUTH_TOKEN": ""}], ids=["unset", "empty"])
def test_login_session_passes_without_token(tmp_path: Path, env: dict) -> None:
    spawn = run(tmp_path, target(tmp_path, env), Spawn(LOGIN))
    assert [argv for argv, _ in spawn.calls] == [["claude", "auth", "status"]]


@pytest.mark.parametrize("method", ["oauth_token", "api_key", None])
def test_without_token_only_a_login_session_passes(tmp_path: Path, method: str | None) -> None:
    tgt = target(tmp_path, {})
    with pytest.raises(ClaudeAuthError, match="expected 'claude.ai' from a claude /login") as caught:
        run(tmp_path, tgt, Spawn(json.dumps({"authMethod": method})))
    assert str(tgt.cwd) in str(caught.value)


@pytest.mark.parametrize("env", [{}, GOOD_ENV], ids=["login", "token"])
@pytest.mark.parametrize("provider", ["bedrock", "vertex", None, "omitted"])
def test_non_first_party_provider_refused(tmp_path: Path, env: dict, provider: str | None) -> None:
    status = {"authMethod": "oauth_token" if env else "claude.ai"}
    if provider != "omitted":
        status["apiProvider"] = provider
    with pytest.raises(ClaudeAuthError, match="apiProvider"):
        run(tmp_path, target(tmp_path, env), Spawn(json.dumps(status)))


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_login_mode_still_refuses_api_keys_before_spawn(tmp_path: Path, key: str) -> None:
    spawn = Spawn(LOGIN)
    with pytest.raises(ClaudeAuthError, match=key):
        run(tmp_path, target(tmp_path, {key: SECRET}), spawn)
    assert spawn.calls == []


def test_login_mode_still_refuses_api_key_helper(tmp_path: Path) -> None:
    tgt = target(tmp_path, {})
    write(tgt.cwd / ".claude" / "settings.json", {"apiKeyHelper": "x"})
    with pytest.raises(ClaudeAuthError, match="apiKeyHelper"):
        run(tmp_path, tgt, Spawn(LOGIN))


# AC-16
@pytest.mark.parametrize("key", auth.REFUSED_ENV_KEYS)
def test_refuse_env_names_key_not_value(key: str) -> None:
    with pytest.raises(ClaudeAuthError) as caught:
        refuse_env({key: SECRET})
    assert key in str(caught.value)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("key", auth.REFUSED_ENV_KEYS)
def test_preflight_refuses_each_key_before_spawn(tmp_path: Path, key: str) -> None:
    spawn = Spawn()
    with pytest.raises(ClaudeAuthError) as caught:
        run(tmp_path, target(tmp_path, {**GOOD_ENV, key: SECRET}), spawn)
    assert key in str(caught.value)
    assert SECRET not in str(caught.value)
    assert spawn.calls == []


def test_refused_keys_are_the_eight_documented_names() -> None:
    assert set(auth.REFUSED_ENV_KEYS) == {
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
    }
    assert len(auth.REFUSED_ENV_KEYS) == 8


def test_default_status_probe_runs_once_per_target_but_env_refusal_runs_every_call(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = Spawn()
    monkeypatch.setattr(auth, "_run_status", spawn)
    tgt = target(tmp_path)
    for _ in range(3):
        preflight_claude_auth([tgt], managed_root=tmp_path / "root")
    assert len(spawn.calls) == 1
    with pytest.raises(ClaudeAuthError, match="ANTHROPIC_API_KEY"):
        preflight_claude_auth([AuthTarget(tgt.cwd, {**GOOD_ENV, "ANTHROPIC_API_KEY": SECRET}, True)],
                              managed_root=tmp_path / "root")
    write(tgt.cwd / ".claude" / "settings.json", {"apiKeyHelper": "x"})
    with pytest.raises(ClaudeAuthError, match="apiKeyHelper"):
        preflight_claude_auth([tgt], managed_root=tmp_path / "root")


def test_failed_status_probe_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad, good = Spawn('{"authMethod": "api_key"}'), Spawn()
    tgt = target(tmp_path)
    monkeypatch.setattr(auth, "_run_status", bad)
    with pytest.raises(ClaudeAuthError):
        preflight_claude_auth([tgt], managed_root=tmp_path / "root")
    monkeypatch.setattr(auth, "_run_status", good)
    preflight_claude_auth([tgt], managed_root=tmp_path / "root")
    assert len(bad.calls) == 1 and len(good.calls) == 1


# AC-17: with the token set, a login session means the token is ignored, so it is refused too.
@pytest.mark.parametrize("method", ["api_key", "claude.ai"])
def test_wrong_auth_method_refused_naming_cwd_and_method(tmp_path: Path, method: str) -> None:
    tgt = target(tmp_path)
    with pytest.raises(ClaudeAuthError) as caught:
        run(tmp_path, tgt, Spawn(json.dumps({"authMethod": method})))
    assert str(tgt.cwd) in str(caught.value)
    assert method in str(caught.value)


def test_nonzero_exit_refused_even_with_oauth_method(tmp_path: Path) -> None:
    with pytest.raises(ClaudeAuthError, match="exit 1"):
        run(tmp_path, target(tmp_path), Spawn(TOKEN_STATUS, returncode=1))


@pytest.mark.parametrize("stdout", ["not json", "", "[1]", "{}"])
def test_unparseable_status_refused(tmp_path: Path, stdout: str) -> None:
    tgt = target(tmp_path)
    with pytest.raises(ClaudeAuthError, match=str(tgt.cwd)):
        run(tmp_path, tgt, Spawn(stdout))


def test_spawn_oserror_refused(tmp_path: Path) -> None:
    def boom(*args, **kwargs):
        raise FileNotFoundError("claude")

    with pytest.raises(ClaudeAuthError, match="could not run"):
        preflight_claude_auth([target(tmp_path)], spawn=boom, managed_root=tmp_path / "root")


def test_passing_status_accepted_and_runs_in_target_cwd_env(tmp_path: Path) -> None:
    tgt = target(tmp_path)
    spawn = run(tmp_path, tgt)
    (argv, kwargs), = spawn.calls
    assert argv == ["claude", "auth", "status"]
    assert kwargs["cwd"] == str(tgt.cwd)
    assert kwargs["env"] == dict(GOOD_ENV)


def test_identical_targets_spawn_once(tmp_path: Path) -> None:
    spawn = Spawn()
    preflight_claude_auth([target(tmp_path), target(tmp_path)], spawn=spawn, managed_root=tmp_path / "root")
    assert len(spawn.calls) == 1


def test_distinct_targets_spawn_twice(tmp_path: Path) -> None:
    spawn = Spawn()
    preflight_claude_auth([target(tmp_path, name="a"), target(tmp_path, name="b")], spawn=spawn,
                          managed_root=tmp_path / "root")
    assert len(spawn.calls) == 2


def test_same_cwd_different_loads_user_settings_are_distinct(tmp_path: Path) -> None:
    spawn = Spawn()
    preflight_claude_auth([target(tmp_path, user=True), target(tmp_path, user=False)], spawn=spawn,
                          managed_root=tmp_path / "root")
    assert len(spawn.calls) == 2


# AC-18
def managed_file(tmp_path: Path) -> Path:
    return tmp_path / "root" / "etc" / "claude-code" / "managed-settings.json"


def managed_drop_in(tmp_path: Path) -> Path:
    return tmp_path / "root" / "etc" / "claude-code" / "managed-settings.d" / "x.json"


def user_file(tmp_path: Path) -> Path:
    return tmp_path / "cfg" / "settings.json"


def project_file(tmp_path: Path) -> Path:
    return tmp_path / "cell" / ".claude" / "settings.json"


def local_file(tmp_path: Path) -> Path:
    return tmp_path / "cell" / ".claude" / "settings.local.json"


def user_target(tmp_path: Path, *, user: bool = True) -> AuthTarget:
    return target(tmp_path, {**GOOD_ENV, "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}, user=user)


BAD_BODIES = [
    ({"env": {"ANTHROPIC_API_KEY": SECRET}}, "ANTHROPIC_API_KEY"),
    ({"env": {"CLAUDE_CODE_USE_BEDROCK": SECRET}}, "CLAUDE_CODE_USE_BEDROCK"),
    ({"apiKeyHelper": f"echo {SECRET}"}, "apiKeyHelper"),
]


@pytest.mark.parametrize("body,key", BAD_BODIES)
@pytest.mark.parametrize("locate", [managed_file, managed_drop_in])
@pytest.mark.parametrize("user", [True, False])
def test_managed_settings_always_scanned(tmp_path: Path, locate, body, key, user) -> None:
    path = write(locate(tmp_path), body)
    with pytest.raises(ClaudeAuthError) as caught:
        run(tmp_path, user_target(tmp_path, user=user))
    assert str(path) in str(caught.value)
    assert key in str(caught.value)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("body,key", BAD_BODIES)
@pytest.mark.parametrize("locate", [user_file, project_file, local_file])
def test_user_project_local_settings_scanned_when_loaded(tmp_path: Path, locate, body, key) -> None:
    path = write(locate(tmp_path), body)
    with pytest.raises(ClaudeAuthError) as caught:
        run(tmp_path, user_target(tmp_path))
    assert str(path) in str(caught.value)
    assert key in str(caught.value)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("locate", [user_file, project_file, local_file])
def test_user_project_local_settings_skipped_when_not_loaded(tmp_path: Path, locate) -> None:
    write(locate(tmp_path), BAD_BODIES[0][0])
    spawn = run(tmp_path, user_target(tmp_path, user=False))
    assert len(spawn.calls) == 1


def test_home_dotclaude_fallback_scanned_without_config_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = write(home / ".claude" / "settings.json", BAD_BODIES[2][0])
    with pytest.raises(ClaudeAuthError, match="apiKeyHelper") as caught:
        run(tmp_path, target(tmp_path, {**GOOD_ENV, "HOME": str(home)}))
    assert str(path) in str(caught.value)


@pytest.mark.parametrize("data", ["{not json", "[1, 2]"])
def test_invalid_settings_refused_naming_file(tmp_path: Path, data: str) -> None:
    path = write(project_file(tmp_path), data)
    with pytest.raises(ClaudeAuthError) as caught:
        run(tmp_path, user_target(tmp_path))
    assert str(path) in str(caught.value)


def test_invalid_managed_settings_refused(tmp_path: Path) -> None:
    path = write(managed_file(tmp_path), "{oops")
    with pytest.raises(ClaudeAuthError, match="not valid JSON") as caught:
        run(tmp_path, user_target(tmp_path, user=False))
    assert str(path) in str(caught.value)


def test_benign_settings_pass(tmp_path: Path) -> None:
    for locate in (managed_file, user_file, project_file, local_file):
        write(locate(tmp_path), {"env": {"FOO": "bar"}, "model": "sonnet"})
    spawn = run(tmp_path, user_target(tmp_path))
    assert len(spawn.calls) == 1


def test_settings_refusal_happens_before_status_spawn(tmp_path: Path) -> None:
    write(project_file(tmp_path), BAD_BODIES[0][0])
    spawn = Spawn()
    with pytest.raises(ClaudeAuthError):
        run(tmp_path, user_target(tmp_path), spawn)
    assert spawn.calls == []


# InitGate
def init(source) -> dict:
    event = {"type": "system", "subtype": "init"}
    if source is not ...:
        event["apiKeySource"] = source
    return event


def test_init_none_then_assistant_passes() -> None:
    gate = InitGate()
    gate.observe(init("none"))
    gate.observe({"type": "assistant"})


@pytest.mark.parametrize("source", [..., None, "ANTHROPIC_API_KEY", "apiKeyHelper", "", 3])
def test_init_with_other_source_refused(source) -> None:
    with pytest.raises(ClaudeAuthError, match="apiKeySource"):
        InitGate().observe(init(source))


def test_assistant_before_init_refused() -> None:
    with pytest.raises(ClaudeAuthError, match="before any init"):
        InitGate().observe({"type": "assistant"})


def test_non_init_system_events_before_init_allowed() -> None:
    gate = InitGate()
    gate.observe({"type": "system", "subtype": "hook_started"})
    gate.observe({"type": "user"})
    gate.observe(init("none"))
    gate.observe({"type": "assistant"})


# AC-21
def stream(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


def test_check_captured_accepts_good_stream() -> None:
    check_captured(stream(init("none"), {"type": "assistant"}, {"type": "result"}))


@pytest.mark.parametrize("source", ["ANTHROPIC_API_KEY", "apiKeyHelper", None])
def test_check_captured_refuses_bad_source(source) -> None:
    with pytest.raises(ClaudeAuthError):
        check_captured(stream(init(source), {"type": "assistant"}))


def test_check_captured_refuses_assistant_before_init() -> None:
    with pytest.raises(ClaudeAuthError):
        check_captured(stream({"type": "assistant"}, init("none")))


def test_check_captured_tolerates_torn_last_line() -> None:
    check_captured(stream(init("none"), {"type": "assistant"}) + '\n{"type": "assis')


def test_check_captured_still_refuses_when_bad_event_precedes_torn_line() -> None:
    with pytest.raises(ClaudeAuthError):
        check_captured(stream(init("ANTHROPIC_API_KEY")) + '\n{"type": "assis')
