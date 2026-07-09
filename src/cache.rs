use std::num::NonZeroUsize;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::SystemTime;

use clru::CLruCache;

use crate::types::Lang;

/// Count cap for the outline-string cache. Entries are small (rendered
/// outline text), so a count cap in the low thousands bounds memory without
/// needing byte-weighting.
const MAX_OUTLINE_ENTRIES: usize = 2000;

/// Count cap for the parsed-file cache. Each entry pins up to 500 KB of
/// source plus a tree-sitter `Tree`; capping at 500 entries bounds retained
/// memory to roughly 250 MB in the worst case, comparable in spirit to
/// snapshots' 64 MiB ceiling (`src/edit/snapshots.rs`) scaled for the larger
/// per-entry cost here.
const MAX_PARSED_ENTRIES: usize = 500;

/// Cached outline entry keyed by path. `mtime` is stored in the value so a
/// stale entry is detected and evicted in O(1) (one map lookup, no `retain`).
struct CacheEntry {
    mtime: SystemTime,
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

/// Cached parsed entry keyed by path.
struct ParsedEntry {
    mtime: SystemTime,
    file: Arc<ParsedFile>,
}

/// Outline cache keyed by canonical path. Eviction is O(1) for staleness (the
/// stored `mtime` is compared to the caller-supplied value; a mismatch
/// replaces the entry) and bounded by an LRU count cap so a long-lived server
/// can't grow these caches without limit.
///
/// Stores two derived analyses: rendered outline strings (used by search
/// formatting) and parsed tree-sitter trees (used by AST scope queries).
/// Both share the same key + invalidation; nothing else is shared.
///
/// `clru::CLruCache` is not thread-safe (unlike the `DashMap` this replaces),
/// so each cache is wrapped in a `Mutex` while keeping the external `&self`
/// API unchanged.
pub struct OutlineCache {
    entries: Mutex<CLruCache<PathBuf, CacheEntry>>,
    parsed: Mutex<CLruCache<PathBuf, ParsedEntry>>,
}

impl Default for OutlineCache {
    fn default() -> Self {
        Self {
            entries: Mutex::new(CLruCache::new(
                NonZeroUsize::new(MAX_OUTLINE_ENTRIES).unwrap(),
            )),
            parsed: Mutex::new(CLruCache::new(
                NonZeroUsize::new(MAX_PARSED_ENTRIES).unwrap(),
            )),
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
        let key = path.to_path_buf();
        // Fast path: entry exists and mtime matches.
        {
            let mut entries = self.entries.lock().unwrap();
            if let Some(e) = entries.get(&key) {
                if e.mtime == mtime {
                    return Arc::clone(&e.outline);
                }
            }
        }
        // Stale or absent — compute and insert, replacing any stale entry.
        let outline: Arc<str> = compute().into();
        let mut entries = self.entries.lock().unwrap();
        entries.put(
            key,
            CacheEntry {
                mtime,
                outline: Arc::clone(&outline),
            },
        );
        outline
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
        let key = path.to_path_buf();
        // Fast path: entry exists and mtime matches.
        {
            let mut parsed = self.parsed.lock().unwrap();
            if let Some(e) = parsed.get(&key) {
                if e.mtime == mtime {
                    return Some(Arc::clone(&e.file));
                }
            }
        }
        // Stale or absent — parse and insert.
        let crate::types::FileType::Code(lang) = crate::lang::detect_file_type(path) else {
            return None;
        };
        let ts_lang = crate::lang::outline::outline_language(lang)?;
        let content = std::fs::read_to_string(path).ok()?;
        let mut parser = tree_sitter::Parser::new();
        parser.set_language(&ts_lang).ok()?;
        let tree = parser.parse(&content, None)?;
        let file = Arc::new(ParsedFile {
            content: Arc::new(content),
            tree,
            lang,
        });
        let mut parsed = self.parsed.lock().unwrap();
        parsed.put(
            key,
            ParsedEntry {
                mtime,
                file: Arc::clone(&file),
            },
        );
        Some(file)
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
        assert_eq!(cache.entries.lock().unwrap().len(), 1);

        // Re-insert with t1 — stale t0 entry must be evicted.
        cache.get_or_compute(path, t1, || "outline v1".to_string());
        assert_eq!(
            cache.entries.lock().unwrap().len(),
            1,
            "stale entry was not evicted"
        );

        // Confirm only the new entry survives.
        let hit = cache.get_or_compute(path, t1, || panic!("should hit cache"));
        assert_eq!(&*hit, "outline v1");
    }

    #[test]
    fn outline_cache_bounds_entry_count() {
        let cache = OutlineCache::new();
        // Insert more than the cap; the cache must never exceed it.
        for i in 0..MAX_OUTLINE_ENTRIES + 50 {
            let path = PathBuf::from(format!("fake/path{i}.rs"));
            cache.get_or_compute(&path, SystemTime::UNIX_EPOCH, || format!("outline {i}"));
        }
        let len = cache.entries.lock().unwrap().len();
        assert!(
            len <= MAX_OUTLINE_ENTRIES,
            "cache grew unbounded: {len} entries"
        );
    }
}
