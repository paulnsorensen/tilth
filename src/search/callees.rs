use std::collections::HashSet;
use std::path::{Path, PathBuf};

use crate::lang::outline::{get_outline_entries, outline_language};
use crate::types::{Lang, OutlineEntry};

/// A resolved callee: a function/method called from within an expanded definition.
#[derive(Debug, Clone)]
pub struct ResolvedCallee {
    pub name: String,
    pub file: PathBuf,
    /// Canonical declaration anchor used for display and identity.
    pub start_line: u32,
    /// Semantic ownership start used when slicing the callee source.
    pub span_start_line: u32,
    pub end_line: u32,
    pub signature: Option<String>,
}

/// A resolved callee with its own callees (2nd hop).
#[derive(Debug)]
pub struct ResolvedCalleeNode {
    pub callee: ResolvedCallee,
    /// 2nd-hop callees resolved from within this callee's body.
    pub children: Vec<ResolvedCallee>,
}

/// Extract names of functions/methods called within a given line range.
/// Uses tree-sitter query patterns to find call expressions.
///
/// If `def_range` is `Some((start, end))`, only callees whose match position
/// falls within lines `start..=end` (1-indexed) are returned.
/// Returns a deduplicated, sorted list of callee names.
pub fn extract_callee_names(
    content: &str,
    lang: Lang,
    def_range: Option<(u32, u32)>,
) -> Vec<String> {
    let Some(language) = outline_language(lang) else {
        return Vec::new();
    };
    let Some(tree) = crate::lang::treesitter::parse_source(content, &language) else {
        return Vec::new();
    };
    extract_callee_names_from_tree(content, lang, &tree, def_range)
}

pub(crate) fn extract_callee_names_from_tree(
    content: &str,
    lang: Lang,
    tree: &tree_sitter::Tree,
    def_range: Option<(u32, u32)>,
) -> Vec<String> {
    let Some(ts_lang) = outline_language(lang) else {
        return Vec::new();
    };
    let Some(query_str) = super::callee_query::callee_query_str(lang) else {
        return Vec::new();
    };
    let content_bytes = content.as_bytes();

    let mut names: Vec<String> = crate::lang::treesitter::query_captures(
        &ts_lang,
        query_str,
        tree.root_node(),
        content_bytes,
        ["callee"],
    )
    .into_iter()
    .filter_map(|[callee]| callee)
    .filter(|callee| {
        let line = callee.start_position().row as u32 + 1;
        def_range.is_none_or(|(start, end)| (start..=end).contains(&line))
    })
    .filter_map(|callee| callee.utf8_text(content_bytes).ok().map(str::to_owned))
    .collect();
    names.sort();
    names.dedup();
    names.retain(|name| (crate::lang::spec::spec(lang).policy.callee_allowed)(name));

    names
}

/// Match callee names against outline entries, moving resolved names out of `remaining`.
fn resolve_from_entries(
    entries: &[OutlineEntry],
    file_path: &Path,
    remaining: &mut std::collections::HashSet<&str>,
    resolved: &mut Vec<ResolvedCallee>,
) {
    for entry in entries {
        // Check top-level entry name
        if remaining.contains(entry.name.as_str()) {
            remaining.remove(entry.name.as_str());
            resolved.push(ResolvedCallee {
                name: entry.name.clone(),
                file: file_path.to_path_buf(),
                start_line: entry.start_line,
                span_start_line: entry.span_start_line,
                end_line: entry.end_line,
                signature: entry.signature.clone(),
            });
        }

        // Check children (methods in classes/impl blocks)
        for child in &entry.children {
            if remaining.contains(child.name.as_str()) {
                remaining.remove(child.name.as_str());
                resolved.push(ResolvedCallee {
                    name: child.name.clone(),
                    file: file_path.to_path_buf(),
                    start_line: child.start_line,
                    span_start_line: child.span_start_line,
                    end_line: child.end_line,
                    signature: child.signature.clone(),
                });
            }
        }

        if remaining.is_empty() {
            return;
        }
    }
}

/// Resolve callee names to their definition locations.
///
/// Strategy: check the source file's own outline first (cheapest), then scan
/// imported files resolved from the source's import statements.
pub fn resolve_callees(
    callee_names: &[String],
    source_path: &Path,
    source_content: &str,
    bloom: &crate::index::bloom::BloomFilterCache,
) -> Vec<ResolvedCallee> {
    resolve_callees_cached(
        callee_names,
        source_path,
        source_content,
        bloom,
        &crate::cache::OutlineCache::new(),
    )
}

pub(crate) fn resolve_callees_cached(
    callee_names: &[String],
    source_path: &Path,
    source_content: &str,
    bloom: &crate::index::bloom::BloomFilterCache,
    cache: &crate::cache::OutlineCache,
) -> Vec<ResolvedCallee> {
    if callee_names.is_empty() {
        return Vec::new();
    }

    let file_type = crate::lang::detect_file_type(source_path);
    let crate::types::FileType::Code(lang) = file_type else {
        return Vec::new();
    };

    let mut remaining: std::collections::HashSet<&str> =
        callee_names.iter().map(String::as_str).collect();
    let mut resolved = Vec::new();

    // 1. Check source file's own outline entries
    let entries = cache.parse_source(source_path, source_content).map_or_else(
        || get_outline_entries(source_content, lang),
        |parsed| parsed.outline_entries(),
    );
    resolve_from_entries(&entries, source_path, &mut remaining, &mut resolved);

    if remaining.is_empty() {
        return resolved;
    }

    // 2. Check imported files
    let imported =
        crate::read::imports::resolve_related_files_with_content(source_path, source_content);

    for import_path in imported {
        if remaining.is_empty() {
            break;
        }

        // Read + bloom prefilter via shared helper. Skip the file when no
        // remaining symbol is bloom-positive.
        let super::bloom_walk::BloomRead::Hit {
            content: import_content,
            ..
        } = super::bloom_walk::read_with_bloom_check(
            &import_path,
            remaining.iter().copied(),
            bloom,
            super::bloom_walk::MAX_FILE_SIZE,
        )
        else {
            continue;
        };

        let import_type = crate::lang::detect_file_type(&import_path);
        let crate::types::FileType::Code(import_lang) = import_type else {
            continue;
        };

        let import_entries = cache
            .parse_source(&import_path, &import_content)
            .map_or_else(
                || get_outline_entries(&import_content, import_lang),
                |parsed| parsed.outline_entries(),
            );
        resolve_from_entries(&import_entries, &import_path, &mut remaining, &mut resolved);
    }

    if remaining.is_empty() {
        return resolved;
    }

    // 3. Scan same-package files when the language defines a directory namespace.
    if let Some(policy) = crate::lang::spec::spec(lang).policy.same_package {
        resolve_same_package(
            &mut remaining,
            &mut resolved,
            source_path,
            lang,
            policy,
            cache,
        );
    }

    resolved
}

/// Go same-package resolution: scan .go files in the same directory.
///
/// Go packages are directory-scoped — all .go files in a directory share the
/// same namespace without explicit imports. This resolves callees like
/// `safeInt8` in `context.go` that are defined in `utils.go`.
fn resolve_same_package(
    remaining: &mut std::collections::HashSet<&str>,
    resolved: &mut Vec<ResolvedCallee>,
    source_path: &Path,
    lang: Lang,
    policy: crate::lang::spec::SamePackagePolicy,
    cache: &crate::cache::OutlineCache,
) {
    let Some(dir) = source_path.parent() else {
        return;
    };

    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };

    // Collect eligible package files, sorted for deterministic order.
    let suffix = format!(".{}", policy.extension);
    let mut package_files: Vec<PathBuf> = entries
        .filter_map(Result::ok)
        .filter(|e| {
            let path = e.path();
            let name = e.file_name();
            let name_str = name.to_string_lossy();
            path != source_path
                && name_str.ends_with(&suffix)
                && !name_str.ends_with(policy.excluded_suffix)
                && e.metadata()
                    .is_ok_and(|metadata| metadata.len() <= policy.max_file_size)
        })
        .map(|e| e.path())
        .collect();

    package_files.sort();
    package_files.truncate(policy.max_files);

    for package_path in package_files {
        if remaining.is_empty() {
            break;
        }

        let Ok(content) = std::fs::read_to_string(&package_path) else {
            continue;
        };

        let outline = cache.parse_source(&package_path, &content).map_or_else(
            || get_outline_entries(&content, lang),
            |parsed| parsed.outline_entries(),
        );
        resolve_from_entries(&outline, &package_path, remaining, resolved);
    }
}

/// Resolve callees transitively up to `depth_limit` hops with budget cap.
///
/// First hop uses `resolve_callees()` on the source content. For each resolved
/// callee at depth < `depth_limit`, reads the callee's file, extracts nested
/// callee names from the definition range, and resolves them as children.
///
/// `budget` caps the total number of 2nd-hop (child) callees across all parents.
/// Cycle detection prevents infinite loops via `(file, start_line)` tracking.
pub(crate) fn resolve_callees_transitive(
    initial_names: &[String],
    source_path: &Path,
    source_content: &str,
    bloom: &crate::index::bloom::BloomFilterCache,
    depth_limit: u32,
    budget: usize,
    cache: &crate::cache::OutlineCache,
) -> Vec<ResolvedCalleeNode> {
    // 1st hop: resolve direct callees (existing logic)
    let first_hop =
        resolve_callees_cached(initial_names, source_path, source_content, bloom, cache);

    if depth_limit < 2 || first_hop.is_empty() {
        return first_hop
            .into_iter()
            .map(|c| ResolvedCalleeNode {
                callee: c,
                children: Vec::new(),
            })
            .collect();
    }

    // Cycle detection: track visited (file, start_line) pairs
    let mut visited: HashSet<(PathBuf, u32)> = HashSet::new();

    // Mark all 1st-hop callees as visited
    for c in &first_hop {
        visited.insert((c.file.clone(), c.start_line));
    }

    let mut budget_remaining = budget;
    let mut result = Vec::with_capacity(first_hop.len());

    for parent in first_hop {
        let children = if budget_remaining > 0 {
            resolve_second_hop(&parent, bloom, &mut visited, &mut budget_remaining, cache)
        } else {
            Vec::new()
        };
        result.push(ResolvedCalleeNode {
            callee: parent,
            children,
        });
    }

    result
}

/// Resolve 2nd-hop callees for a single parent callee.
fn resolve_second_hop(
    parent: &ResolvedCallee,
    bloom: &crate::index::bloom::BloomFilterCache,
    visited: &mut HashSet<(PathBuf, u32)>,
    budget: &mut usize,
    cache: &crate::cache::OutlineCache,
) -> Vec<ResolvedCallee> {
    let file_type = crate::lang::detect_file_type(&parent.file);
    let crate::types::FileType::Code(lang) = file_type else {
        return Vec::new();
    };
    let Ok(content) = std::fs::read_to_string(&parent.file) else {
        return Vec::new();
    };

    let def_range = Some((parent.span_start_line, parent.end_line));
    let nested_names = cache.parse_source(&parent.file, &content).map_or_else(
        || extract_callee_names(&content, lang, def_range),
        |parsed| extract_callee_names_from_tree(parsed.content(), lang, parsed.tree(), def_range),
    );

    if nested_names.is_empty() {
        return Vec::new();
    }

    let mut resolved = resolve_callees_cached(&nested_names, &parent.file, &content, bloom, cache);

    // Filter: skip self-recursive calls and already-visited callees
    resolved.retain(|c| {
        let key = (c.file.clone(), c.start_line);
        // Skip if same definition as parent
        if c.file == parent.file && c.start_line == parent.start_line {
            return false;
        }
        // Skip if already visited (cycle detection)
        if visited.contains(&key) {
            return false;
        }
        true
    });

    // Apply budget cap
    if resolved.len() > *budget {
        resolved.truncate(*budget);
    }

    // Mark children as visited and decrement budget
    for c in &resolved {
        visited.insert((c.file.clone(), c.start_line));
    }
    *budget = budget.saturating_sub(resolved.len());

    resolved
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Every callee pattern of every language, as raw names before resolution.
    /// Most fixtures also read a member without a call; that name must not appear.
    /// Elixir parses `s.field` as a call, so its row expects `field`.
    /// Bash has no members, so its row checks that an assignment is not a call.
    #[test]
    fn callee_names_cover_every_query_pattern() {
        let cases: &[(Lang, &str, &[&str])] = &[
            (
                Lang::Rust,
                "fn f() { plain(); s.method(); Kind::scoped(); shout!(); s.field; }\n",
                &["method", "plain", "scoped", "shout"],
            ),
            (
                Lang::Python,
                "plain()\ns.method()\ns.field\n",
                &["method", "plain"],
            ),
            (
                Lang::Go,
                "package p\nfunc f() { plain(); s.method(); _ = s.field }\n",
                &["method", "plain"],
            ),
            (
                Lang::C,
                "void f(void) { plain(); s.field(); p->arrow(); s.member; p->pointee; }\n",
                &["arrow", "field", "plain"],
            ),
            (
                Lang::Cpp,
                "void f() { plain(); s.field(); this->arrow(); s.member; this->pointee; }\n",
                &["arrow", "field", "plain"],
            ),
            (
                Lang::CSharp,
                "class K { void F() { Plain(); s.Member(); var v = s.Field; } }\n",
                &["Member", "Plain"],
            ),
            (
                Lang::Java,
                "class K { void f() { plain(); s.member(); int v = s.field; } }\n",
                &["member", "plain"],
            ),
            (
                Lang::JavaScript,
                "plain();\ns.member();\ns.field;\n",
                &["member", "plain"],
            ),
            (
                Lang::TypeScript,
                "plain();\ns.member();\ns.field;\n",
                &["member", "plain"],
            ),
            (
                Lang::Tsx,
                "const v = <div>{plain() + s.member() + s.field}</div>;\n",
                &["member", "plain"],
            ),
            (
                Lang::Kotlin,
                "fun f() { plain(); s.member(); val v = s.field }\n",
                &["member", "plain"],
            ),
            (
                Lang::Php,
                "<?php\nplain();\n\\ns\\qualified();\nnamespace\\relative();\n$o->member();\n$o?->nullsafe();\nK::scoped();\n$o->field;\n",
                &[
                    "\\ns\\qualified",
                    "member",
                    "namespace\\relative",
                    "nullsafe",
                    "plain",
                    "scoped",
                ],
            ),
            (
                Lang::Ruby,
                "plain()\ns.member\nbare\n",
                &["member", "plain"],
            ),
            (
                Lang::Scala,
                "object O { plain(); s.member(); a infix b; val v = s.field }\n",
                &["infix", "member", "plain"],
            ),
            (
                Lang::Swift,
                "plain()\ns.member()\nprint(s.field)\n",
                &["member", "plain", "print"],
            ),
            (
                Lang::Elixir,
                "plain()\nMod.remote()\ns.field\n",
                &["field", "plain", "remote"],
            ),
            (Lang::Bash, "first arg\nsecond\nfield=1\n", &["first", "second"]),
        ];
        for (lang, source, expected) in cases {
            assert_eq!(
                extract_callee_names(source, *lang, None),
                *expected,
                "{lang:?}"
            );
        }
    }

    #[test]
    fn extract_kotlin_callee_names() {
        let kotlin = r#"fun example() {
    println("hello")
    val x = listOf(1, 2, 3)
    x.forEach { it.toString() }
}
"#;
        let names = extract_callee_names(kotlin, crate::types::Lang::Kotlin, None);

        assert!(
            names.contains(&"println".to_string()),
            "expected println, got: {names:?}"
        );
        assert!(
            names.contains(&"listOf".to_string()),
            "expected listOf, got: {names:?}"
        );
        assert!(
            names.contains(&"forEach".to_string()),
            "expected forEach, got: {names:?}"
        );
        assert!(
            names.contains(&"toString".to_string()),
            "expected toString, got: {names:?}"
        );
    }

    #[test]
    fn extract_php_callee_names() {
        let php = r"<?php
function run($svc): void {
    local_helper();
    Foo\Bar::staticCall();
    $svc->methodCall();
    $svc?->nullableCall();
}
";

        let names = extract_callee_names(php, Lang::Php, None);

        assert!(names.contains(&"local_helper".to_string()));
        assert!(names.contains(&"staticCall".to_string()));
        assert!(names.contains(&"methodCall".to_string()));
        assert!(names.contains(&"nullableCall".to_string()));
    }

    #[test]
    fn extract_elixir_callee_names() {
        let elixir = r#"defmodule Example do
  def run(conn) do
    result = query(conn, "SELECT 1")
    Enum.map(result, &to_string/1)
    IO.puts("done")
    local_func()
  end
end
"#;
        let names = extract_callee_names(elixir, Lang::Elixir, None);

        assert!(
            names.contains(&"query".to_string()),
            "expected query, got: {names:?}"
        );
        assert!(
            names.contains(&"map".to_string()),
            "expected map (from Enum.map), got: {names:?}"
        );
        assert!(
            names.contains(&"puts".to_string()),
            "expected puts (from IO.puts), got: {names:?}"
        );
        assert!(
            names.contains(&"local_func".to_string()),
            "expected local_func, got: {names:?}"
        );

        // Definition keywords must NOT appear as callees
        assert!(
            !names.contains(&"def".to_string()),
            "definition keyword 'def' should be filtered, got: {names:?}"
        );
        assert!(
            !names.contains(&"defmodule".to_string()),
            "definition keyword 'defmodule' should be filtered, got: {names:?}"
        );
    }

    #[test]
    fn extract_elixir_callee_names_pipes() {
        let elixir = r#"defmodule Pipes do
  def run(conn) do
    conn
    |> prepare("sql")
    |> execute()
    |> Enum.map(&transform/1)
  end
end
"#;
        let names = extract_callee_names(elixir, Lang::Elixir, None);

        // Pipe targets are regular call nodes — the callee query should find them
        assert!(
            names.contains(&"prepare".to_string()),
            "expected prepare from pipe, got: {names:?}"
        );
        assert!(
            names.contains(&"execute".to_string()),
            "expected execute from pipe, got: {names:?}"
        );
        assert!(
            names.contains(&"map".to_string()),
            "expected map from Enum.map pipe, got: {names:?}"
        );
    }

    #[test]
    fn go_same_package_uses_sorted_twenty_file_prefix_and_exclusions() {
        const EXPECTED_MAX_FILES: usize = 20;

        let tmp = tempfile::tempdir().unwrap();
        let source_path = tmp.path().join("main.go");
        let source_content = "package sample\n\nfunc caller() {}\n";
        std::fs::write(&source_path, source_content).unwrap();

        for index in (0..=EXPECTED_MAX_FILES).rev() {
            let function = format!("Func{index:02}");
            let content = format!("package sample\n\nfunc {function}() {{}}\n");
            std::fs::write(tmp.path().join(format!("file_{index:02}.go")), content).unwrap();
        }
        std::fs::write(
            tmp.path().join("excluded_test.go"),
            "package sample\n\nfunc ExcludedTest() {}\n",
        )
        .unwrap();
        std::fs::write(
            tmp.path().join("00_wrong.txt"),
            "package sample\n\nfunc WrongExtension() {}\n",
        )
        .unwrap();

        let mut names: Vec<String> = (0..=EXPECTED_MAX_FILES)
            .map(|index| format!("Func{index:02}"))
            .collect();
        names.extend(["ExcludedTest".to_string(), "WrongExtension".to_string()]);

        let resolved = resolve_callees(
            &names,
            &source_path,
            source_content,
            &crate::index::bloom::BloomFilterCache::new(),
        );
        let actual: Vec<(String, PathBuf)> = resolved
            .into_iter()
            .map(|callee| (callee.name, callee.file))
            .collect();
        let expected: Vec<(String, PathBuf)> = (0..EXPECTED_MAX_FILES)
            .map(|index| {
                (
                    format!("Func{index:02}"),
                    tmp.path().join(format!("file_{index:02}.go")),
                )
            })
            .collect();

        assert_eq!(actual, expected);
    }

    #[test]
    fn go_same_package_accepts_100000_bytes_and_rejects_100001() {
        const EXPECTED_MAX_FILE_SIZE: usize = 100_000;

        fn sized_go_file(function: &str, size: usize) -> String {
            let mut content = format!("package sample\n\nfunc {function}() {{}}\n");
            assert!(content.len() <= size);
            content.push_str(&" ".repeat(size - content.len()));
            content
        }

        let tmp = tempfile::tempdir().unwrap();
        let source_path = tmp.path().join("main.go");
        let source_content = "package sample\n\nfunc caller() {}\n";
        std::fs::write(&source_path, source_content).unwrap();
        std::fs::write(
            tmp.path().join("at_limit.go"),
            sized_go_file("AtLimit", EXPECTED_MAX_FILE_SIZE),
        )
        .unwrap();
        std::fs::write(
            tmp.path().join("over_limit.go"),
            sized_go_file("OverLimit", EXPECTED_MAX_FILE_SIZE + 1),
        )
        .unwrap();

        let resolved = resolve_callees(
            &["AtLimit".to_string(), "OverLimit".to_string()],
            &source_path,
            source_content,
            &crate::index::bloom::BloomFilterCache::new(),
        );
        let actual: Vec<(String, PathBuf)> = resolved
            .into_iter()
            .map(|callee| (callee.name, callee.file))
            .collect();

        assert_eq!(
            actual,
            vec![("AtLimit".to_string(), tmp.path().join("at_limit.go"))]
        );
    }
}
