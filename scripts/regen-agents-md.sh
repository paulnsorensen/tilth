#!/usr/bin/env bash
# Regenerate AGENTS.md from prompts/mcp.md.
#
# AGENTS.md is a generated artifact — edit the source file in prompts/, not
# AGENTS.md. The contents are also embedded into the MCP server at compile time
# via include_str! in src/mcp/mod.rs; running this script keeps the human-facing
# AGENTS.md in lockstep with what MCP hosts receive in the `instructions` field.
#
# Idempotent: running twice produces no diff.
set -euo pipefail

cd "$(dirname "$0")/.."

src="prompts/mcp.md"
out="AGENTS.md"

if [[ ! -f $src ]]; then
  echo "missing prompt source: $src" >&2
  exit 1
fi

{
  printf '<!-- generated from prompts/mcp.md by scripts/regen-agents-md.sh — do not edit directly -->\n\n'
  cat "$src"
  printf '\n'
} > "$out"

echo "wrote $out ($(wc -c < "$out" | tr -d ' ') bytes)"
