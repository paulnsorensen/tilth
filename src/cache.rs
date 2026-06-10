use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::SystemTime;

use dashmap::mapref::entry::Entry;
use dashmap::DashMap;

use crate::types::Lang;

/// Cached outline entry.
struct CacheEntry {
    outline: Arc<str>,
}

/// File contents and its tree-sitter parse, cached together so AST consumers
/// don't re-parse on every call. `content` is `Arc<String>` so callers can
/// hold the bytes for `Node::utf8_text` without copying.
pub struct ParsedFile {
    pub content: Arc<String>,
    pub tree: tree_sitter::Tree,
    pub lang: Lang,
}

/// Outline cache keyed by (canonical path, mtime). If the file changes,
/// mtime changes; the old entry is evicted on the next insert so the cache
/// holds at most one entry per live path.
///
/// Stores two derived analyses: rendered outline strings (used by search
/// formatting) and parsed tree-sitter trees (used by AST scope queries).
/// Both share the same key + invalidation; nothing else is shared.
pub struct OutlineCache {
    entries: DashMap<(PathBuf, SystemTime), CacheEntry>,
    parsed: DashMap<(PathBuf, SystemTime), Arc<ParsedFile>>,
}

impl Default for OutlineCache {
    fn default() -> Self {
        Self {
            entries: DashMap::new(),
            parsed: DashMap::new(),
        }
    }
}

impl OutlineCache {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Get cached outline or compute and cache it. Accepts `&Path` (not `&PathBuf`).
    pub fn get_or_compute(
        &self,
        path: &Path,
        mtime: SystemTime,
        compute: impl FnOnce() -> String,
    ) -> Arc<str> {
        let key = (path.to_path_buf(), mtime);
        if let Some(e) = self.entries.get(&key) {
            return Arc::clone(&e.outline);
        }
        // Evict stale entries for this path (different mtime) BEFORE taking an
        // entry guard. `retain` locks every shard, so calling it while holding
        // an `entry()` guard on the same map deadlocks — it must run first.
        self.entries.retain(|(p, t), _| p != path || *t == mtime);
        match self.entries.entry(key) {
            Entry::Occupied(e) => Arc::clone(&e.get().outline),
            Entry::Vacant(e) => {
                let outline: Arc<str> = compute().into();
                e.insert(CacheEntry {
                    outline: Arc::clone(&outline),
                });
                outline
            }
        }
    }

    /// Parse a code file with tree-sitter and cache the result. Returns
    /// `None` for non-code files, files larger than the 500 KB cap, or parse
    /// failures.
    #[must_use]
    pub fn get_or_parse(&self, path: &Path) -> Option<Arc<ParsedFile>> {
        let meta = std::fs::metadata(path).ok()?;
        let mtime = meta.modified().ok()?;
        if meta.len() > 500_000 {
            return None;
        }
        let key = (path.to_path_buf(), mtime);
        if let Some(e) = self.parsed.get(&key) {
            return Some(Arc::clone(&e));
        }
        // Evict stale entries for this path BEFORE taking an entry guard
        // (see `get_or_compute`: `retain` under a held guard deadlocks).
        self.parsed.retain(|(p, t), _| p != path || *t == mtime);
        match self.parsed.entry(key) {
            Entry::Occupied(e) => Some(Arc::clone(e.get())),
            Entry::Vacant(e) => {
                let crate::types::FileType::Code(lang) = crate::lang::detect_file_type(path) else {
                    return None;
                };
                let ts_lang = crate::lang::outline::outline_language(lang)?;
                let content = std::fs::read_to_string(path).ok()?;
                let mut parser = tree_sitter::Parser::new();
                parser.set_language(&ts_lang).ok()?;
                let tree = parser.parse(&content, None)?;
                let parsed = Arc::new(ParsedFile {
                    content: Arc::new(content),
                    tree,
                    lang,
                });
                e.insert(Arc::clone(&parsed));
                Some(parsed)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, SystemTime};

    #[test]
    fn evicts_stale_mtime_on_reinsert() {
        let cache = OutlineCache::new();
        let path = std::path::Path::new("fake/path.rs");
        let t0 = SystemTime::UNIX_EPOCH;
        let t1 = t0 + Duration::from_secs(1);

        // Insert with t0.
        cache.get_or_compute(path, t0, || "outline v0".to_string());
        assert_eq!(cache.entries.len(), 1);

        // Re-insert with t1 — stale t0 entry must be evicted.
        cache.get_or_compute(path, t1, || "outline v1".to_string());
        assert_eq!(cache.entries.len(), 1, "stale entry was not evicted");

        // Confirm only the new entry survives.
        let hit = cache.get_or_compute(path, t1, || panic!("should hit cache"));
        assert_eq!(&*hit, "outline v1");
    }
}
