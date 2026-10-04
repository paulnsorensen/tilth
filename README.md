# tilth

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/jahala/tilth/badge)](https://scorecard.dev/viewer/?uri=github.com/jahala/tilth)

**Smart code reading for humans and AI agents.** Reduces cost per correct answer by **44%** on Sonnet, **39%** on Opus, and **38%** on Haiku across 160 benchmark runs. ([benchmarks](#benchmarks))

tilth is what happens when you give `ripgrep`, `tree-sitter`, and `cat` a shared brain.

```bash
$ tilth src/auth.ts
# src/auth.ts (258 lines, ~3.4k tokens) [outline]

[1-12]   imports: express(2), jsonwebtoken, @/config
[14-22]  interface AuthConfig
[24-42]  fn validateToken(token: string): Claims | null
[44-89]  export fn handleAuth(req, res, next)
[91-258] export class AuthManager
  [99-130]  fn authenticate(credentials)
  [132-180] fn authorize(user, resource)
```

Small files come back whole. Large files get an outline. Drill in with `--section`:

```bash
tilth src/auth.ts --section 44-89
tilth docs/guide.md --section "## Installation"
```

## Search finds definitions first

```
$ tilth handleAuth --scope src/
# Search: "handleAuth" in src/ — 6 matches (2 definitions, 4 usages)

## src/auth.ts:44-89 [definition]
  [24-42]  fn validateToken(token: string)
→ [44-89]  export fn handleAuth(req, res, next)
  [91-120] fn refreshSession(req, res)

  44 │ export function handleAuth(req, res, next) {
  45 │   const token = req.headers.authorization?.split(' ')[1];
  ...
  88 │   next();
  89 │ }

── calls ──
  validateToken  src/auth.ts:24-42  fn validateToken(token: string): Claims | null
  refreshSession  src/auth.ts:91-120  fn refreshSession(req, res)

## src/routes/api.ts:34 [usage]
→ [34]   router.use('/api/protected/*', handleAuth);
```

Tree-sitter finds where symbols are **defined** — not just where strings appear. Each match shows its surrounding file structure so you know what you're looking at without a second read.

Expanded definitions include a **callee footer** (`── calls ──`) showing resolved callees with file, line range, and signature — the agent can follow call chains without separate searches for each callee.

### Expanded search

CLI search returns compact results by default. Use `--expand` to inline source for the top matches:

```bash
tilth handleAuth --scope src/ --expand       # top 2 (default when flag is bare)
tilth handleAuth --scope src/ --expand=5     # top 5
```

In MCP mode, `tilth_search` routes each `query` automatically. Do not pass `expand`, `kind`, or `context`.

### Multi-symbol search

Trace across files in one call:

```bash
tilth "ServeHTTP, HandlersChain, Next" --scope .
```

Each symbol gets its own result block with definitions and expansions. The expand budget is shared — at least one expansion per symbol, deduped across files.

### Callers query

Find all call sites of a symbol using structural tree-sitter matching (not text search):

```bash
$ tilth isTrustedProxy --callers --scope .
# Callers of "isTrustedProxy" — 5 call sites

## context.go:1011 [caller: ClientIP]
→ trusted = c.engine.isTrustedProxy(remoteIP)
```

In MCP mode, pass an unchanged `fetch_callers` hint from a search response as a `follow` entry.

### Structural patterns in MCP

Use an explicit pattern entry to match syntax, rather than literal text or regular expressions:

```json
{
  "cwd": "/absolute/project",
  "queries": [
    {"pattern": "Some($A)", "language": "rust", "glob": "src/*.rs"},
    {"pattern": "wrap($A)", "language": "typescript"},
    {"pattern": "wrap($$$ARGS)", "language": "python"}
  ]
}
```

Supported languages are Rust, TypeScript, TSX, JavaScript, Python, Go, Java, Scala, C, C++, Ruby, PHP, Swift, Kotlin, C#, Elixir, and Bash.
The `language` value is the lowercase name, for example `c++` or `c#`.
`typescript` matches `.ts` files only; use `tsx` for `.tsx` files.
In every language, `$` followed by an uppercase letter or `_` starts a metavariable.
So in PHP and Bash, `$this` and `$name` stay literal variables, but `$HOME` is a metavariable.
In C, a bare call such as `wrap($A)` parses as a declaration; write `wrap($A);` to match the call statement.
Each entry contains exactly one of `query`, `follow`, or `pattern`.
Pattern entries require `language` and accept only an optional `glob`.
Invalid patterns and unsupported languages fail without a text-search fallback.

Each structural result carries `view`, `total_matches` (all matches in scope, including any beyond the retention cap), and `files_matched`.
Ordering is always deterministic: by path, then by start byte, then by end byte.

The default `view` is `"matches"`.
Its `items` are file groups sorted by path: `{"path": "src/a.rs", "matches": [[start_line, end_line, start_byte, end_byte], ...]}`.
A match with captures has a fifth element, an object that maps each capture name to a list of the same four-number ranges.
Byte offsets are zero-based and half-open. Line numbers are one-based.
The end line identifies the exclusive end position, including the next line when the range ends after a newline.
Multi-captures retain matched punctuation, such as commas, in source order.
Results contain owned ranges, not source text or borrowed syntax nodes.

When the response exceeds the budget, a structural result steps down the first of these tiers that fits.
Every step sets `completeness: "partial"` and `budget_limited: true`, and adds a `note`.

1. One matched file: `items` keeps the longest prefix of that file's matches that fits, and `shown` gives its length. `view` stays `"matches"`.
2. `view: "files"`: `files` is `[[path, count], ...]` sorted by path. It covers every matching file, and counts include matches beyond the retention cap. This tier applies only to 100 or fewer matching files; a longer list is not actionable, so it skips to tier 3. When a listed path has no `/`, the note adds "Prefix a top-level file with / to match only that file."
3. `view: "directories"`: `directories` is `[[dir, count, files], ...]` sorted by directory. Directories group at the smallest depth of at least 1 that yields two or more groups, and root-level files group as `"."`. `top_files` lists up to 10 `[path, count]` pairs, largest count first, then by path.
4. `view: "none"`: the listing is removed. Only the totals, `budget_limited`, and a `note` that says to raise the budget or narrow with `glob` are left.

To narrow a trimmed result, rerun the entry with `glob` set to a listed path or to `"<dir>/**"`.
A path that contains a `/` matches exactly that file.
A root-level file name has no `/`, so a bare name also matches same-named files in subdirectories; prefix it with `/` (for example `"/main.rs"`) to match only the root file.
Paths that contain glob metacharacters (`*`, `?`, `[`, `]`, `{`, `}`, `\`) are treated as patterns, not literal paths, so they cannot be used as an exact glob.

Structural search uses the same scope, ignore, secret-file, and response-budget policies as other search entries.
Matching reuses cached source and trees.
Each distinct language/pattern pair compiles once per request.
The scan retains at most 1,000 matches, sets `match_limited` with a `note` when more exist, and marks limited or skipped-file scans as partial.
Files above the existing 500,000-byte parse limit are skipped.
Skipped files are counted in `skipped_files`.

### Session dedup

In MCP mode, previously expanded definitions show `[shown earlier]` instead of the full body on subsequent searches. Saves tokens when the agent revisits symbols it already saw.

## Benchmarks

Code navigation tasks across 4 real-world repos (Express, FastAPI, Gin, ripgrep). Baseline = Claude Code built-in tools. tilth = built-in tools + tilth MCP server. We report **cost per correct answer** (`total_spend / correct_answers`) — the expected cost under retry. See [benchmark/](benchmark/) for full methodology.

| Model | Tasks | Runs | Baseline $/correct | tilth $/correct | Change | Baseline acc | tilth acc |
|---|---|---|---|---|---|---|---|
| Sonnet 4.6 | 26 | 86 | $0.26 | $0.15 | **-44%** | 84% | 94% |
| Opus 4.6 | 26 | 25 | $0.22 | $0.14 | **-39%** | 91% | 92% |
| Haiku 4.5 | 26 | 49 | $0.12 | $0.08 | **-38%** | 54% | 73% |
| **Average** | | **160** | **$0.20** | **$0.12** | **-40%** | **76%** | **86%** |

v0.5.0 introduces top-weighted MCP instructions and scope fallback, achieving 40% average cost reduction across all three models. Sonnet accuracy improves from 84% to 94%, Haiku from 54% to 73%. All models show significant turn reduction (25% average fewer turns).

Scope confusion (models passing invalid directory paths) is now handled with automatic fallback to cwd with a warning. DO NOT rules at the top of MCP instructions reduced redundant built-in tool usage (Grep, Read, Glob) to near-zero across all models.

See [benchmark/](benchmark/) for per-task results, by-language breakdowns, and model comparison.

## Why

I built this because I watched AI agents make 6 tool calls to find one function. `glob → read → "too big" → grep → read again → read another file`. Each round-trip burns tokens and inference time.

tilth gives structural awareness in one call. The outline tells you *what's in the file*. The search tells you *where things are defined*. `--section` gets you *exactly the lines you need*.

## Install

```bash
cargo install tilth
# or
npx tilth
```

Prebuilt binaries on the [releases page](https://github.com/jahala/tilth/releases).

### MCP server

```bash
tilth install claude-code      # ~/.claude.json
tilth install cursor           # ~/.cursor/mcp.json
tilth install windsurf         # ~/.codeium/windsurf/mcp_config.json
tilth install vscode           # .vscode/mcp.json (project scope)
tilth install claude-desktop
tilth install opencode         # ~/.config/opencode/opencode.json
tilth install gemini           # ~/.gemini/settings.json
tilth install codex            # ~/.codex/config.toml
tilth install amp              # ~/.config/amp/settings.json
tilth install droid            # ~/.factory/mcp.json
tilth install antigravity      # ~/.gemini/antigravity/mcp_config.json
tilth install zed              # ~/.config/zed/settings.json
tilth install copilot-cli      # ~/.copilot/mcp-config.json
tilth install augment          # ~/.augment/settings.json
tilth install kiro             # ~/.kiro/settings/mcp.json
tilth install kilo-code        # VS Code globalStorage (extension)
tilth install cline            # VS Code globalStorage (extension)
tilth install roo-code         # VS Code globalStorage (extension)
tilth install trae             # .trae/mcp.json (project scope)
tilth install qwen-code        # ~/.qwen/settings.json
tilth install crush            # ~/.config/crush/crush.json
tilth install pi               # ~/.pi/agent/mcp.json
```

Every install includes tag-anchored file editing (see [Edit mode](#edit-mode)). The old `--edit` flag is still accepted and does nothing.

Or call it from bash — see [AGENTS.md](./AGENTS.md) for the MCP agent prompt, or [skills/SKILL.md](./skills/SKILL.md) for a Claude Code skill prompt.

### Smaller models

Smaller models (e.g. Haiku) may ignore tilth tools in favor of built-in Bash/Grep. To force tilth adoption, disable the overlapping built-in tools:

```bash
claude --disallowedTools "Bash,Grep,Glob"
```

Benchmarks show Haiku benefits significantly from tilth (54% → 73% accuracy) but may still fall back to built-in tools. Forced mode ensures consistent tool adoption.

### Tool gating

Descriptions alone do not flip write routing: in production telemetry across three model tiers, agents given a free choice preferred the host editor 3–9× over `tilth_write` (Sonnet: 10% tilth share), while the same tier ran 93% through tilth once the agent definition removed the host edit tools. Gate tools instead of prompting for them — in Claude Code subagents, allowlist tools and omit `Edit`/`Write`/`Grep`/`Glob`, keeping `Bash` for tests and builds:

```yaml
# .claude/agents/coder.md frontmatter
tools: Bash, Read, ToolSearch, mcp__tilth
```

For headless runs, `--disallowedTools` (above) is the equivalent lever.

### Bash guard hook

`scripts/tilth-bash-guard` routes project content reads from leading `grep`/`rg`/`cat` commands to `tilth_search`/`tilth_read`.
Shell `ls` and `find` allow directory browsing.
Project input reads, output redirects, and `find` execution or write actions remain guarded.
Wrapper forms such as `bash -c` are not inspected.
Pipe filters (`cargo test | grep foo`) and paths outside the project pass through.
Run `python3 scripts/tilth-bash-guard --self-test` before installation.
Install the hook in a tilth-enabled project's `.claude/settings.json`.
Review the script before installation, especially on untrusted branches:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {"type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR\"/scripts/tilth-bash-guard"}
        ]
      }
    ]
  }
}
```

## How it decides what to show

| Input | Behaviour |
|-------|-----------|
| 0 bytes | `[empty]` |
| Binary | `[skipped]` with mime type |
| Generated (lockfiles, .min.js) | `[generated]` |
| < ~6000 tokens | Full content with line numbers |
| > ~6000 tokens | Structural outline with line ranges |

Token-based, not line-based — a 1-line minified bundle gets outlined; a 120-line focused module prints whole.

## Edit mode

The MCP server always registers `tilth_write`. `tilth_read` prints a `[path#TAG]` header over 1-based numbered lines. `tilth_search` shows `path#TAG` in match headers too, so lines a search shows are editable with no read:

```
[src/auth.ts#1A2B]
42:  let x = compute();
43:  return x;
```

`tilth_write` takes a JSON `edits` array of `{path, tag, ops}` sections. Copy the TAG verbatim — it binds the edit to the content you saw. Every edit must fall on lines a read or search showed. If the file changed since, tilth 3-way-merges your ops onto the live file and rejects the section only when the merge conflicts:

```json
{"path": "src/auth.ts", "tag": "1A2B", "ops": [
  {"op": "replace_text", "old": "compute()", "new": "recompute()", "all": true},
  {"op": "rewrite", "pattern": "$R.Render($W)", "rewrite": "$R.Render(ctx, $W)"}
]}
```

`replace_text` matches one exact string; `all: true` replaces every match and `count: N` requires exactly N. `rewrite` replaces outermost, non-overlapping ast-grep matches in the file's language. Nested matches inside a selected match are not rewritten, and `count: N` counts only selected matches.

Large files still outline first — read a `path#n-m` section to get numbered content for the part you need.

Inspired by [The Harness Problem](https://blog.can.ac/2026/02/12/the-harness-problem/).

## Usage

```bash
tilth <path>                      # read file (outline if large)
tilth <path> --section 45-89      # exact line range
tilth <path> --section "## Foo"   # markdown heading
tilth <path> --full               # force full content
tilth <symbol> --scope <dir>      # definitions + usages
tilth <symbol> --expand=5         # inline source for top 5 matches
tilth <symbol> --callers          # find call sites (structural)
tilth "TODO: fix" --scope <dir>   # content search
tilth "/<regex>/" --scope <dir>   # regex search
tilth "*.test.ts" --scope <dir>   # glob files
```

Use shell `git diff` and `git log` for change review, and `ls` or `find` to browse directories.

## Speed

CLI times on x86_64 Mac, 26–1060 file codebases. Includes ~17ms process startup (MCP mode pays this once).

| Operation | ~30 files | ~1000 files |
|-----------|-----------|-------------|
| File read + type detect | ~18ms | ~18ms |
| Code outline (400 lines) | ~18ms | ~18ms |
| Symbol search | ~27ms | — |
| Content search | ~26ms | — |
| Glob | ~24ms | — |
| Map (codebase skeleton) | ~21ms | ~240ms |

Search, content search, and glob use early termination — time is roughly constant regardless of codebase size.

## What's inside

Rust. ~20,000 lines. No runtime dependencies.

- **tree-sitter** — AST parsing for 17 languages (Rust, TypeScript, TSX, JavaScript, Python, Go, Java, Scala, C, C++, Ruby, PHP, Swift, Kotlin, C#, Elixir, Bash). Used for definition detection, callee extraction, callers query, and structural outlines.
- **ast-grep** (`ast-grep-core`, `ast-grep-language`) — structural pattern search over the cached tree-sitter trees, with metavariables (`$A`, `$$$ARGS`) for every language above. PHP patterns parse with the same full grammar as PHP documents, so templates with inline HTML match too.
- **ripgrep internals** (`grep-regex`, `grep-searcher`) — fast content search
- **ignore** crate — parallel directory walking, searches all files including gitignored
- **memmap2** — memory-mapped file reads (no buffers)
- **clru** — bounded LRU outline cache, invalidated by file revision

Search runs definitions and usages in parallel via `rayon::join`. Callee resolution runs at expand time — extract callee names via tree-sitter queries, resolve against the source file's outline and imported files. Callers query uses the same tree-sitter patterns in reverse, walking the codebase with `memchr` SIMD pre-filtering for fast elimination.

The search output format is informed by wavelet multi-resolution (outline headers show line ranges for drill-down) and 1-hop callee expansion (expanded definitions resolve callees inline).

## Name

**tilth** — the state of soil that's been prepared for planting. Your codebase is the soil; tilth gives it structure so you can find where to dig.

## Support

[!["Buy Me A Coffee"](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/jahala)

## License

MIT
