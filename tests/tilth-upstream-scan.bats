#!/usr/bin/env bats
# Tests for scripts/tilth-upstream-scan.
#
# This is a deliberate convention addition: tilth's scripts/ has no existing
# shell-test harness (regen-agents-md.sh and release.sh are untested). Flag
# this for reviewer/Paul before landing -- see the routine-scaffold handback.
#
# Mocks git/yq/gh on $PATH so every test is deterministic with no real
# network access and no dependency on this checkout's actual git state.
# jq is left as the real binary (pure, deterministic data transform).

SCAN_SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/scripts/tilth-upstream-scan"

setup() {
  WORK="$(mktemp -d)"
  mkdir -p "$WORK/scripts" "$WORK/agents/tilth-upstream" "$WORK/mockbin"
  cp "$SCAN_SCRIPT" "$WORK/scripts/tilth-upstream-scan"
  chmod +x "$WORK/scripts/tilth-upstream-scan"

  # Fixture config -- mock yq below ignores its contents and returns fixed
  # values, but the scanner's `[[ -f "$CONFIG" ]]` guard requires the file
  # to exist.
  cat > "$WORK/agents/tilth-upstream/sources.yaml" << 'YAML'
default_branch: main
upstreamable_globs:
  - "src/**"
  - "Cargo.toml"
  - "README.md"
exclude_globs:
  - ".claude/**"
  - ".hallouminate/**"
max_candidate_files: 5
max_candidate_lines: 150
YAML

  cat > "$WORK/mockbin/yq" << 'MOCKYQ'
#!/usr/bin/env bash
set -euo pipefail
query="$2"
case "$query" in
  .default_branch) echo "main" ;;
  .max_candidate_files) echo "5" ;;
  .max_candidate_lines) echo "150" ;;
  .upstreamable_globs\[\]) printf '%s\n' "src/**" "Cargo.toml" "README.md" ;;
  .exclude_globs\[\]) printf '%s\n' ".claude/**" ".hallouminate/**" ;;
  *)
    echo "mock yq: unhandled query: $query" >&2
    exit 99
    ;;
esac
MOCKYQ
  chmod +x "$WORK/mockbin/yq"

  cat > "$WORK/mockbin/gh" << 'MOCKGH'
#!/usr/bin/env bash
# The scanner never calls gh (dedup is the routine's job, not the
# scanner's) -- this stub exists only so a stray call fails loudly instead
# of hitting the real network.
echo "mock gh: unexpected invocation: $*" >&2
: > "${MOCK_GH_CALLED_MARKER:-/dev/null}"
exit 99
MOCKGH
  chmod +x "$WORK/mockbin/gh"

  MOCK_GIT_FIXTURES="$WORK/fixtures"
  mkdir -p "$MOCK_GIT_FIXTURES"
  export MOCK_GIT_FIXTURES

  cat > "$WORK/mockbin/git" << 'MOCKGIT'
#!/usr/bin/env bash
set -euo pipefail
fx="$MOCK_GIT_FIXTURES"
args="$*"

case "$args" in
  "rev-list --count upstream/main..origin/main")
    cat "$fx/ahead_by"
    ;;
  "rev-list --count origin/main..upstream/main")
    cat "$fx/behind_by"
    ;;
  "rev-list --reverse --no-merges upstream/main..origin/main")
    cat "$fx/commits" 2>/dev/null || true
    ;;
  "diff-tree --no-commit-id --name-only -r "*)
    sha="${args##* }"
    cat "$fx/commit-$sha/files"
    ;;
  "diff-tree --no-commit-id --numstat -r "*)
    sha="${args##* }"
    cat "$fx/commit-$sha/numstat"
    ;;
  "log -1 --format=%s "*)
    sha="${args##* }"
    cat "$fx/commit-$sha/subject"
    ;;
  "rev-parse "*"^")
    sha="${args#rev-parse }"
    sha="${sha%^}"
    echo "${sha}-parent"
    ;;
  "merge-tree --write-tree --quiet --merge-base="*)
    sha="${args##* }"
    status="$(cat "$fx/commit-$sha/clean")"
    exit "$status"
    ;;
  *)
    echo "mock git: unhandled invocation: $args" >&2
    exit 99
    ;;
esac
MOCKGIT
  chmod +x "$WORK/mockbin/git"

  export PATH="$WORK/mockbin:$PATH"
  export MOCK_GH_CALLED_MARKER="$WORK/gh-called"
  cd "$WORK"
}

teardown() {
  rm -rf "$WORK"
}

# fixture_commit SHA FILES NUMSTAT SUBJECT CLEAN
# CLEAN is a git exit code: 0 = applies cleanly, 1 = conflict.
fixture_commit() {
  local sha="$1" files="$2" numstat="$3" subject="$4" clean="$5"
  mkdir -p "$MOCK_GIT_FIXTURES/commit-$sha"
  printf '%s\n' "$files" > "$MOCK_GIT_FIXTURES/commit-$sha/files"
  printf '%s\n' "$numstat" > "$MOCK_GIT_FIXTURES/commit-$sha/numstat"
  printf '%s\n' "$subject" > "$MOCK_GIT_FIXTURES/commit-$sha/subject"
  printf '%s\n' "$clean" > "$MOCK_GIT_FIXTURES/commit-$sha/clean"
}

@test "ahead/behind math comes straight from git rev-list --count" {
  echo 3 > "$MOCK_GIT_FIXTURES/ahead_by"
  echo 7 > "$MOCK_GIT_FIXTURES/behind_by"
  : > "$MOCK_GIT_FIXTURES/commits"

  run scripts/tilth-upstream-scan
  [ "$status" -eq 0 ]

  ahead=$(jq -r '.ahead_by' <<<"$output")
  behind=$(jq -r '.behind_by' <<<"$output")
  sync_needed=$(jq -r '.sync_needed' <<<"$output")
  [ "$ahead" = "3" ]
  [ "$behind" = "7" ]
  [ "$sync_needed" = "true" ]
}

@test "sync_needed is false when behind_by is zero" {
  echo 3 > "$MOCK_GIT_FIXTURES/ahead_by"
  echo 0 > "$MOCK_GIT_FIXTURES/behind_by"
  : > "$MOCK_GIT_FIXTURES/commits"

  run scripts/tilth-upstream-scan
  [ "$status" -eq 0 ]

  sync_needed=$(jq -r '.sync_needed' <<<"$output")
  [ "$sync_needed" = "false" ]
}

@test "a commit touching only an excluded-glob path never becomes a candidate" {
  echo 1 > "$MOCK_GIT_FIXTURES/ahead_by"
  echo 0 > "$MOCK_GIT_FIXTURES/behind_by"
  printf '%s\n' "bbbb222" > "$MOCK_GIT_FIXTURES/commits"
  fixture_commit "bbbb222" ".claude/settings.json" "$(printf '4\t1\t.claude/settings.json')" \
    "chore(claude): update local config" 0

  run scripts/tilth-upstream-scan
  [ "$status" -eq 0 ]

  count=$(jq '.candidates | length' <<<"$output")
  [ "$count" -eq 0 ]

  # Belt check on the guard itself: no candidate ever carries an excluded path.
  hits=$(jq -r '.candidates[].files[]' <<<"$output" | grep -c '^\.claude/' || true)
  [ "$hits" -eq 0 ]
}

@test "a commit touching an excluded path nested under an allowed prefix never becomes a candidate" {
  echo 1 > "$MOCK_GIT_FIXTURES/ahead_by"
  echo 0 > "$MOCK_GIT_FIXTURES/behind_by"
  printf '%s\n' "ffff666" > "$MOCK_GIT_FIXTURES/commits"
  fixture_commit "ffff666" "src/.claude/settings.json" "$(printf '4\t1\tsrc/.claude/settings.json')" \
    "chore(claude): nested fork-only config" 0

  run scripts/tilth-upstream-scan
  [ "$status" -eq 0 ]

  count=$(jq '.candidates | length' <<<"$output")
  [ "$count" -eq 0 ]
}

@test "a commit mixing an excluded path with an allowed path is rejected outright" {
  echo 1 > "$MOCK_GIT_FIXTURES/ahead_by"
  echo 0 > "$MOCK_GIT_FIXTURES/behind_by"
  printf '%s\n' "eeee555" > "$MOCK_GIT_FIXTURES/commits"
  fixture_commit "eeee555" "$(printf 'src/foo.rs\n.hallouminate/wiki/notes.md')" \
    "$(printf '10\t2\tsrc/foo.rs\n3\t0\t.hallouminate/wiki/notes.md')" \
    "feat: foo plus a wiki note" 0

  run scripts/tilth-upstream-scan
  [ "$status" -eq 0 ]

  count=$(jq '.candidates | length' <<<"$output")
  [ "$count" -eq 0 ]
}

@test "size thresholds classify easy vs not-easy correctly" {
  echo 2 > "$MOCK_GIT_FIXTURES/ahead_by"
  echo 0 > "$MOCK_GIT_FIXTURES/behind_by"
  printf '%s\n' "aaaa111" "cccc333" > "$MOCK_GIT_FIXTURES/commits"

  # Small, clean commit: easy.
  fixture_commit "aaaa111" "src/foo.rs" "$(printf '10\t5\tsrc/foo.rs')" \
    "feat: add foo" 0

  # One file but 200 changed lines -- over the 150-line threshold: not easy.
  fixture_commit "cccc333" "src/big.rs" "$(printf '200\t0\tsrc/big.rs')" \
    "refactor: huge rewrite" 0

  run scripts/tilth-upstream-scan
  [ "$status" -eq 0 ]

  easy_small=$(jq -r '.candidates[] | select(.key | startswith("feat-add-foo")) | .easy' <<<"$output")
  easy_big=$(jq -r '.candidates[] | select(.key | startswith("refactor-huge-rewrite")) | .easy' <<<"$output")
  lines_big=$(jq -r '.candidates[] | select(.key | startswith("refactor-huge-rewrite")) | .line_count' <<<"$output")

  [ "$easy_small" = "true" ]
  [ "$easy_big" = "false" ]
  [ "$lines_big" -eq 200 ]
}

@test "a commit that fails clean cherry-pick apply is never marked easy" {
  echo 1 > "$MOCK_GIT_FIXTURES/ahead_by"
  echo 0 > "$MOCK_GIT_FIXTURES/behind_by"
  printf '%s\n' "dddd444" > "$MOCK_GIT_FIXTURES/commits"
  fixture_commit "dddd444" "README.md" "$(printf '3\t2\tREADME.md')" \
    "docs: update readme" 1

  run scripts/tilth-upstream-scan
  [ "$status" -eq 0 ]

  applies_clean=$(jq -r '.candidates[0].applies_clean' <<<"$output")
  easy=$(jq -r '.candidates[0].easy' <<<"$output")
  [ "$applies_clean" = "false" ]
  [ "$easy" = "false" ]
}

@test "candidate key is a stable slug of the subject plus the short sha" {
  echo 1 > "$MOCK_GIT_FIXTURES/ahead_by"
  echo 0 > "$MOCK_GIT_FIXTURES/behind_by"
  printf '%s\n' "aaaa111" > "$MOCK_GIT_FIXTURES/commits"
  fixture_commit "aaaa111" "src/foo.rs" "$(printf '10\t5\tsrc/foo.rs')" \
    "feat: add foo" 0

  run scripts/tilth-upstream-scan
  [ "$status" -eq 0 ]
  key=$(jq -r '.candidates[0].key' <<<"$output")
  [ "$key" = "feat-add-foo-aaaa111" ]

  # Determinism: re-running against the same fixtures yields the same key.
  run scripts/tilth-upstream-scan
  [ "$status" -eq 0 ]
  key2=$(jq -r '.candidates[0].key' <<<"$output")
  [ "$key2" = "feat-add-foo-aaaa111" ]
}

@test "empty candidates and no drift is the exit-quietly signal" {
  echo 0 > "$MOCK_GIT_FIXTURES/ahead_by"
  echo 0 > "$MOCK_GIT_FIXTURES/behind_by"
  : > "$MOCK_GIT_FIXTURES/commits"

  run scripts/tilth-upstream-scan
  [ "$status" -eq 0 ]

  expected='{"ahead_by":0,"behind_by":0,"sync_needed":false,"candidates":[]}'
  actual="$(jq -c . <<<"$output")"
  [ "$actual" = "$expected" ]
}

@test "the scanner never invokes gh (read-only, no network mutation)" {
  echo 1 > "$MOCK_GIT_FIXTURES/ahead_by"
  echo 1 > "$MOCK_GIT_FIXTURES/behind_by"
  printf '%s\n' "aaaa111" > "$MOCK_GIT_FIXTURES/commits"
  fixture_commit "aaaa111" "src/foo.rs" "$(printf '10\t5\tsrc/foo.rs')" \
    "feat: add foo" 0

  run scripts/tilth-upstream-scan
  [ "$status" -eq 0 ]
  [ ! -e "$MOCK_GH_CALLED_MARKER" ]
}
