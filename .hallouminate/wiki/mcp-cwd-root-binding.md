# MCP cwd / workspace-root binding

How tilth resolves relative paths to the *right* checkout, why it uses a **required** per-call `root` param instead of the MCP `roots` capability, and the git-worktree gotcha that motivated it. Full cited research lives at `.cheese/research/mcp-cwd-root-binding/` (local, gitignored). Tracked in issue #78 / PR #79; the hard-refuse follow-up is `tilth-require-root` (v0.9.0).

## The problem: a long-lived server can't learn the live cwd

tilth is a long-lived stdio MCP server, spawned once per session. It cannot *pull* the caller's current directory:

- The OS cwd of the tilth process is frozen at spawn; no other process's `cd` changes it.
- The MCP `tools/call` frame carries no cwd field.
- The directory the agent "is in" lives in a different process (the harness's Bash shell) or only in the agent's intent — nothing tilth can query.

So the live cwd must be *pushed* by the caller. The only universally reliable push is an explicit argument on each call.

## The git-worktree gotcha (issue #78)

Server launched at `/repo` (main checkout); agent works in `/repo/.worktrees/<slug>/`. Relative reads/writes resolve against the **main checkout**, not the worktree. The read *succeeds* (reads the parent copy) so hash anchors match and the edit looks correct — the only tell is `git status` in the worktree coming up empty. Silent and dangerous.

## Decision: per-call `root` param, now REQUIRED for relatives (not MCP roots)

The file-I/O tools take a `root`: an absolute path/scope is used as-is; a relative path/scope anchors under an absolute `root`. As of `tilth-require-root` (v0.9.0) the silent cwd fallback is **removed** — a relative path/scope with no absolute `root` (and a relative `root`) is a **hard tool error**, not a cwd-relative read. This intentionally **breaks** the #78 promise that "omitting `root` is byte-identical to cwd-relative"; that byte-identical behavior *was* the worktree bug. The MCP-only refusal carries an actionable message naming the offending path and the absolute-`root` escape hatch. The CLI is unchanged — its cwd *is* the user's shell cwd.

Why hard-refuse instead of fall back to cwd? A silent fallback is indistinguishable from a correct read until `git status` comes up empty — exactly the failure #78 documented. Refusing is fail-fast and loud (Rule 4 in the robustness ladder of `~/mcproots.md`: "never resolve relatives against process cwd" is the Best tier).

Why not the MCP `roots` capability (the spec-blessed channel)? Cross-client research across 8 harnesses found **no client fires `notifications/roots/list_changed` for a git-worktree `cd`** — it's a terminal action, not a workspace-folder event. Adoption is also patchy:

| Harness | roots | listChanged | Notes |
|---|---|---|---|
| VS Code (Copilot) | yes | yes (folder UI only) | not fired for a worktree `cd` |
| Claude Code | partial | no (#53861 open) | `roots/list` works v2.1.39+; `CLAUDE_PROJECT_DIR` set once at launch |
| Cursor | declares, broken | no | `roots/list` → `-32601 Method not found` |
| Zed / Codex / Cline / Continue / Windsurf | no | no | spawn-cwd at best, frozen at launch |

So per-call `root` is the only channel that fixes the worktree case on any client — and it matches the dominant peer-server pattern: `mcp-server-git` takes `repo_path` per call; the official `filesystem` server and DesktopCommander mandate per-call absolute paths.

## Implementation state

- `tilth_write` gained `root` in #73/#76 (`resolve_write_path`, `src/mcp/tools/write.rs`).
- `tilth_read` / `tilth_search` / `tilth_list` / `tilth_deps` gained `root` in #78 / PR #79: a shared `resolve_read_path` helper + `resolve_scope(args, Option<&Path>)` (`src/mcp/tools/mod.rs`, `read.rs`, `search.rs`, `list.rs`, `deps.rs`).
- `tilth-require-root` (v0.9.0) made the resolvers **fallible** behind one shared `anchor_path(raw, root, label)` predicate (`src/mcp/tools/mod.rs`): absolute → ok; relative + absolute root → join; relative + relative root → `Err`; relative + no root → `Err`. `resolve_read_path` / `resolve_scope` / `resolve_write_path` all route through it and propagate `?` into `tool_read` / `tool_search` / `tool_list` / `tool_deps` / `tool_write` / `tool_grok`. `tilth_grok` gained a `root` param for parity (its scope shares the hazard). The write **scope guard** (overwrite/append containment boundary) keeps its cwd default on purpose — it bounds *where a write may land*, separate from the path-resolution channel.
- MCP `roots/list` is handled **one-shot at startup only** (chdir on first valid root, `src/mcp/mod.rs`); there is **no** `roots/listChanged` handler (notifications are dropped). **Rule-4 resolution:** the generic `~/mcproots.md` advice favors implementing `roots/listChanged`, but our cross-client research (table below) found no client fires it for a worktree `cd`. The more-tested, more-recent finding wins: `listChanged` stays unimplemented; per-call absolute `root` is the only channel that fixes the worktree case on any client.
- No git / `GIT_DIR` / `rev-parse` worktree auto-detection anywhere.
- `tilth_search`'s `context` param (a ranking-proximity hint) is intentionally **not** anchored under `root` — it's not a path to read.

## Deliberately not done (lower value)

- A stateful `set_default_root` tool (Serena `activate_project` / cyanheads `set_filesystem_default` precedent) — open design question; lower per-call friction but invisible session state that can drift.
- A `roots/listChanged` handler — no client fires it for worktree switches (Rule-4 resolution above: our cross-client research beats the generic `~/mcproots.md` recommendation here).
- Path-boundary sandboxing — tilth is a local single-user tool with no allow-list to enforce.

## Caveat

One sub-agent claimed MCP `roots` is deprecated in a "2026-07-28 spec RC"; this was **not** verifiable (the date is in the future) and is not load-bearing for the decision above. Re-check before relying on it.

## Related

- Issue: <https://github.com/paulnsorensen/tilth/issues/78>
- PR: <https://github.com/paulnsorensen/tilth/pull/79>
- Full cited research (local, gitignored): `.cheese/research/mcp-cwd-root-binding/`
