# CLI

`tilth <QUERY>` runs one query and prints the result. tilth decides from the query if it is a file path, a symbol, text, a regex, or a glob.
Run `tilth --help` for the full flag list.

The samples on this page come from this repository. Long outputs are cut.

## Read a file

```bash
tilth src/install.rs
```

```
# /path/to/tilth/src/install.rs (2723 lines, ~26.1k tokens) [outline]

[1-]   imports: std::fs, std::path, serde_json
[31-54]      const SUPPORTED_HOSTS  // //   pi:             ~/.pi/agent/mcp.json                ...
[57-74]      fn tilth_server_entry
           fn tilth_server_entry(format: &ConfigFormat) -> Value  // The tilth server entry as JSON. Format depends on the hos...
[89-119]     fn run
           pub fn run(host: &str) -> Result<(), String>  // mode flag.
```

| Input | Result |
|---|---|
| Empty file | `[empty]` header |
| Binary file | Header with the MIME type |
| Generated file (lockfiles and similar) | `[generated — skipped]` header |
| Minified file | Header only |
| About 6,000 tokens or fewer | Whole file |
| More than about 6,000 tokens | Outline with line ranges |

The limit counts tokens, not lines. When an outline is not much smaller than the file, tilth prints the whole file.
When stdout is not a terminal, a file read prints the whole file, with these exceptions: a binary, generated, or minified file still gets its header only, and a file above the full-read size limit gets an outline with a warning.

### Sections

```bash
tilth src/launch.rs --section 63-65
tilth README.md --section "## Install"
```

A line range that ends inside a function grows to the end of that function. In the first example, `63-65` prints lines 63 to 93.

`--budget <N>` limits the response to about N tokens.

## Search

```bash
tilth print_preamble --scope src
```

```
# Search: "print_preamble" in /path/to/tilth/src — 2 matches (1 definitions, 1 usages)

### launch.rs:30-57 [definition]
-> [31-57]      fn print_preamble
-> [31]   pub fn print_preamble(path: bool) -> i32 {

### main.rs:191 [usage in function main]
-> [160-302]    fn main
190:            Command::Preamble { path } => {
-> 191:                process::exit(tilth::launch::print_preamble(path));
192:            }
```

Definitions come first. Each match shows the enclosing function or type.

### Expand

`--expand` prints the source of the top matches. A bare `--expand` means 2. `--expand=5` means 5.
An expanded definition ends with a `-- calls --` list of the functions it calls:

```
-- calls --
  ensure_preamble_file  launch.rs:157-174  fn ensure_preamble_file() -> io::Result<PathBuf>
    -> cache_dir  launch.rs:144-150  fn cache_dir(xdg: Option<OsString>, home: Option<PathBuf>) -> Option<PathBuf>
    -> preamble_name  launch.rs:152-155  fn preamble_name() -> String
```

`--full` on a search expands every match. An explicit `--expand=N` wins over `--full`.

### Several symbols

```bash
tilth "print_preamble, ensure_preamble_file" --scope src
```

A comma-separated list of identifiers gives one result block for each symbol.

### Callers

```bash
tilth print_preamble --callers --scope src
```

```
# Callers of "print_preamble" in /path/to/tilth/src — 1 call site

## main.rs:191 [caller: main]
-> process::exit(tilth::launch::print_preamble(path));
```

The callers query matches call sites in the syntax tree, not text.

### Text, regex, and glob

```bash
tilth "TODO: fix" --scope src      # text
tilth "/fn \w+_preamble/" --scope src   # regex
tilth "*.md" --scope prompts       # glob
```

```
# Glob: "*.md" in /path/to/tilth/prompts — 5 files
  preamble.md  (~745 tokens)
  mcp.md  (~393 tokens)
  tools/read.md  (~112 tokens)
  tools/write.md  (~465 tokens)
  tools/search.md  (~158 tokens)
```

## Flags

| Flag | Effect |
|---|---|
| `--scope <DIR>` | Directory to search in. The default is `.`. |
| `--section <RANGE>` | Line range or markdown heading. |
| `--budget <N>` | Limit the response to about N tokens. |
| `--full` | Whole file for a read. Expand every match for a search. |
| `--expand[=N]` | Inline source for the top N matches. |
| `--callers` | Find call sites of a symbol. |
| `--glob <GLOB>` | Filter files, for example `"*.rs"` or `"!*.test.ts"`. |
| `--json` | Wrap the output in JSON. |
| `--mcp` | Run the MCP server on stdio. |
| `--completions <SHELL>` | Print shell completions for bash, elvish, fish, powershell, or zsh. |

## Subcommands

| Command | Use |
|---|---|
| `tilth install <host> [--hook]` | Write MCP config. See [hosts.md](hosts.md). |
| `tilth hook` | Shell guard. See [hosts.md](hosts.md#shell-guard-hook). |
| `tilth claude ...` / `tilth codex ...` | Start the harness with the preamble. See the [README](../README.md#launcher-and-preamble). |
| `tilth preamble [--path] [--append <FILE>]` | Print the preamble or its cache path. `--append` adds the file's bytes after the bundled text. |

`claude`, `codex`, `preamble`, `install`, and `hook` are subcommands. To search for one of these words, run `tilth -- <word>`. Put other flags before `--`.

The fork has no `diff`, `overview`, `--map`, or `grok`. Use `git diff`, `git log`, `ls`, and `find`.
