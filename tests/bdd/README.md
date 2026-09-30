# Cucumber prototype

This suite tests the compiled `tilth` binary through its MCP stdio interface.
It adds coverage beside the existing Rust and Python suites.

## Run

From the checkout root:

```sh
cargo test --test bdd
cargo test edit
cargo test --test bdd -- --name "Search reflects"
```

A normal `cargo test` also runs these scenarios.
Cargo positional filters match literal substrings in scenario names.
Explicit Cucumber `--name` or `--tags` filters take priority over a positional filter.
Cargo supplies `CARGO_BIN_EXE_tilth`, so the runner uses the current build.
Use `cargo test --test bdd -- --help` for Cucumber filters.
Do not pass libtest flags such as `--nocapture` to this custom runner.

## Add a scenario

1. Add a scenario to `features/edit.feature`, or create another `.feature` file under `features/`.
2. Use `Given an example workspace` to copy the fixture and start a new MCP session.
3. Reuse the steps in `main.rs`.
4. Add a step only when the behavior needs a new operation.
5. Put expected definitions and complete file content in Gherkin docstrings.
6. Run the new scenario by name, then run the complete suite.

The fixture is a small Rust project under `fixtures/project/`.
Paths in steps are relative to its isolated copy.
The file-content step expects one final newline after its docstring.
Search checks inspect the result target and definition, not the echoed query.

Each scenario owns one temporary workspace, cache, stderr log, and live process.
Read tags come from that process and return to the same process for writes.
The process worker gives each request a 15-second deadline, including stdin and stdout operations.
Failures show the last request, response, and stderr.
Cleanup kills and reaps the process, joins the worker, and removes temporary files.
Scenarios run serially by default.
Missing or skipped steps fail, including steps tagged `@allow.skipped`.

The scenarios cover a search-edit-search workflow, an independent external change, and a conflicting external change.
The conflict scenario compares the final bytes with both the pre-write bytes and the expected file.
This prototype does not replace the existing suites or add a general MCP client.

Runner API: [Cucumber Rust](https://docs.rs/cucumber/latest/cucumber/).

## Language and history coverage

`languages.feature` owns the fixed 39-filename matrix: 29 extensions and 10 exact filenames across 19 languages. The 35 grammar-backed rows require structural symbols. The four Docker and Make rows require literal matches from valid native syntax.

`history.feature` records each regression family with commit or pull-request tags. Expected paths, canonical lines, names, ambiguity, and bodies are fixture literals. Tests do not read the production registry or parser tables to build an oracle.

`upstream.feature` covers reported Python caller loss, Ruby declarations, C++ ownership and operators, and JavaScript-to-TypeScript import resolution. Caller checks compare exact locations on cold and warm requests. Dependency checks compare resolved paths and existing reverse call-site dependents. These regressions use the existing grammars and filename registry.

A leading vertical bar in an expected docstring preserves source indentation. The step implementation removes only that marker before exact comparison.

## Unit migration parity

Eleven candidate unit tests are fully migrated. `deeply_nested_definitions_detected` is split because raw definition counts have no public MCP equivalent.

| Candidate unit function | Original assertions | Migration disposition |
| --- | --- | --- |
| `resolve_qualified_target_strips_type_prefix` | Both separator forms resolve name `dispatch`; each has zero other definitions. | “Rust qualified targets accept both separator forms” checks both public grok headers, bodies, and absence of ambiguity. |
| `qualified_nested_wrapped_classes_keep_the_real_owner` | Python and TypeScript qualified targets retain the inner class name. | “Nested wrapped classes keep their real owners” checks exact public grok names, paths, canonical lines, and bodies. The TypeScript wrapper duplicate remains visible because the removed unit did not assert its ambiguity count. |
| `resolve_qualified_target_ambiguous_segment_resolves_named_owner` | Alpha and Beta resolve name `dispatch`, select their owner files, and report zero other definitions. | “Qualified Rust owner removes same-name ambiguity” checks both candidates, then exact Alpha and Beta grok targets without ambiguity. |
| `resolve_qualified_target_typescript_class_method` | Name is `dispatch`; path is `beta.ts`. | “Qualified TypeScript owner selects the requested method” checks name, path, line, body, and ambiguity. |
| `resolve_qualified_target_python_class_method` | Name is `dispatch`; path is `alpha.py`. | “Qualified Python owner selects the requested method” checks name, path, line, body, and ambiguity. |
| `resolve_qualified_target_go_receiver_type` | Name is `Bar`; receiver owner selects `foo.go`. | “Qualified Go owner selects the requested receiver” checks name, path, line, body, and ambiguity. |
| `resolve_go_grouped_and_multi_name_declarations_use_query_name` | Returned names equal `StatusInactive` and `CounterB`. | “Grouped and multi-name Go declarations retain the query name” checks both public grok headers and bodies. |
| `deeply_nested_definitions_detected` | TypeScript and Rust each return exactly one raw definition, set `is_definition`, and use lines 2 and 3. | The two “Deep …” scenarios check public classification, path, line, and body. A focused unit test retains exact raw-count, line, and definition invariants because MCP deduplicates overlapping definitions. |
| `elixir_definitions_detected` | The dotted module is a definition; public function, private function, and macro block forms are non-empty definitions. | The Elixir scenario checks the dotted module through grok, block-form `hello` and `my_macro`, and keyword-form `private_helper` through exact structural search. |
| `elixir_guard_clause_definitions` | Guarded public block form, guarded private keyword form, and `defguard` are non-empty definitions. | The Elixir scenario checks block-form `safe_div`, keyword-form private `checked`, and `is_positive` as exact structural results. |
| `elixir_delegate_and_nested_modules` | Delegate and nested module are non-empty definitions. | The Elixir scenario checks `count` and `Inner` as exact structural results. |
| `symbol_read_resolves_doubly_nested_definition` | Private range resolution returns lines 3–5. | “Deep Rust definitions are searchable and readable by symbol” compares exactly the three numbered source lines. |

## Retained private invariants

The migration retains tests for these internal properties:

- registry completeness and case-sensitive alias classification;
- query-cache keys and grammar-query reuse;
- TypeScript constant definition ranges;
- Go weights, byte ranges, and grouped-name occurrence identity;
- pathological nesting depth guards and deep call-site rejection;
- raw TypeScript and Rust definition uniqueness before MCP deduplication;
- same-line ambiguity and semantic-span fields;
- `.mjs` and `.cjs` strip-only compatibility behavior.

These properties have no stable public MCP equivalent.
