# Go grouped declaration resolution

Go const and var declaration wrappers can define more than one name.
Definition search must inspect every spec name instead of relying only on the singular outline name.[^1]

## Decision

Keep `extract_definition_name` singular because outline and scope entries represent one declaration wrapper.
Use `go_declaration_name_line` in symbol search to scan every `name` field in nested Go specs.[^1]

Set `Match.line` to the matched identifier line.
Keep `def_range` on the enclosing declaration wrapper.[^2]
The matching line lets search deduplicate the textual usage on that line.
The wrapper range lets grok render the complete grouped declaration.

A grouped outline entry still uses its first name.
When grok resolves a later const or var name, it replaces that outline-derived name with the requested query.[^3]

## Regression boundary

Tests must include a later member of a grouped declaration and a later name in one comma-separated spec.[^4]
A complete check verifies three results:

1. Search returns one definition and no duplicate usage.
2. Grok resolves instead of returning not found.
3. The grok header uses the requested name.

## Failure modes

Using only `extract_definition_name` finds the first spec and misses later names.
Using the wrapper start line creates a second usage result for a later grouped member.
Using the outline name unchanged makes `grok StatusInactive` render as `StatusActive`.

[^1]: src/lang/treesitter.rs:107-153
[^2]: src/search/symbol.rs:347-376
[^3]: src/search/grok.rs:385-423
[^4]: src/search/symbol.rs:855-889; src/search/grok.rs:1605-1617
