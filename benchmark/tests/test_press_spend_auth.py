"""Press-phase adversarial tests for the spend ledger and the subscription-only auth guard."""

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import auth
import spend
from auth import AuthTarget, ClaudeAuthError, InitGate, check_captured, preflight_claude_auth
from spend import SpendLedger, settle_failed

GOOD_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-fixture"}
OK = '{"authMethod": "oauth_token", "apiProvider": "firstParty"}'


def _spawn(argv, **kwargs):
    return subprocess.CompletedProcess(argv, 0, OK, "")


def _preflight(tmp_path: Path, user: bool = True) -> None:
    cwd = tmp_path / "cell"
    cwd.mkdir(exist_ok=True)
    env = {**GOOD_ENV, "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}
    preflight_claude_auth([AuthTarget(cwd, env, user)], spawn=_spawn, managed_root=tmp_path / "root")


def _settings(tmp_path: Path, text) -> Path:
    path = tmp_path / "cfg" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text if isinstance(text, str) else json.dumps(text))
    return path


def _ev(**kw) -> str:
    return json.dumps(kw)


# --- 1. budget file robustness (G-1, G-2)

def test_persistent_torn_last_line_does_not_undercount_or_must_fail_loudly(tmp_path: Path) -> None:
    ledger = SpendLedger.persistent("b", 100.0, root=tmp_path)
    ledger.charge(2.0, source="native", run_key="k1")
    with open(tmp_path / "b.jsonl", "a") as out:
        out.write('{"run_key": "k2", "charged_usd": 5.0, "cost_sou')  # crash mid-append
    try:
        reopened = SpendLedger.persistent("b", 100.0, root=tmp_path)
    except Exception:
        return  # failing loudly is acceptable
    assert reopened.spent >= 7.0, f"torn record silently dropped: spent={reopened.spent}"


def test_charge_after_torn_line_is_still_counted_on_reopen(tmp_path: Path) -> None:
    # Corrected attack: the torn record's amount is unknown, so a reopen must fail loudly (G-2), and
    # --fresh-budget must recover with its marker and later charges kept whole after the torn tail.
    first = SpendLedger.persistent("b", 100.0, root=tmp_path)
    first.charge(2.0, source="native", run_key="k1")
    with open(tmp_path / "b.jsonl", "a") as out:
        out.write('{"run_key": "k2", "charged_usd": 5.0')  # torn, no newline
    with pytest.raises(ValueError, match="--fresh-budget"):
        SpendLedger.persistent("b", 100.0, root=tmp_path)
    second = SpendLedger.persistent("b", 100.0, fresh=True, root=tmp_path)
    second.charge(3.0, source="native", run_key="k3")
    third = SpendLedger.persistent("b", 100.0, root=tmp_path)
    assert third.spent == 3.0, f"charge appended after a torn line is lost: spent={third.spent}"


def test_persistent_garbage_line_does_not_seed_zero(tmp_path: Path) -> None:
    (tmp_path / "b.jsonl").write_text("not json at all\n")
    try:
        ledger = SpendLedger.persistent("b", 100.0, root=tmp_path)
    except Exception:
        return  # failing loudly is acceptable
    assert ledger.spent != 0.0, "corrupt budget file silently seeds zero spend"


# --- 3. charge record values

def test_recorded_negative_charge_cannot_lower_spend(tmp_path: Path) -> None:
    ledger = SpendLedger.persistent("b", 100.0, root=tmp_path)
    ledger.charge(4.0, source="native", run_key="k1")
    with open(tmp_path / "b.jsonl", "a") as out:
        out.write(json.dumps({"run_key": "x", "charged_usd": -4.0, "cost_source": "native"}) + "\n")
    assert SpendLedger.persistent("b", 100.0, root=tmp_path).spent >= 4.0


def test_charge_nan_is_rejected_or_keeps_ceiling_effective(tmp_path: Path) -> None:
    ledger = SpendLedger.persistent("b", 10.0, root=tmp_path)
    try:
        ledger.charge(float("nan"), source="native", run_key="k")
    except ValueError:
        return
    assert ledger.would_cross(1.0), "NaN spend disables the ceiling forever"


def test_charge_nan_in_memory_ledger_does_not_disable_ceiling() -> None:
    ledger = SpendLedger(max_usd=1.0)
    try:
        ledger.charge(float("nan"), source="native")
    except ValueError:
        return
    assert ledger.would_cross(0.5), "NaN spend disables the ceiling"


def test_recorded_string_charge_is_not_silently_ignored(tmp_path: Path) -> None:
    (tmp_path / "b.jsonl").write_text(json.dumps({"run_key": "x", "charged_usd": "9.0"}) + "\n")
    try:
        ledger = SpendLedger.persistent("b", 100.0, root=tmp_path)
    except Exception:
        return
    assert ledger.spent != 0.0, "unparseable charge record silently counts zero"


def test_multiple_reset_markers_only_newest_counts(tmp_path: Path) -> None:
    def rec(**kw) -> str:
        return json.dumps(kw) + "\n"

    (tmp_path / "b.jsonl").write_text(rec(charged_usd=1.0) + rec(reset=True) + rec(charged_usd=2.0)
                                      + rec(reset=True) + rec(charged_usd=4.0))
    assert SpendLedger.persistent("b", 100.0, root=tmp_path).spent == 4.0


def test_marker_then_charges_then_reopen_without_fresh_keeps_post_marker_charges(tmp_path: Path) -> None:
    a = SpendLedger.persistent("b", 100.0, root=tmp_path)
    a.charge(5.0, source="native", run_key="old")
    b = SpendLedger.persistent("b", 100.0, fresh=True, root=tmp_path)
    assert b.spent == 0.0
    b.charge(1.5, source="native", run_key="new")
    assert SpendLedger.persistent("b", 100.0, root=tmp_path).spent == 1.5


def test_reset_marker_with_truthy_non_true_value_does_not_reset(tmp_path: Path) -> None:
    (tmp_path / "b.jsonl").write_text(json.dumps({"charged_usd": 3.0}) + "\n" + json.dumps({"reset": 1}) + "\n")
    assert SpendLedger.persistent("b", 100.0, root=tmp_path).spent == 3.0


# --- 2. budget id injection

@pytest.mark.parametrize("bad", ["/etc/x", "a/../b", "..", "../x", "a/b", "", ".hidden", "a\x00b", "-x",
                                 "a\\..\\b", "é"])
def test_budget_id_never_escapes_budget_dir(tmp_path: Path, bad: str) -> None:
    root = tmp_path / "budgets"
    try:
        SpendLedger.persistent(bad, 1.0, root=root)
    except (ValueError, OSError):
        pass
    for path in tmp_path.rglob("*"):
        assert root in path.parents or path == root, f"{path} escaped {root}"
    assert not Path("/etc/x.jsonl").exists()


def test_budget_id_with_trailing_newline_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SpendLedger.persistent("abc\n", 1.0, root=tmp_path)


def test_budget_id_very_long_fails_with_value_or_os_error(tmp_path: Path) -> None:
    with pytest.raises((ValueError, OSError)):
        SpendLedger.persistent("a" * 5000, 1.0, root=tmp_path)


def test_budget_id_unicode_digits_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SpendLedger.persistent("٣٣", 1.0, root=tmp_path)


def test_plan_budget_id_is_order_and_duplicate_insensitive_and_distinguishes_sets() -> None:
    assert spend.plan_budget_id(["a", "b"]) == spend.plan_budget_id(["b", "a", "a"])
    assert spend.plan_budget_id(["a", "b"]) != spend.plan_budget_id(["a", "c"])


def test_followup_plan_budget_id_newline_in_key_collides_with_two_keys() -> None:
    assert spend.plan_budget_id(["a\nb"]) == spend.plan_budget_id(["a", "b"])  # documents: run keys hold no newline


def test_plan_budget_id_empty_plan_is_valid_id(tmp_path: Path) -> None:
    SpendLedger.persistent(spend.plan_budget_id([]), 1.0, root=tmp_path)


# --- 8. concurrency (documents current behaviour)

def test_two_ledgers_sharing_a_budget_each_see_the_others_charge(tmp_path: Path) -> None:
    a = SpendLedger.persistent("b", 10.0, root=tmp_path)
    b = SpendLedger.persistent("b", 10.0, root=tmp_path)
    a.charge(6.0, source="native", run_key="a")
    assert b.would_cross(5.0)


def test_followup_check_then_charge_race_overshoots_the_ceiling(tmp_path: Path) -> None:
    a = SpendLedger.persistent("b", 10.0, root=tmp_path)
    b = SpendLedger.persistent("b", 10.0, root=tmp_path)
    assert not a.would_cross(6.0) and not b.would_cross(6.0)
    a.charge(6.0, source="native", run_key="a")
    b.charge(6.0, source="native", run_key="b")
    assert b.spent == 12.0  # documents: overshoot is bounded by one in-flight cell per concurrent run


# --- 4. InitGate / check_captured

def _init(source="none") -> dict:
    return {"type": "system", "subtype": "init", "apiKeySource": source}


def test_second_init_with_disallowed_source_is_refused() -> None:
    gate = InitGate()
    gate.observe(_init())
    with pytest.raises(ClaudeAuthError):
        gate.observe(_init("ANTHROPIC_API_KEY"))


@pytest.mark.parametrize("source", ["none ", "NONE", " none", "None", "", None, 0, ["none"], "none\n"])
def test_init_source_must_match_exactly(source) -> None:
    with pytest.raises(ClaudeAuthError):
        InitGate().observe(_init(source))


def test_init_missing_api_key_source_is_refused() -> None:
    with pytest.raises(ClaudeAuthError):
        InitGate().observe({"type": "system", "subtype": "init"})


def test_assistant_after_non_init_system_event_is_refused() -> None:
    gate = InitGate()
    gate.observe({"type": "system", "subtype": "hook_started"})
    with pytest.raises(ClaudeAuthError):
        gate.observe({"type": "assistant", "message": {}})


def test_check_captured_crlf_stream_still_gates() -> None:
    stream = "\r\n".join([_ev(type="assistant", message={}), _ev(**_init())])
    with pytest.raises(ClaudeAuthError):
        check_captured(stream)


def test_check_captured_crlf_good_stream_passes() -> None:
    check_captured("\r\n".join([_ev(**_init()), _ev(type="assistant", message={})]))


def test_check_captured_bad_init_after_torn_line_still_refused() -> None:
    with pytest.raises(ClaudeAuthError):
        check_captured('{"type": "sys\n' + _ev(**_init("ANTHROPIC_API_KEY")) + "\n")


def test_check_captured_empty_stream_passes_without_error() -> None:
    check_captured("")


def test_check_captured_assistant_after_torn_init_is_refused() -> None:
    stream = '{"type": "system", "subtype": "init", "apiKeyS\n' + _ev(type="assistant", message={}) + "\n"
    with pytest.raises(ClaudeAuthError):
        check_captured(stream)


@pytest.mark.parametrize("kind", ["user", "result", "stream_event"])
def test_non_assistant_event_before_init_is_tolerated(kind: str) -> None:
    gate = InitGate()
    gate.observe({"type": kind})
    gate.observe(_init())


# --- 5. settle_failed

def _assistant(mid="m1", **usage) -> str:
    return _ev(type="assistant", message={"id": mid, "usage": usage, "content": []})


def test_settle_failed_nan_native_cost_is_not_returned() -> None:
    stream = '{"type": "result", "is_error": true, "total_cost_usd": NaN}\n'
    cost, _ = settle_failed(stream, "claude", 1.0)
    assert not math.isnan(cost), "NaN native cost flows to the ledger"


def test_settle_failed_negative_native_cost_would_not_crash_charge() -> None:
    cost, source = settle_failed(_ev(type="result", is_error=True, total_cost_usd=-2.0) + "\n", "claude", 1.0)
    assert cost >= 0, f"negative native cost {cost} ({source}) would crash SpendLedger.charge"


def test_settle_failed_error_result_with_cost_is_native() -> None:
    stream = _assistant(input_tokens=10, output_tokens=10) + "\n" + _ev(
        type="result", is_error=True, total_cost_usd=0.42) + "\n"
    assert settle_failed(stream, "claude", 9.0) == (0.42, "native")


def test_settle_failed_zero_native_cost_is_native_zero() -> None:
    assert settle_failed(_ev(type="result", total_cost_usd=0.0) + "\n", "claude", 9.0) == (0.0, "native")


def test_settle_failed_empty_stream_charges_estimate() -> None:
    assert settle_failed("", "claude", 1.25) == (1.25, "estimate")
    assert settle_failed("", "codex", 1.25) == (1.25, "estimate")


def test_settle_failed_only_torn_garbage_charges_estimate() -> None:
    assert settle_failed('{"type": "assis', "claude", 1.25) == (1.25, "estimate")


def test_settle_failed_usage_with_unknown_model_never_charges_zero() -> None:
    stream = _assistant(input_tokens=500, output_tokens=500) + "\n"
    try:
        cost, source = settle_failed(stream, "claude", 2.0, "totally-unknown-model")
    except Exception:
        return
    assert cost > 0, (cost, source)


# --- 6. settings scan

@pytest.mark.parametrize("key", list(auth.REFUSED_ENV_KEYS))
def test_settings_env_key_with_empty_value_still_refused(tmp_path: Path, key: str) -> None:
    _settings(tmp_path, {"env": {key: ""}})
    with pytest.raises(ClaudeAuthError):
        _preflight(tmp_path)


@pytest.mark.parametrize("value", ["", False, 0, None])
def test_followup_falsy_api_key_helper_is_not_refused(tmp_path: Path, value) -> None:
    _settings(tmp_path, {"apiKeyHelper": value})
    _preflight(tmp_path)  # documents: a falsy helper counts as unset


def test_api_key_helper_non_empty_refused(tmp_path: Path) -> None:
    _settings(tmp_path, {"apiKeyHelper": "/bin/true"})
    with pytest.raises(ClaudeAuthError):
        _preflight(tmp_path)


def test_settings_file_that_is_a_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "cfg" / "settings.json").mkdir(parents=True)
    with pytest.raises(ClaudeAuthError):
        _preflight(tmp_path)


def test_settings_with_bom_is_refused_not_ignored(tmp_path: Path) -> None:
    _settings(tmp_path, b"\xef\xbb\xbf" + json.dumps({"env": {"ANTHROPIC_API_KEY": "k"}}).encode())
    with pytest.raises(ClaudeAuthError):
        _preflight(tmp_path)


def test_settings_with_invalid_utf8_raises_claude_auth_error(tmp_path: Path) -> None:
    _settings(tmp_path, b'{"env": {"ANTHROPIC_API_KEY": "\xff\xfe"}}')
    with pytest.raises(ClaudeAuthError):
        _preflight(tmp_path)


def test_symlinked_settings_file_with_key_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "t"}}))
    link = tmp_path / "cfg" / "settings.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    with pytest.raises(ClaudeAuthError):
        _preflight(tmp_path)


def test_dangling_symlink_settings_is_treated_as_absent(tmp_path: Path) -> None:
    link = tmp_path / "cfg" / "settings.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "missing.json")
    _preflight(tmp_path)


def test_project_settings_ignored_when_cell_skips_user_settings(tmp_path: Path) -> None:
    path = tmp_path / "cell" / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "k"}}))
    _preflight(tmp_path, user=False)


def test_managed_settings_refused_even_when_cell_skips_user_settings(tmp_path: Path) -> None:
    path = tmp_path / "root" / "etc" / "claude-code" / "managed-settings.d" / "10-x.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}))
    with pytest.raises(ClaudeAuthError):
        _preflight(tmp_path, user=False)


def test_refusal_message_never_contains_settings_value(tmp_path: Path) -> None:
    _settings(tmp_path, {"env": {"ANTHROPIC_API_KEY": "sk-ant-LEAK"}})
    with pytest.raises(ClaudeAuthError) as caught:
        _preflight(tmp_path)
    assert "sk-ant-LEAK" not in str(caught.value)


# --- 7. process env

def test_followup_empty_string_refused_env_key_is_not_refused() -> None:
    auth.refuse_env({"ANTHROPIC_API_KEY": ""})  # documents: an empty value counts as unset
