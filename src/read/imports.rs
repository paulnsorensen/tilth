//! Resolve import statements to local file paths.
//! Used by the MCP layer to hint related files after an outlined read.

use std::fs;
use std::path::{Path, PathBuf};

use crate::lang::detect_file_type;
use crate::types::{FileType, Lang};

const MAX_SUGGESTIONS: usize = 8;

/// Extract import sources from a code file and resolve them to existing local file paths.
/// Returns empty Vec for non-code files, files with no imports, or when all imports are external.
pub fn resolve_related_files(file_path: &Path) -> Vec<PathBuf> {
    let Ok(content) = fs::read_to_string(file_path) else {
        return Vec::new();
    };
    resolve_related_files_with_content(file_path, &content)
}

/// Same as `resolve_related_files` but takes pre-read content to avoid a redundant file read.
pub fn resolve_related_files_with_content(file_path: &Path, content: &str) -> Vec<PathBuf> {
    let FileType::Code(lang) = detect_file_type(file_path) else {
        return Vec::new();
    };

    let Some(dir) = file_path.parent() else {
        return Vec::new();
    };

    let mut results = Vec::new();
    for line in content.lines() {
        if results.len() >= MAX_SUGGESTIONS {
            break;
        }
        if !is_import_line(line, lang) {
            continue;
        }
        let source = crate::lang::outline::extract_import_source(line, Some(lang));
        if source.is_empty() || is_external(&source, lang) {
            continue;
        }
        if let Some(path) = resolve(dir, &source, lang) {
            if !results.contains(&path) {
                results.push(path);
            }
        }
    }
    results
}

pub(crate) fn is_import_line(line: &str, lang: Lang) -> bool {
    (crate::lang::spec::spec(lang).policy.import_line)(line)
}

pub(crate) fn is_external(source: &str, lang: Lang) -> bool {
    (crate::lang::spec::spec(lang).policy.import_external)(source)
}

fn resolve(dir: &Path, source: &str, lang: Lang) -> Option<PathBuf> {
    (crate::lang::spec::spec(lang).policy.import_resolver)(dir, source)
        .map(|path| normalize_path(&path))
}

/// Lexically collapse `.` and `..` components without touching the filesystem.
/// `dir.join("../foo")` returns a `PathBuf` containing literal `..`; without this,
/// distinct spellings of the same target file produce distinct `PathBuf`s and
/// downstream callers (dedup loops, `HashMap` keys) treat them as different files.
fn normalize_path(path: &Path) -> PathBuf {
    use std::path::Component;
    let mut out = PathBuf::new();
    for comp in path.components() {
        match comp {
            Component::CurDir => {}
            Component::ParentDir => {
                let pop_ok = matches!(
                    out.components().next_back(),
                    Some(Component::Normal(_) | Component::Prefix(_))
                );
                if pop_ok {
                    out.pop();
                } else {
                    out.push(comp);
                }
            }
            _ => out.push(comp),
        }
    }
    out
}

#[cfg(test)]
fn resolve_js(dir: &Path, source: &str) -> Option<PathBuf> {
    crate::lang::javascript::resolve_import(dir, source)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn normalize_collapses_dot_and_parent_components() {
        let p = Path::new("temporal/workflows/../utils/activityProxies.ts");
        assert_eq!(
            normalize_path(p),
            PathBuf::from("temporal/utils/activityProxies.ts")
        );
        let p = Path::new("app/db/./db.ts");
        assert_eq!(normalize_path(p), PathBuf::from("app/db/db.ts"));
    }

    #[test]
    fn normalize_preserves_leading_parent_when_unresolvable() {
        // No prior Normal component to pop, so ".." is kept.
        let p = Path::new("../outside.ts");
        assert_eq!(normalize_path(p), PathBuf::from("../outside.ts"));
    }

    #[test]
    fn js_resolve_returns_normalized_path_for_parent_import() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        fs::create_dir_all(root.join("temporal/workflows")).unwrap();
        fs::create_dir_all(root.join("temporal/utils")).unwrap();
        fs::write(root.join("temporal/utils/activityProxies.ts"), "").unwrap();

        let resolved = resolve_js(&root.join("temporal/workflows"), "../utils/activityProxies")
            .expect("should resolve");
        let normalized = normalize_path(&resolved);
        assert_eq!(normalized, root.join("temporal/utils/activityProxies.ts"));
        // No "../" component should survive normalization.
        assert!(
            !normalized
                .components()
                .any(|c| matches!(c, std::path::Component::ParentDir)),
            "normalized path still contains '..': {normalized:?}"
        );
    }

    #[test]
    fn js_resolve_dedups_different_spellings_of_same_file() {
        // Two importers of the same file via different relative paths must
        // produce the same PathBuf so that hot-file counting aggregates them.
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        fs::create_dir_all(root.join("a/b")).unwrap();
        fs::create_dir_all(root.join("a/c")).unwrap();
        fs::write(root.join("a/b/target.ts"), "").unwrap();

        let from_sibling =
            resolve(&root.join("a/b"), "./target", Lang::TypeScript).expect("sibling");
        let from_cousin =
            resolve(&root.join("a/c"), "../b/target", Lang::TypeScript).expect("cousin");

        assert_eq!(
            from_sibling, from_cousin,
            "different spellings should normalize to the same PathBuf"
        );
    }

    #[test]
    fn js_specifier_falls_back_to_typescript_when_javascript_is_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        fs::write(root.join("types.ts"), "").unwrap();

        assert_eq!(resolve_js(root, "./types.js"), Some(root.join("types.ts")));

        fs::write(root.join("types.js"), "").unwrap();
        assert_eq!(resolve_js(root, "./types.js"), Some(root.join("types.js")));
    }

    #[test]
    fn js_specifier_skips_typescript_directory_and_finds_tsx_file() {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        fs::create_dir(root.join("view.ts")).unwrap();
        fs::write(root.join("view.tsx"), "").unwrap();

        assert_eq!(resolve_js(root, "./view.jsx"), Some(root.join("view.tsx")));
    }

    #[test]
    fn bash_is_import_line_tab_separated() {
        // Tab between `source` and path is valid bash and must be detected.
        assert!(
            is_import_line("source\t./lib.sh", Lang::Bash),
            "source<TAB>./lib.sh should be detected as an import line"
        );
        // False positives: `sourcefile=1` looks like it starts with `source` but
        // has no whitespace separator.
        assert!(
            !is_import_line("sourcefile=1", Lang::Bash),
            "sourcefile=1 must not be detected as an import line"
        );
        // `./script.sh` is a script execution, not a source directive.
        assert!(
            !is_import_line("./script.sh", Lang::Bash),
            "./script.sh must not be detected as an import line"
        );
        // `.bashrc` — dot followed by non-whitespace, not a source directive.
        assert!(
            !is_import_line(".bashrc", Lang::Bash),
            ".bashrc must not be detected as an import line"
        );
    }
}
