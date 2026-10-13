# tilth API adoption: replace_text, create_file, list overview (history)

Spec approved 2026-08-09 and shipped in #177 (`e8f8d9b`). `replace_text` and `create_file` are live `tilth_write` ops.
The patternless `tilth_list` overview is retired: #282 (`d88bdf0`) removes `tilth_list`.
This page is a decision record, not live guidance.

## Problem

Agents went around the tilth API where it fought their trained shapes or lacked an affordance. Instruction bans alone decayed.

- Haiku used Bash in 4/10 fork benchmark runs vs 0/10 upstream. Most leaks were repo orientation with `find` or `ls`.
- Sonnet 5 fell back to host Edit in fork edit tasks.
- 23 production `tilth_write` calls failed because they invented tag-less create ops (`seed`, `write`, `create`).

## Proposal

1. `replace_text {old, new}`: host-Edit substring semantics inside the tag contract. `old` must match once. The op expands to whole lines and lowers to `LineOp::Swap` before any gate runs.
2. `create_file {content}`: a named file op beside `delete_file` and `move_file`, for tag-less sections only.
3. A patternless `tilth_list` call returns a project overview, not the full tree.
4. Keep the `--mcp --edit` surface at or below 13,779 characters.

## Decisions

- Substring match over whole-line match, for trained-shape alignment. Both fail closed (ADR-001).
- No op pruning. Production use showed block ops in 11 projects and 1,662 insert uses (ADR-002).
- The character cap never yields (ADR-003).
- No scratch-file affordance. Answer inline.

## What shipped

- `Op::TextSwap` (`src/edit/parser.rs:66`), lowered by `lower_text_swaps` (`src/edit/apply.rs:491`).
- `Op::Create`, serialized as `create_file` (`src/edit/json.rs:480`).
- Op-kind telemetry (F001, #173 closed): `benchmark/parse.py:577` records `op_kinds` per result row.
- Block-op teaching line (F002): #174 closed 2026-08-09.

## What did not survive

- The patternless overview. #282 retires `tilth_list`. `src/overview.rs` is also gone (CLAUDE.md "Fork law").

## Follow-ups

- #173 (op-kind telemetry): closed, shipped by #178 (`7d527a4`).
- #174 (block-op teaching line): closed.
- #175 (`replace_all` escape hatch): still OPEN on GitHub. #308 (`f236f77`) ships the behavior: `Op::TextSwapAll` with `all` and `count` (`src/edit/parser.rs:69`). The issue looks ready to close.

## Successor

None. The ops are live. Benchmark evidence lives in `op_kinds` rows.

_Source: former `.cheese/specs/tilth-api-adoption.md`; commits `e8f8d9b` (#177), `7d527a4` (#178), `d88bdf0` (#282), `f236f77` (#308); issues #173-#175 · Updated: 2026-10-13_
