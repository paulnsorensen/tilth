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
