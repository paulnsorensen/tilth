use std::num::NonZeroUsize;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use clru::CLruCache;

use crate::types::Lang;
use crate::util::{content_fingerprint, FileRevision};

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

#[derive(PartialEq, Eq)]
enum OutlineRevision {
    Content(u64),
    Disk(FileRevision),
}

/// One revision per path and rendering mode; stale values replace old entries.
struct CacheEntry {
    revision: OutlineRevision,
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

impl ParsedFile {
    pub(crate) fn outline_entries(&self) -> Vec<crate::types::OutlineEntry> {
        let lines: Vec<_> = self.content.lines().collect();
        crate::lang::outline::walk_top_level(self.tree.root_node(), &lines, self.lang)
    }
}

/// Cached parsed entry keyed by path.
struct ParsedEntry {
    revision: FileRevision,
    file: Arc<ParsedFile>,
}

/// Bounded outline and parsed-file caches. Each entry retains its source revision.
/// Outline keys also distinguish capped and uncapped rendering.
pub struct OutlineCache {
    entries: Mutex<CLruCache<(PathBuf, bool), CacheEntry>>,
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

    /// Cache an outline from the supplied bytes and rendering mode.
    /// The closure must render these bytes, not read a later disk version.
    pub fn get_or_compute(
        &self,
        path: &Path,
        content: &[u8],
        capped: bool,
        compute: impl FnOnce() -> String,
    ) -> Arc<str> {
        self.compute_revision(
            path,
            capped,
            OutlineRevision::Content(content_fingerprint(content)),
            compute,
        )
    }

    /// Cache a disk-backed outline without reading source bytes on warm hits.
    /// The closure must read the file after this method starts.
    pub fn get_or_compute_disk(
        &self,
        path: &Path,
        capped: bool,
        compute: impl FnOnce() -> String,
    ) -> Arc<str> {
        let Some(revision) = FileRevision::of(path) else {
            return compute().into();
        };
        self.compute_revision(path, capped, OutlineRevision::Disk(revision), compute)
    }

    fn compute_revision(
        &self,
        path: &Path,
        capped: bool,
        revision: OutlineRevision,
        compute: impl FnOnce() -> String,
    ) -> Arc<str> {
        let key = (path.to_path_buf(), capped);
        {
            let mut entries = self
                .entries
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            if let Some(entry) = entries.get(&key) {
                if entry.revision == revision {
                    return Arc::clone(&entry.outline);
                }
            }
        }
        let outline: Arc<str> = compute().into();
        if let OutlineRevision::Disk(before) = &revision {
            if FileRevision::of(path).as_ref() != Some(before) {
                return outline;
            }
        }
        self.entries
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .put(
                key,
                CacheEntry {
                    revision,
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
        let crate::types::FileType::Code(lang) = crate::lang::detect_file_type(path) else {
            return None;
        };
        let meta = std::fs::metadata(path).ok()?;
        if meta.len() > 500_000 {
            return None;
        }
        let revision = FileRevision::from_metadata(path, &meta)?;
        let key = path.to_path_buf();
        // Reuse the tree only while the disk revision matches.
        {
            let mut parsed = self
                .parsed
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            if let Some(e) = parsed.get(&key) {
                if e.revision == revision {
                    return Some(Arc::clone(&e.file));
                }
            }
        }
        // Stale or absent — parse and insert.
        let ts_lang = crate::lang::outline::outline_language(lang)?;
        let content = std::fs::read_to_string(path).ok()?;
        let tree = crate::lang::treesitter::parse_source(&content, &ts_lang)?;
        if FileRevision::of(path).as_ref() != Some(&revision) {
            return None;
        }
        let file = Arc::new(ParsedFile {
            content: Arc::new(content),
            tree,
            lang,
        });
        self.publish_or_reuse_if_current(path, revision, file)
    }

    /// Reuse a disk snapshot only when its bytes match the caller's source.
    /// Large files and retained source revisions parse without entering the cache.
    pub(crate) fn parse_source(&self, path: &Path, content: &str) -> Option<Arc<ParsedFile>> {
        if let Some(file) = self.get_or_parse(path) {
            if file.content.as_str() == content {
                return Some(file);
            }
        }
        let crate::types::FileType::Code(lang) = crate::lang::detect_file_type(path) else {
            return None;
        };
        let language = crate::lang::outline::outline_language(lang)?;
        let tree = crate::lang::treesitter::parse_source(content, &language)?;
        Some(Arc::new(ParsedFile {
            content: Arc::new(content.to_string()),
            tree,
            lang,
        }))
    }

    /// Publish a completed parse only while it still describes the file on disk.
    ///
    /// This final revision check happens under the cache lock. A slow parse for
    /// an old revision cannot replace a newer snapshot that another reader has
    /// already published. A concurrent parse of the same revision reuses the
    /// first published immutable snapshot.
    fn publish_or_reuse_if_current(
        &self,
        path: &Path,
        revision: FileRevision,
        file: Arc<ParsedFile>,
    ) -> Option<Arc<ParsedFile>> {
        let mut parsed = self
            .parsed
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if FileRevision::of(path).as_ref() != Some(&revision) {
            return None;
        }
        if let Some(entry) = parsed.get(path) {
            if entry.revision == revision {
                return Some(Arc::clone(&entry.file));
            }
        }
        parsed.put(
            path.to_path_buf(),
            ParsedEntry {
                revision,
                file: Arc::clone(&file),
            },
        );
        Some(file)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::SystemTime;

    fn restore_mtime(path: &Path, mtime: SystemTime) {
        std::fs::File::options()
            .write(true)
            .open(path)
            .unwrap()
            .set_times(std::fs::FileTimes::new().set_modified(mtime))
            .unwrap();
    }

    #[test]
    fn freshness_parsed_preserved_mtime_and_warm_reuse() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("source.rs");
        let other = dir.path().join("other.rs");
        std::fs::write(&path, "fn alpha() {}").unwrap();
        std::fs::write(&other, "fn other() {}").unwrap();
        let mtime = std::fs::metadata(&path).unwrap().modified().unwrap();
        let cache = OutlineCache::new();
        let first = cache.get_or_parse(&path).unwrap();
        let unchanged = cache.get_or_parse(&other).unwrap();
        assert!(Arc::ptr_eq(&first, &cache.get_or_parse(&path).unwrap()));
        std::fs::write(&path, "fn bravo() {}").unwrap();
        restore_mtime(&path, mtime);
        let changed = cache.get_or_parse(&path).unwrap();
        assert_eq!(&**changed.content, "fn bravo() {}");
        assert!(!Arc::ptr_eq(&first, &changed));
        assert!(Arc::ptr_eq(
            &unchanged,
            &cache.get_or_parse(&other).unwrap()
        ));
    }

    #[cfg(unix)]
    #[test]
    fn freshness_parsed_pre_epoch_mtime_reuses_and_revalidates() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("source.rs");
        let mtime = SystemTime::UNIX_EPOCH
            .checked_sub(std::time::Duration::new(1, 500_000_000))
            .expect("1968 timestamp is representable");
        std::fs::write(&path, "fn alpha() {}").unwrap();
        restore_mtime(&path, mtime);

        let cache = OutlineCache::new();
        let first = cache
            .get_or_parse(&path)
            .expect("pre-epoch source must parse");
        let warm = cache
            .get_or_parse(&path)
            .expect("pre-epoch source must remain cached");
        assert!(
            Arc::ptr_eq(&first, &warm),
            "unchanged pre-epoch source must reuse"
        );

        std::fs::write(&path, "fn bravo() {}").unwrap();
        restore_mtime(&path, mtime);
        let changed = cache
            .get_or_parse(&path)
            .expect("changed pre-epoch source must parse");
        assert_eq!(&**changed.content, "fn bravo() {}");
        assert!(
            !Arc::ptr_eq(&first, &changed),
            "changed pre-epoch source must not reuse stale content"
        );
    }

    #[test]
    fn freshness_outline_preserved_mtime() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("source.rs");
        std::fs::write(&path, "fn alpha() {}").unwrap();
        let mtime = std::fs::metadata(&path).unwrap().modified().unwrap();
        let cache = OutlineCache::new();
        cache.get_or_compute_disk(&path, false, || std::fs::read_to_string(&path).unwrap());
        std::fs::write(&path, "fn bravo() {}").unwrap();
        restore_mtime(&path, mtime);
        let changed =
            cache.get_or_compute_disk(&path, false, || std::fs::read_to_string(&path).unwrap());
        assert_eq!(&*changed, "fn bravo() {}");
    }

    #[test]
    fn freshness_outline_does_not_publish_during_mutation() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("source.rs");
        std::fs::write(&path, "fn alpha() {}").unwrap();
        let mtime = std::fs::metadata(&path).unwrap().modified().unwrap();
        let cache = OutlineCache::new();
        cache.get_or_compute_disk(&path, false, || {
            let old = std::fs::read_to_string(&path).unwrap();
            std::fs::write(&path, "fn bravo() {}").unwrap();
            restore_mtime(&path, mtime);
            old
        });
        let current =
            cache.get_or_compute_disk(&path, false, || std::fs::read_to_string(&path).unwrap());
        assert_eq!(&*current, "fn bravo() {}");
    }

    #[test]
    fn evicts_stale_content_on_reinsert() {
        let cache = OutlineCache::new();
        let path = Path::new("fake/path.rs");
        cache.get_or_compute(path, b"old", false, || "outline v0".to_string());
        assert_eq!(cache.entries.lock().unwrap().len(), 1);
        cache.get_or_compute(path, b"new", false, || "outline v1".to_string());
        assert_eq!(cache.entries.lock().unwrap().len(), 1);
        let hit = cache.get_or_compute(path, b"new", false, || panic!("must hit cache"));
        assert_eq!(&*hit, "outline v1");
    }

    #[test]
    fn freshness_replacement_recreation_and_rename() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("source.rs");
        let moved = dir.path().join("moved.rs");
        std::fs::write(&path, "fn alpha() {}").unwrap();
        let mtime = std::fs::metadata(&path).unwrap().modified().unwrap();
        let cache = OutlineCache::new();
        for (step, expected) in ["fn bravo() {}", "fn gamma() {}", "fn delta() {}"]
            .into_iter()
            .enumerate()
        {
            cache.get_or_parse(&path).unwrap();
            cache.get_or_compute_disk(&path, false, || std::fs::read_to_string(&path).unwrap());
            match step {
                0 => crate::util::atomic_write_bytes(&path, expected.as_bytes()).unwrap(),
                1 => {
                    std::fs::remove_file(&path).unwrap();
                    assert!(cache.get_or_parse(&path).is_none());
                    std::fs::write(&path, expected).unwrap();
                }
                _ => {
                    std::fs::rename(&path, &moved).unwrap();
                    assert!(cache.get_or_parse(&path).is_none());
                    assert_eq!(
                        &**cache.get_or_parse(&moved).unwrap().content,
                        "fn gamma() {}"
                    );
                    std::fs::write(&path, expected).unwrap();
                }
            }
            restore_mtime(&path, mtime);
            assert_eq!(&**cache.get_or_parse(&path).unwrap().content, expected);
            let outline =
                cache.get_or_compute_disk(&path, false, || std::fs::read_to_string(&path).unwrap());
            assert_eq!(&*outline, expected);
            let warm = cache.get_or_compute_disk(&path, false, || panic!("warm disk hit"));
            assert!(Arc::ptr_eq(&outline, &warm));
        }
    }

    #[test]
    fn freshness_preloaded_outlines_track_bytes_and_rendering_mode() {
        let cache = OutlineCache::new();
        let path = Path::new("source.rs");
        let first = cache.get_or_compute(path, b"old", false, || "old outline".into());
        let warm = cache.get_or_compute(path, b"old", false, || panic!("warm content hit"));
        assert!(Arc::ptr_eq(&first, &warm));
        let capped = cache.get_or_compute(path, b"old", true, || "capped outline".into());
        assert_eq!(&*capped, "capped outline");
        let changed = cache.get_or_compute(path, b"new", false, || "new outline".into());
        assert_eq!(&*changed, "new outline");
        let unchanged = cache.get_or_compute(path, b"old", true, || panic!("capped hit"));
        assert!(Arc::ptr_eq(&capped, &unchanged));
    }

    #[test]
    fn freshness_parsed_cache_preserves_size_and_type_limits() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("large.rs");
        std::fs::File::create(&path)
            .unwrap()
            .set_len(500_001)
            .unwrap();
        let cache = OutlineCache::new();
        assert!(cache.get_or_parse(&path).is_none());
        let text = dir.path().join("source.txt");
        std::fs::write(&text, "fn alpha() {}").unwrap();
        assert!(cache.get_or_parse(&text).is_none());
        assert!(cache.parsed.lock().unwrap().is_empty());
    }

    #[test]
    fn documents_growth_beyond_limit_then_shrink_refreshes_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("resized.rs");
        std::fs::write(&path, "fn before() {}").unwrap();
        let cache = OutlineCache::new();
        let retained = cache.get_or_parse(&path).unwrap();

        std::fs::File::create(&path)
            .unwrap()
            .set_len(500_001)
            .unwrap();
        assert!(cache.get_or_parse(&path).is_none());

        std::fs::write(&path, "fn after() {}").unwrap();
        let current = cache.get_or_parse(&path).unwrap();
        assert!(!Arc::ptr_eq(&retained, &current));
        assert_eq!(retained.outline_entries()[0].name, "before");
        assert_eq!(current.outline_entries()[0].name, "after");
        assert!(Arc::ptr_eq(&current, &cache.get_or_parse(&path).unwrap()));
    }

    #[test]
    fn outline_cache_bounds_entry_count() {
        let cache = OutlineCache::new();
        // Insert more than the cap; the cache must never exceed it.
        for i in 0..MAX_OUTLINE_ENTRIES + 50 {
            let path = PathBuf::from(format!("fake/path{i}.rs"));
            cache.get_or_compute(&path, b"source", false, || format!("outline {i}"));
        }
        let len = cache.entries.lock().unwrap().len();
        assert!(
            len <= MAX_OUTLINE_ENTRIES,
            "cache grew unbounded: {len} entries"
        );
    }

    #[test]
    fn late_old_revision_cannot_replace_newer_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("source.rs");
        std::fs::write(&path, "fn old() {}").unwrap();
        let cache = OutlineCache::new();
        let old_revision = FileRevision::of(&path).unwrap();
        let old_snapshot = cache.get_or_parse(&path).unwrap();

        std::fs::write(&path, "fn new() {}").unwrap();
        let newer_snapshot = cache.get_or_parse(&path).unwrap();
        assert!(
            cache
                .publish_or_reuse_if_current(&path, old_revision, old_snapshot)
                .is_none(),
            "a late parse for an old disk revision must not replace a newer snapshot"
        );
        let retained = cache.get_or_parse(&path).unwrap();
        assert!(Arc::ptr_eq(&newer_snapshot, &retained));
        assert_eq!(&**retained.content, "fn new() {}");
    }

    #[test]
    fn concurrent_misses_reuse_one_published_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let path = Arc::new(dir.path().join("source.rs"));
        std::fs::write(&*path, "fn shared() {}").unwrap();
        let cache = Arc::new(OutlineCache::new());
        let start = Arc::new(std::sync::Barrier::new(3));
        let mut workers = Vec::new();
        for _ in 0..2 {
            let cache = Arc::clone(&cache);
            let path = Arc::clone(&path);
            let start = Arc::clone(&start);
            workers.push(std::thread::spawn(move || {
                start.wait();
                cache.get_or_parse(&path).unwrap()
            }));
        }
        start.wait();
        let first = workers.pop().unwrap().join().unwrap();
        let second = workers.pop().unwrap().join().unwrap();
        assert!(Arc::ptr_eq(&first, &second));
        assert_eq!(&**first.content, "fn shared() {}");
    }

    #[test]
    fn documents_replacement_during_real_parse_does_not_publish_stale_work() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("race.rs");
        let source = format!("// {}\nfn before() {{}}\n", dir.path().display());
        std::fs::write(&path, &source).unwrap();
        let witness = crate::lang::treesitter::ParseWitness::new(&source);
        let (entered, resume) = witness.pause_next();
        let cache = Arc::new(OutlineCache::new());
        let worker_cache = Arc::clone(&cache);
        let worker_path = path.clone();
        let worker = std::thread::spawn(move || worker_cache.get_or_parse(&worker_path));
        entered
            .recv_timeout(std::time::Duration::from_secs(5))
            .unwrap();
        crate::util::atomic_write_bytes(&path, b"fn after() {}\n").unwrap();
        let current = cache.get_or_parse(&path).unwrap();
        resume.send(()).unwrap();
        assert!(worker.join().unwrap().is_none());
        assert!(Arc::ptr_eq(&current, &cache.get_or_parse(&path).unwrap()));
        assert_eq!(current.outline_entries()[0].name, "after");
        assert_eq!(witness.count(), 1);
    }

    #[test]
    fn documents_retained_source_and_tree_stay_coherent_after_refresh() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("retained.rs");
        std::fs::write(&path, "fn before() {}").unwrap();
        let cache = OutlineCache::new();
        let retained = cache.get_or_parse(&path).unwrap();
        std::fs::write(&path, "fn after() {}").unwrap();
        let current = cache.get_or_parse(&path).unwrap();
        assert!(!Arc::ptr_eq(&retained, &current));
        assert_eq!(retained.outline_entries()[0].name, "before");
        assert_eq!(current.outline_entries()[0].name, "after");
        assert_eq!(
            retained
                .tree
                .root_node()
                .utf8_text(retained.content.as_bytes())
                .unwrap(),
            "fn before() {}"
        );
        let supplied = cache.parse_source(&path, &retained.content).unwrap();
        assert_eq!(supplied.outline_entries()[0].name, "before");
        assert!(Arc::ptr_eq(&current, &cache.get_or_parse(&path).unwrap()));
        std::fs::remove_file(&path).unwrap();
        assert!(cache.get_or_parse(&path).is_none());
        assert_eq!(retained.outline_entries()[0].name, "before");
    }

    #[test]
    fn documents_parsed_capacity_is_bounded_and_missing_paths_do_not_evict() {
        let dir = tempfile::tempdir().unwrap();
        let cache = OutlineCache::new();
        let first_path = dir.path().join("first.rs");
        std::fs::write(&first_path, "fn retained() {}").unwrap();
        let retained = cache.get_or_parse(&first_path).unwrap();
        for index in 0..MAX_PARSED_ENTRIES {
            let path = dir.path().join(format!("file_{index}.rs"));
            std::fs::write(&path, format!("fn entry_{index}() {{}}")).unwrap();
            assert!(cache.get_or_parse(&path).is_some());
        }
        assert_eq!(cache.parsed.lock().unwrap().len(), MAX_PARSED_ENTRIES);
        assert!(!cache.parsed.lock().unwrap().contains(&first_path));
        let neighbor = dir.path().join("file_499.rs");
        let unchanged = cache.get_or_parse(&neighbor).unwrap();
        assert!(cache.get_or_parse(&dir.path().join("missing.rs")).is_none());
        assert!(Arc::ptr_eq(
            &unchanged,
            &cache.get_or_parse(&neighbor).unwrap()
        ));
        assert_eq!(cache.parsed.lock().unwrap().len(), MAX_PARSED_ENTRIES);
        assert_eq!(retained.outline_entries()[0].name, "retained");
    }

    #[test]
    fn documents_capped_and_uncapped_renderings_share_one_parse() {
        use std::fmt::Write as _;
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("outline_test.rs");
        let mut source = format!("// {}\n", dir.path().display());
        for index in 0..150 {
            writeln!(source, "pub fn entry_{index}() {{}}").unwrap();
        }
        std::fs::write(&path, &source).unwrap();
        let witness = crate::lang::treesitter::ParseWitness::new(&source);
        let cache = OutlineCache::new();
        let file_type = crate::types::FileType::Code(Lang::Rust);
        let render = |capped| {
            cache.get_or_compute(&path, source.as_bytes(), capped, || {
                crate::read::outline::generate_cached(
                    &path,
                    file_type,
                    &source,
                    source.as_bytes(),
                    capped,
                    &cache,
                )
            })
        };
        let capped = render(true);
        let uncapped = render(false);
        assert!(capped.contains("outline truncated"));
        assert!(!capped.contains("entry_149"));
        assert!(uncapped.contains("entry_149"));
        assert!(!uncapped.contains("outline truncated"));
        assert!(Arc::ptr_eq(&capped, &render(true)));
        assert!(Arc::ptr_eq(&uncapped, &render(false)));
        assert_eq!(witness.count(), 1);
        for (capped_mode, actual) in [(true, capped), (false, uncapped)] {
            let expected = crate::read::outline::generate(
                &path,
                file_type,
                &source,
                source.as_bytes(),
                capped_mode,
            );
            assert_eq!(&*actual, expected);
        }
    }
}
