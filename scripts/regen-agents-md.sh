#!/usr/bin/env bash
# Regenerate AGENTS.md from prompts/mcp-base.md + prompts/mcp-edit.md +
# prompts/mcp-v2-nudge.md.
#
# AGENTS.md is a generated artifact — edit the source files in prompts/, not
# AGENTS.md. The contents are also embedded into the MCP server at compile time
# via include_str! in src/mcp/mod.rs; running this script keeps the human-facing
# AGENTS.md in lockstep with what MCP hosts receive in the `instructions` field.
#
# Idempotent: running twice produces no diff.
set -euo pipefail

cd "$(dirname "$0")/.."

base="prompts/mcp-base.md"
edit="prompts/mcp-edit.md"
nudge="prompts/mcp-v2-nudge.md"
out="AGENTS.md"

for src in "$base" "$edit" "$nudge"; do
  if [[ ! -f $src ]]; then
    echo "missing prompt source: $src" >&2
    exit 1
  fi
done

# Render both mode files, clearly labeled, so AGENTS.md documents the
# mode-select served instructions (one complete file per mode — no
# concatenation) rather than a single composed prompt. The search-v2 nudge is
# rendered once more as its own section: the server splices it above ROUTE in
# either mode when `--search-surface v2|both`.
{
  printf '<!-- generated from prompts/mcp-base.md + prompts/mcp-edit.md + prompts/mcp-v2-nudge.md by scripts/regen-agents-md.sh — do not edit directly -->\n\n'
  printf '## Base mode\n\n'
  cat "$base"
  printf '\n\n## Edit mode\n\n'
  cat "$edit"
  printf '\n\n## Search-v2 surfaces\n\nSpliced above the ROUTE line in either mode when `--search-surface v2|both`:\n\n'
  cat "$nudge"
  printf '\n'
} > "$out"

echo "wrote $out ($(wc -c < "$out" | tr -d ' ') bytes)"
