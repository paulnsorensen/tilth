use std::io::Read;
use std::num::NonZeroUsize;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};

use clru::CLruCache;

use crate::types::Lang;
use crate::util::{content_fingerprint, FileRevision};

/// Count cap for the outline-string cache. Entries are small (rendered
/// outline text), so a count cap in the low thousands bounds memory without
/// needing byte-weighting.
const MAX_OUTLINE_ENTRIES: usize = 2000;

/// Count cap for the parsed-file cache. Each entry pins up to 500 KB of
/// source plus a tree-sitter `Tree`. The cap bounds retained source to
/// roughly 250 MB in the worst case. Trees add more on top of that. The
/// snapshots ceiling in `src/edit/snapshots.rs` is 64 MiB.
const MAX_PARSED_ENTRIES: usize = 500;

/// Largest file the parsed-file cache accepts.
const MAX_PARSED_FILE_BYTES: u64 = 500_000;

/// Reservation attempts before `get_or_parse` parses once without caching.
const MAX_LOAD_ATTEMPTS: usize = 3;

/// Whether a cached outline was rendered in size-capped or full form.
#[derive(Clone, Copy, PartialEq, Eq, Hash)]
pub enum OutlineMode {
    Capped,
    Full,
}

/// Content-supplied and disk-backed outlines key the same path independently;
/// without this, one path's warm content entry and warm disk entry would
/// evict each other.
#[derive(Clone, Copy, PartialEq, Eq, Hash)]
enum RevisionKind {
    Content,
    Disk,
}

#[derive(PartialEq, Eq)]
enum OutlineRevision {
    Content(u64),
    Disk(FileRevision),
}

impl OutlineRevision {
    fn kind(&self) -> RevisionKind {
        match self {
            OutlineRevision::Content(_) => RevisionKind::Content,
            OutlineRevision::Disk(_) => RevisionKind::Disk,
        }
    }
}

/// One revision per path and rendering mode; stale values replace old entries.
struct CacheEntry {
    revision: OutlineRevision,
    outline: Arc<str>,
}

/// One ast-grep-owned source and tree snapshot shared by all AST consumers.
pub struct ParsedFile {
    document: crate::lang::treesitter::ParsedDocument,
    pub lang: Lang,
}

impl std::fmt::Debug for ParsedFile {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ParsedFile")
            .field("content_len", &self.content().len())
            .field("lang", &self.lang)
            .finish_non_exhaustive()
    }
}

impl ParsedFile {
    #[must_use]
    pub fn content(&self) -> &String {
        &self.document.root().get_doc().src
    }

    #[must_use]
    pub fn tree(&self) -> &tree_sitter::Tree {
        &self.document.root().get_doc().tree
    }

    pub(crate) fn ast(&self) -> &crate::lang::treesitter::ParsedDocument {
        &self.document
    }

    pub(crate) fn outline_entries(&self) -> Vec<crate::types::OutlineEntry> {
        let lines: Vec<_> = self.content().lines().collect();
        crate::lang::outline::walk_top_level(self.tree().root_node(), &lines, self.lang)
    }
}

/// A shared load slot. It is empty while one caller loads it.
type LoadSlot = Arc<OnceLock<Option<Arc<ParsedFile>>>>;

/// One bounded resident entry: a revision and its load slot.
struct ParsedEntry {
    revision: FileRevision,
    file: LoadSlot,
}

#[cfg(test)]
static READ_COUNTS: std::sync::LazyLock<Mutex<std::collections::HashMap<PathBuf, usize>>> =
    std::sync::LazyLock::new(|| Mutex::new(std::collections::HashMap::new()));

#[cfg(test)]
type ReadPause = (std::sync::mpsc::Sender<()>, std::sync::mpsc::Receiver<()>);

#[cfg(test)]
static READ_PAUSES: std::sync::LazyLock<Mutex<std::collections::HashMap<PathBuf, ReadPause>>> =
    std::sync::LazyLock::new(|| Mutex::new(std::collections::HashMap::new()));

#[cfg(test)]
static READ_FAILURE_PAUSES: std::sync::LazyLock<
    Mutex<std::collections::HashMap<PathBuf, ReadPause>>,
> = std::sync::LazyLock::new(|| Mutex::new(std::collections::HashMap::new()));

#[cfg(test)]
static RESERVATION_PAUSES: std::sync::LazyLock<
    Mutex<std::collections::HashMap<PathBuf, ReadPause>>,
> = std::sync::LazyLock::new(|| Mutex::new(std::collections::HashMap::new()));

#[cfg(test)]
static LOAD_DECISIONS: std::sync::LazyLock<
    Mutex<std::collections::HashMap<PathBuf, std::sync::mpsc::Sender<()>>>,
> = std::sync::LazyLock::new(|| Mutex::new(std::collections::HashMap::new()));

#[cfg(test)]
static RESERVATION_REFUSALS: std::sync::LazyLock<Mutex<std::collections::HashMap<PathBuf, usize>>> =
    std::sync::LazyLock::new(|| Mutex::new(std::collections::HashMap::new()));

fn read_source(path: &Path) -> std::io::Result<String> {
    #[cfg(test)]
    {
        if let Some(count) = READ_COUNTS.lock().unwrap().get_mut(path) {
            *count += 1;
        }
        let pause = READ_PAUSES.lock().unwrap().remove(path);
        if let Some((entered, resume)) = pause {
            entered.send(()).unwrap();
            resume
                .recv_timeout(std::time::Duration::from_secs(5))
                .unwrap();
        }
    }
    let result = std::fs::read_to_string(path);
    #[cfg(test)]
    if result.is_err() {
        let pause = READ_FAILURE_PAUSES.lock().unwrap().remove(path);
        if let Some((entered, resume)) = pause {
            entered.send(()).unwrap();
            resume
                .recv_timeout(std::time::Duration::from_secs(5))
                .unwrap();
        }
    }
    result
}

fn pause_reservation(path: &Path) {
    #[cfg(test)]
    {
        let pause = RESERVATION_PAUSES.lock().unwrap().remove(path);
        if let Some((entered, resume)) = pause {
            entered.send(()).unwrap();
            resume
                .recv_timeout(std::time::Duration::from_secs(5))
                .unwrap();
        }
    }
    #[cfg(not(test))]
    let _ = path;
}

fn record_load_decision(path: &Path) {
    #[cfg(test)]
    if let Some(notify) = LOAD_DECISIONS.lock().unwrap().get(path) {
        let _ = notify.send(());
    }
    #[cfg(not(test))]
    let _ = path;
}

/// Test hook: refuse the next reservations for `path`, as a changing file does.
fn refuse_reservation(path: &Path) -> bool {
    #[cfg(test)]
    if let Some(remaining) = RESERVATION_REFUSALS.lock().unwrap().get_mut(path) {
        if *remaining > 0 {
            *remaining -= 1;
            return true;
        }
    }
    #[cfg(not(test))]
    let _ = path;
    false
}

/// Bounded outline and parsed-file caches. Each entry retains its source revision.
/// Outline keys also distinguish rendering mode and content vs. disk revisions.
pub struct OutlineCache {
    entries: Mutex<CLruCache<(PathBuf, OutlineMode, RevisionKind), CacheEntry>>,
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
        mode: OutlineMode,
        compute: impl FnOnce() -> String,
    ) -> Arc<str> {
        self.compute_revision(
            path,
            mode,
            OutlineRevision::Content(content_fingerprint(content)),
            compute,
        )
    }

    /// Cache a disk-backed outline. On Unix a warm hit costs one `stat`, no
    /// source read; non-Unix warm hits still read the file to fingerprint it
    /// because mtime and length alone are insufficient there.
    /// The closure must read the file after this method starts.
    pub fn get_or_compute_disk(
        &self,
        path: &Path,
        mode: OutlineMode,
        compute: impl FnOnce() -> String,
    ) -> Arc<str> {
        let Some(revision) = FileRevision::of(path) else {
            return compute().into();
        };
        self.compute_revision(path, mode, OutlineRevision::Disk(revision), compute)
    }

    /// Same as `get_or_compute_disk`, but reuses metadata the caller already
    /// fetched instead of `stat`-ing the file again.
    pub fn get_or_compute_disk_with_metadata(
        &self,
        path: &Path,
        mode: OutlineMode,
        meta: &std::fs::Metadata,
        compute: impl FnOnce() -> String,
    ) -> Arc<str> {
        let Some(revision) = FileRevision::from_metadata(path, meta) else {
            return compute().into();
        };
        self.compute_revision(path, mode, OutlineRevision::Disk(revision), compute)
    }

    fn compute_revision(
        &self,
        path: &Path,
        mode: OutlineMode,
        revision: OutlineRevision,
        compute: impl FnOnce() -> String,
    ) -> Arc<str> {
        let key = (path.to_path_buf(), mode, revision.kind());
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
            if !before.is_current(path) {
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
    /// failures. Concurrent requests for one resident revision share one load.
    /// A file changed during parsing is returned but not retained. A file that
    /// keeps changing during reservation is parsed once without caching.
    #[must_use]
    pub fn get_or_parse(&self, path: &Path) -> Option<Arc<ParsedFile>> {
        let crate::types::FileType::Code(lang) = crate::lang::detect_file_type(path) else {
            return None;
        };
        let language = crate::lang::outline::outline_language(lang)?;
        let load_uncached = || {
            let content = read_source(path).ok()?;
            let document = crate::lang::treesitter::parse_document(content, &language)?;
            Some(Arc::new(ParsedFile { document, lang }))
        };
        for _ in 0..MAX_LOAD_ATTEMPTS {
            let meta = std::fs::metadata(path).ok()?;
            if meta.len() > MAX_PARSED_FILE_BYTES {
                return None;
            }
            let revision = FileRevision::from_metadata(path, &meta)?;
            let Some(load) = self.load_for_revision(path, &revision, None) else {
                continue;
            };
            record_load_decision(path);
            let mut loaded = false;
            let file = load
                .get_or_init(|| {
                    loaded = true;
                    load_uncached()
                })
                .clone();
            // Only the caller that ran the load rechecks freshness; a warm hit
            // trusts the revision it just observed, as a plain lookup does.
            if file.is_none() || (loaded && !revision.is_current(path)) {
                self.discard_if_entry(path, &revision, &load);
            }
            return file;
        }
        if std::fs::metadata(path).ok()?.len() > MAX_PARSED_FILE_BYTES {
            return None;
        }
        load_uncached()
    }

    /// Return the resident snapshot for `path` when its revision matches and,
    /// when `content` is given, its bytes equal `content`. This never reads,
    /// parses, or waits for a load.
    fn lookup_parsed(
        &self,
        path: &Path,
        revision: &FileRevision,
        content: Option<&str>,
    ) -> Option<Arc<ParsedFile>> {
        let mut parsed = self
            .parsed
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let entry = parsed.get(path)?;
        if entry.revision != *revision {
            return None;
        }
        let file = entry.file.get()?.as_ref()?;
        if content.is_some_and(|c| file.content().as_str() != c) {
            return None;
        }
        Some(Arc::clone(file))
    }

    /// Reuse a disk snapshot only when its bytes match the caller's source.
    /// Large files and retained source revisions parse without entering the cache.
    pub(crate) fn parse_source(&self, path: &Path, content: &str) -> Option<Arc<ParsedFile>> {
        if let Some(file) = self.get_or_parse(path) {
            if file.content() == content {
                return Some(file);
            }
        }
        Self::parse_supplied(path, content)
    }

    /// Parse bytes that a bulk walk already read. The caller must take
    /// `revision` BEFORE it reads the bytes. A hit needs an equal revision
    /// and equal bytes. A miss parses the supplied bytes once and never
    /// re-reads the file. The parse is published only if the file still has
    /// `revision` after the read and the resident entry did not change while
    /// that check ran. A change between the stat and the read, or after the
    /// read, fails that check, so no stale bytes enter the cache. Without a
    /// revision, or over the size limit, nothing is published.
    pub(crate) fn parse_with_revision<C: AsRef<str> + Into<String>>(
        &self,
        path: &Path,
        content: C,
        revision: Option<FileRevision>,
    ) -> Option<Arc<ParsedFile>> {
        let Some(revision) = revision else {
            return Self::parse_supplied(path, content);
        };
        if content.as_ref().len() as u64 > MAX_PARSED_FILE_BYTES {
            return Self::parse_supplied(path, content);
        }
        if let Some(file) = self.lookup_parsed(path, &revision, Some(content.as_ref())) {
            return Some(file);
        }
        let file = Self::parse_supplied(path, content)?;
        self.publish_or_reuse_if_current(path, &revision, Arc::clone(&file))
            .or(Some(file))
    }

    /// Return the cached snapshot for `path` at `revision` without reading or parsing.
    pub(crate) fn cached_parse(
        &self,
        path: &Path,
        revision: &FileRevision,
    ) -> Option<Arc<ParsedFile>> {
        self.lookup_parsed(path, revision, None)
    }

    /// Parse supplied bytes; an owned `String` moves into the snapshot without a copy.
    fn parse_supplied<C: Into<String>>(path: &Path, content: C) -> Option<Arc<ParsedFile>> {
        let crate::types::FileType::Code(lang) = crate::lang::detect_file_type(path) else {
            return None;
        };
        let language = crate::lang::outline::outline_language(lang)?;
        let document = crate::lang::treesitter::parse_document(content, &language)?;
        Some(Arc::new(ParsedFile { document, lang }))
    }

    /// Remove only this path's rendered outlines and parsed snapshot.
    /// Return the removed parsed snapshot, if any.
    pub(crate) fn invalidate(&self, path: &Path) -> Option<Arc<ParsedFile>> {
        let mut entries = self
            .entries
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        for mode in [OutlineMode::Capped, OutlineMode::Full] {
            for kind in [RevisionKind::Content, RevisionKind::Disk] {
                entries.pop(&(path.to_path_buf(), mode, kind));
            }
        }
        drop(entries);
        self.parsed
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .pop(path)
            .and_then(|entry| entry.file.get().and_then(Option::as_ref).cloned())
    }

    /// Remove `path` and its canonical spelling. `canonical` comes from the
    /// caller because it must be captured before a remove or move.
    pub(crate) fn invalidate_spellings(&self, path: &Path, canonical: &Path) {
        self.invalidate_alias(path, canonical);
        self.invalidate(path);
    }

    /// Remove only the canonical spelling when it differs from `path`.
    pub(crate) fn invalidate_alias(&self, path: &Path, canonical: &Path) {
        if canonical != path {
            self.invalidate(canonical);
        }
    }

    /// Report whether a parsed snapshot exists for this path.
    #[cfg(test)]
    pub(crate) fn has_parsed(&self, path: &Path) -> bool {
        self.parsed
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .contains(path)
    }

    /// Publish incremental bytes only after a successful write and disk verification.
    /// Cold paths stay cold. A mismatch discards this path, never unrelated entries.
    pub(crate) fn update_after_write(&self, path: &Path, before: &str, after: &str) {
        let Some(previous) = self
            .invalidate(path)
            .filter(|file| file.content() == before)
        else {
            return;
        };
        if after.len() as u64 > MAX_PARSED_FILE_BYTES {
            return;
        }
        let Ok(meta) = std::fs::metadata(path) else {
            return;
        };
        if !std::fs::File::open(path)
            .and_then(|file| written_bytes_match(file, after.as_bytes()))
            .unwrap_or(false)
        {
            return;
        }
        let Some(revision) = FileRevision::from_metadata_and_bytes(&meta, after.as_bytes()) else {
            return;
        };
        if !revision.is_current(path) {
            return;
        }
        let Some(document) =
            crate::lang::treesitter::document_after_edit(before, after, previous.ast())
        else {
            return;
        };
        let file = Arc::new(ParsedFile {
            document,
            lang: previous.lang,
        });
        let _ = self.publish_or_reuse_if_current(path, &revision, file);
    }

    /// Publish parsed bytes only while they still describe the file on disk.
    fn publish_or_reuse_if_current(
        &self,
        path: &Path,
        revision: &FileRevision,
        file: Arc<ParsedFile>,
    ) -> Option<Arc<ParsedFile>> {
        if !revision.is_current(path) {
            return None;
        }
        pause_reservation(path);
        let load = self.load_for_revision(path, revision, Some(Arc::clone(&file)))?;
        // A ready slot wins. A slot that another caller is still loading is not
        // awaited; this caller keeps its own parse.
        Some(load.get().cloned().flatten().unwrap_or(file))
    }

    /// Return the slot for `revision`, or reserve a new one. A new slot holds
    /// `ready` when given, so no other caller can claim it empty.
    fn load_for_revision(
        &self,
        path: &Path,
        revision: &FileRevision,
        ready: Option<Arc<ParsedFile>>,
    ) -> Option<LoadSlot> {
        if refuse_reservation(path) {
            return None;
        }
        let observed = {
            let mut parsed = self
                .parsed
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            match parsed.get(path) {
                Some(entry) if entry.revision == *revision => {
                    return Some(Arc::clone(&entry.file));
                }
                Some(entry) => Some(Arc::clone(&entry.file)),
                None => None,
            }
        };
        if !revision.is_current(path) {
            return None;
        }
        pause_reservation(path);
        let mut parsed = self
            .parsed
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let unchanged = match parsed.get(path) {
            Some(entry) if entry.revision == *revision => {
                return Some(Arc::clone(&entry.file));
            }
            Some(entry) => observed
                .as_ref()
                .is_some_and(|previous| Arc::ptr_eq(previous, &entry.file)),
            None => observed.is_none(),
        };
        if !unchanged {
            return None;
        }
        let load = Arc::new(ready.map_or_else(OnceLock::new, |file| OnceLock::from(Some(file))));
        parsed.put(
            path.to_path_buf(),
            ParsedEntry {
                revision: revision.clone(),
                file: Arc::clone(&load),
            },
        );
        Some(load)
    }

    fn discard_if_entry(&self, path: &Path, revision: &FileRevision, load: &LoadSlot) {
        let mut parsed = self
            .parsed
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let matches = parsed
            .get(path)
            .is_some_and(|entry| entry.revision == *revision && Arc::ptr_eq(&entry.file, load));
        if matches {
            parsed.pop(path);
        }
    }
}

fn written_bytes_match(reader: impl Read, expected: &[u8]) -> std::io::Result<bool> {
    let mut actual = Vec::with_capacity(expected.len() + 1);
    reader
        .take(expected.len() as u64 + 1)
        .read_to_end(&mut actual)?;
    Ok(actual == expected)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::SystemTime;

    #[test]
    fn incremental_verification_reads_only_expected_bytes_plus_one() {
        let expected = b"fn exact() {}";
        let mut bytes = expected.to_vec();
        bytes.extend_from_slice(b"external replacement suffix");
        let mut source = std::io::Cursor::new(bytes);
        assert!(!written_bytes_match(&mut source, expected).unwrap());
        assert_eq!(source.position(), expected.len() as u64 + 1);

        for (source, matches) in [
            (expected.as_slice(), true),
            (b"fn other() {}".as_slice(), false),
            (&expected[..expected.len() - 1], false),
        ] {
            assert_eq!(written_bytes_match(source, expected).unwrap(), matches);
        }
        let mut source = std::io::Cursor::new(b"external");
        assert!(!written_bytes_match(&mut source, b"").unwrap());
        assert_eq!(source.position(), 1);
        assert!(written_bytes_match(b"".as_slice(), b"").unwrap());
    }

    #[test]
    fn incremental_invalidation_removes_all_path_variants_only() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("changed.rs");
        let other = dir.path().join("other.rs");
        let cache = OutlineCache::new();
        for file in [&path, &other] {
            std::fs::write(file, "fn retained() {}").unwrap();
            cache.get_or_parse(file).unwrap();
            for mode in [OutlineMode::Capped, OutlineMode::Full] {
                cache.get_or_compute(file, b"fn retained() {}", mode, || "content".into());
                cache.get_or_compute_disk(file, mode, || "disk".into());
            }
        }
        assert_eq!(cache.entries.lock().unwrap().len(), 8);
        assert_eq!(cache.parsed.lock().unwrap().len(), 2);
        let retained = cache.get_or_parse(&other).unwrap();
        cache.invalidate(&path);
        let entries = cache.entries.lock().unwrap();
        assert_eq!(entries.len(), 4);
        assert!(entries.iter().all(|((key, _, _), _)| key == &other));
        drop(entries);
        assert_eq!(cache.parsed.lock().unwrap().len(), 1);
        assert!(Arc::ptr_eq(&retained, &cache.get_or_parse(&other).unwrap()));
    }

    #[test]
    fn incremental_growth_past_byte_cap_discards_snapshot_without_parsing() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("large.rs");
        let source = format!("// {}\nfn old() {{}}\n", dir.path().display());
        std::fs::write(&path, &source).unwrap();
        let cache = OutlineCache::new();
        let old = cache.get_or_parse(&path).unwrap();
        let padding = MAX_PARSED_FILE_BYTES as usize - source.len() - 2 + 1;
        let after = format!("{source}//{}", "x".repeat(padding));
        assert_eq!(after.len() as u64, MAX_PARSED_FILE_BYTES + 1);
        let witness = crate::lang::treesitter::ParseWitness::new(&after);
        crate::util::atomic_write_bytes(&path, after.as_bytes()).unwrap();
        cache.update_after_write(&path, &source, &after);
        assert_eq!(cache.parsed.lock().unwrap().len(), 0);
        assert!(cache.get_or_parse(&path).is_none());
        assert_eq!(witness.count(), 0);
        assert_eq!(witness.incremental_count(), 0);
        assert_eq!(old.content().as_str(), source);
    }

    #[test]
    fn incremental_growth_to_exact_byte_cap_reparses_incrementally() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("exact.rs");
        let source = format!("// {}\nfn old() {{}}\n", dir.path().display());
        std::fs::write(&path, &source).unwrap();
        let cache = OutlineCache::new();
        cache.get_or_parse(&path).unwrap();
        let padding = MAX_PARSED_FILE_BYTES as usize - source.len() - 2;
        let after = format!("{source}//{}", "x".repeat(padding));
        assert_eq!(after.len() as u64, MAX_PARSED_FILE_BYTES);
        let witness = crate::lang::treesitter::ParseWitness::new(&after);
        crate::util::atomic_write_bytes(&path, after.as_bytes()).unwrap();
        cache.update_after_write(&path, &source, &after);
        assert_eq!(witness.count(), 0);
        assert_eq!(witness.incremental_count(), 1);
        assert_eq!(cache.get_or_parse(&path).unwrap().content().as_str(), after);
    }

    #[test]
    fn incremental_write_replacement_before_verification_stays_cold() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("race.rs");
        let before = format!("// {}\nfn before() {{}}\n", dir.path().display());
        let after = before.replace("before", "tilth_");
        let external = before.replace("before", "other_");
        std::fs::write(&path, &before).unwrap();
        let cache = OutlineCache::new();
        let old = cache.get_or_parse(&path).unwrap();
        let witness = crate::lang::treesitter::ParseWitness::new(&after);
        let external_witness = crate::lang::treesitter::ParseWitness::new(&external);
        crate::util::atomic_write_bytes(&path, after.as_bytes()).unwrap();
        // An external writer replaces the file with equal-length bytes and
        // restores the mtime. Only the byte read-back can detect this.
        let mtime = std::fs::metadata(&path).unwrap().modified().unwrap();
        crate::util::atomic_write_bytes(&path, external.as_bytes()).unwrap();
        crate::util::set_mtime(&path, mtime);
        cache.update_after_write(&path, &before, &after);
        assert!(!cache.has_parsed(&path));
        assert_eq!(witness.count(), 0);
        assert_eq!(witness.incremental_count(), 0);
        assert_eq!(external_witness.count(), 0);
        assert_eq!(external_witness.incremental_count(), 0);
        assert_eq!(old.content().as_str(), before);
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
        crate::util::rewrite_with_restored_mtime(&path, mtime, || {
            std::fs::write(&path, "fn bravo() {}").unwrap();
        });
        let changed = cache.get_or_parse(&path).unwrap();
        assert_eq!(changed.content().as_str(), "fn bravo() {}");
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
        crate::util::set_mtime(&path, mtime);

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

        crate::util::rewrite_with_restored_mtime(&path, mtime, || {
            std::fs::write(&path, "fn bravo() {}").unwrap();
        });
        let changed = cache
            .get_or_parse(&path)
            .expect("changed pre-epoch source must parse");
        assert_eq!(changed.content().as_str(), "fn bravo() {}");
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
        cache.get_or_compute_disk(&path, OutlineMode::Full, || {
            std::fs::read_to_string(&path).unwrap()
        });
        crate::util::rewrite_with_restored_mtime(&path, mtime, || {
            std::fs::write(&path, "fn bravo() {}").unwrap();
        });
        let changed = cache.get_or_compute_disk(&path, OutlineMode::Full, || {
            std::fs::read_to_string(&path).unwrap()
        });
        assert_eq!(&*changed, "fn bravo() {}");
    }

    #[test]
    fn freshness_outline_does_not_publish_during_mutation() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("source.rs");
        std::fs::write(&path, "fn alpha() {}").unwrap();
        let mtime = std::fs::metadata(&path).unwrap().modified().unwrap();
        let before = crate::util::FileRevision::of(&path).unwrap();
        let cache = OutlineCache::new();
        cache.get_or_compute_disk(&path, OutlineMode::Full, || {
            let old = std::fs::read_to_string(&path).unwrap();
            crate::util::rewrite_with_restored_mtime(&path, mtime, || {
                std::fs::write(&path, "fn bravo() {}").unwrap();
            });
            old
        });
        assert_ne!(crate::util::FileRevision::of(&path).unwrap(), before);
        assert!(
            cache.entries.lock().unwrap().is_empty(),
            "must not publish an outline computed from bytes that no longer match disk"
        );
        let current = cache.get_or_compute_disk(&path, OutlineMode::Full, || {
            std::fs::read_to_string(&path).unwrap()
        });
        assert_eq!(&*current, "fn bravo() {}");
    }

    #[test]
    fn evicts_stale_content_on_reinsert() {
        let cache = OutlineCache::new();
        let path = Path::new("fake/path.rs");
        cache.get_or_compute(path, b"old", OutlineMode::Full, || "outline v0".to_string());
        assert_eq!(cache.entries.lock().unwrap().len(), 1);
        cache.get_or_compute(path, b"new", OutlineMode::Full, || "outline v1".to_string());
        assert_eq!(cache.entries.lock().unwrap().len(), 1);
        let hit =
            cache.get_or_compute(path, b"new", OutlineMode::Full, || panic!("must hit cache"));
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
            cache.get_or_compute_disk(&path, OutlineMode::Full, || {
                std::fs::read_to_string(&path).unwrap()
            });
            match step {
                0 => crate::util::rewrite_with_restored_mtime(&path, mtime, || {
                    crate::util::atomic_write_bytes(&path, expected.as_bytes()).unwrap();
                }),
                1 => {
                    std::fs::remove_file(&path).unwrap();
                    assert!(cache.get_or_parse(&path).is_none());
                    crate::util::rewrite_with_restored_mtime(&path, mtime, || {
                        std::fs::write(&path, expected).unwrap();
                    });
                }
                _ => {
                    std::fs::rename(&path, &moved).unwrap();
                    assert!(cache.get_or_parse(&path).is_none());
                    assert_eq!(
                        cache.get_or_parse(&moved).unwrap().content().as_str(),
                        "fn gamma() {}"
                    );
                    crate::util::rewrite_with_restored_mtime(&path, mtime, || {
                        std::fs::write(&path, expected).unwrap();
                    });
                }
            }
            assert_eq!(
                cache.get_or_parse(&path).unwrap().content().as_str(),
                expected
            );
            let outline = cache.get_or_compute_disk(&path, OutlineMode::Full, || {
                std::fs::read_to_string(&path).unwrap()
            });
            assert_eq!(&*outline, expected);
            let warm =
                cache.get_or_compute_disk(&path, OutlineMode::Full, || panic!("warm disk hit"));
            assert!(Arc::ptr_eq(&outline, &warm));
        }
    }

    #[test]
    fn freshness_preloaded_outlines_track_bytes_and_rendering_mode() {
        let cache = OutlineCache::new();
        let path = Path::new("source.rs");
        let first = cache.get_or_compute(path, b"old", OutlineMode::Full, || "old outline".into());
        let warm = cache.get_or_compute(path, b"old", OutlineMode::Full, || {
            panic!("warm content hit")
        });
        assert!(Arc::ptr_eq(&first, &warm));
        let capped = cache.get_or_compute(path, b"old", OutlineMode::Capped, || {
            "capped outline".into()
        });
        assert_eq!(&*capped, "capped outline");
        let changed =
            cache.get_or_compute(path, b"new", OutlineMode::Full, || "new outline".into());
        assert_eq!(&*changed, "new outline");
        let unchanged =
            cache.get_or_compute(path, b"old", OutlineMode::Capped, || panic!("capped hit"));
        assert!(Arc::ptr_eq(&capped, &unchanged));
    }

    #[test]
    fn freshness_parsed_cache_preserves_size_and_type_limits() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("large.rs");
        let prefix = "fn exact() {}\n";
        let exact = format!("{prefix}{}", " ".repeat(500_000 - prefix.len()));
        std::fs::write(&path, exact).unwrap();
        let cache = OutlineCache::new();
        assert!(cache.get_or_parse(&path).is_some());
        std::fs::File::create(&path)
            .unwrap()
            .set_len(500_001)
            .unwrap();
        assert!(cache.get_or_parse(&path).is_none());
        let text = dir.path().join("source.txt");
        std::fs::write(&text, "fn alpha() {}").unwrap();
        assert!(cache.get_or_parse(&text).is_none());
        assert_eq!(cache.parsed.lock().unwrap().len(), 1);
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
            cache.get_or_compute(&path, b"source", OutlineMode::Full, || {
                format!("outline {i}")
            });
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

        crate::util::atomic_write_bytes(&path, b"fn new() {}").unwrap();
        let newer_snapshot = cache.get_or_parse(&path).unwrap();
        assert!(
            cache
                .publish_or_reuse_if_current(&path, &old_revision, old_snapshot)
                .is_none(),
            "a late parse for an old disk revision must not replace a newer snapshot"
        );
        let retained = cache.get_or_parse(&path).unwrap();
        assert!(Arc::ptr_eq(&newer_snapshot, &retained));
        assert_eq!(retained.content().as_str(), "fn new() {}");
    }

    struct ReadWitness {
        path: PathBuf,
        decisions: std::sync::mpsc::Receiver<()>,
    }

    impl ReadWitness {
        fn new(path: &Path) -> Self {
            assert!(READ_COUNTS
                .lock()
                .unwrap()
                .insert(path.to_path_buf(), 0)
                .is_none());
            let (notify, decisions) = std::sync::mpsc::channel();
            assert!(LOAD_DECISIONS
                .lock()
                .unwrap()
                .insert(path.to_path_buf(), notify)
                .is_none());
            Self {
                path: path.to_path_buf(),
                decisions,
            }
        }

        fn count(&self) -> usize {
            READ_COUNTS.lock().unwrap()[&self.path]
        }

        fn observe_decision(&self) {
            self.decisions
                .recv_timeout(std::time::Duration::from_secs(5))
                .unwrap();
        }

        fn pause_next(&self) -> (std::sync::mpsc::Receiver<()>, std::sync::mpsc::Sender<()>) {
            let (entered, observed) = std::sync::mpsc::channel();
            let (resume, release) = std::sync::mpsc::channel();
            assert!(READ_PAUSES
                .lock()
                .unwrap()
                .insert(self.path.clone(), (entered, release))
                .is_none());
            (observed, resume)
        }

        fn pause_failure(&self) -> (std::sync::mpsc::Receiver<()>, std::sync::mpsc::Sender<()>) {
            let (entered, observed) = std::sync::mpsc::channel();
            let (resume, release) = std::sync::mpsc::channel();
            assert!(READ_FAILURE_PAUSES
                .lock()
                .unwrap()
                .insert(self.path.clone(), (entered, release))
                .is_none());
            (observed, resume)
        }
    }

    impl Drop for ReadWitness {
        fn drop(&mut self) {
            READ_COUNTS.lock().unwrap().remove(&self.path);
            READ_PAUSES.lock().unwrap().remove(&self.path);
            READ_FAILURE_PAUSES.lock().unwrap().remove(&self.path);
            LOAD_DECISIONS.lock().unwrap().remove(&self.path);
        }
    }

    fn pause_next_reservation(
        path: &Path,
    ) -> (std::sync::mpsc::Receiver<()>, std::sync::mpsc::Sender<()>) {
        let (entered, observed) = std::sync::mpsc::channel();
        let (resume, release) = std::sync::mpsc::channel();
        assert!(RESERVATION_PAUSES
            .lock()
            .unwrap()
            .insert(path.to_path_buf(), (entered, release))
            .is_none());
        (observed, resume)
    }

    #[test]
    fn concurrent_misses_perform_one_real_read_and_parse() {
        let dir = tempfile::tempdir().unwrap();
        let path = Arc::new(dir.path().join("source.rs"));
        let source = format!("// {}\nfn shared() {{}}\n", dir.path().display());
        std::fs::write(&*path, &source).unwrap();
        let reads = ReadWitness::new(&path);
        let parses = crate::lang::treesitter::ParseWitness::new(&source);
        let (read_entered, resume_read) = reads.pause_next();
        let cache = Arc::new(OutlineCache::new());

        let first = {
            let cache = Arc::clone(&cache);
            let path = Arc::clone(&path);
            std::thread::spawn(move || cache.get_or_parse(&path).unwrap())
        };
        reads.observe_decision();
        read_entered
            .recv_timeout(std::time::Duration::from_secs(5))
            .unwrap();
        let second = {
            let cache = Arc::clone(&cache);
            let path = Arc::clone(&path);
            std::thread::spawn(move || cache.get_or_parse(&path).unwrap())
        };
        reads.observe_decision();
        resume_read.send(()).unwrap();

        let first = first.join().unwrap();
        let second = second.join().unwrap();
        assert!(Arc::ptr_eq(&first, &second));
        assert_eq!(first.content().as_str(), source);
        assert_eq!(
            (reads.count(), parses.count()),
            (1, 1),
            "overlapping loads must perform one read and one parse"
        );
    }

    #[test]
    fn failed_initializer_cannot_remove_its_replacement_and_releases_waiters() {
        let dir = tempfile::tempdir().unwrap();
        let path = Arc::new(dir.path().join("retry.rs"));
        let source = format!("// {}\nfn retry() {{}}\n", dir.path().display());
        std::fs::write(&*path, &source).unwrap();
        let reads = ReadWitness::new(&path);
        let parses = crate::lang::treesitter::ParseWitness::new(&source);
        let (read_entered, resume_read) = reads.pause_next();
        let (failure_entered, resume_failure) = reads.pause_failure();
        let cache = Arc::new(OutlineCache::new());

        let first = {
            let cache = Arc::clone(&cache);
            let path = Arc::clone(&path);
            std::thread::spawn(move || cache.get_or_parse(&path))
        };
        reads.observe_decision();
        read_entered
            .recv_timeout(std::time::Duration::from_secs(5))
            .unwrap();
        let second = {
            let cache = Arc::clone(&cache);
            let path = Arc::clone(&path);
            std::thread::spawn(move || cache.get_or_parse(&path))
        };
        reads.observe_decision();
        cache.invalidate(&path);
        std::fs::remove_file(&*path).unwrap();
        resume_read.send(()).unwrap();
        failure_entered
            .recv_timeout(std::time::Duration::from_secs(5))
            .unwrap();

        std::fs::write(&*path, &source).unwrap();
        let replacement = cache.get_or_parse(&path).unwrap();
        resume_failure.send(()).unwrap();
        assert!(first.join().unwrap().is_none());
        assert!(second.join().unwrap().is_none());
        assert!(Arc::ptr_eq(
            &replacement,
            &cache.get_or_parse(&path).unwrap()
        ));
        assert_eq!((reads.count(), parses.count()), (2, 1));
    }

    #[test]
    fn paused_real_read_does_not_block_an_independent_key() {
        let dir = tempfile::tempdir().unwrap();
        let first_path = dir.path().join("paused.rs");
        let second_path = dir.path().join("independent.rs");
        let first_source = format!("// {}\nfn paused() {{}}\n", dir.path().display());
        let second_source = "fn independent() {}\n";
        std::fs::write(&first_path, &first_source).unwrap();
        std::fs::write(&second_path, second_source).unwrap();
        let reads = ReadWitness::new(&first_path);
        let first_parse = crate::lang::treesitter::ParseWitness::new(&first_source);
        let second_parse = crate::lang::treesitter::ParseWitness::new(second_source);
        let (entered, resume) = reads.pause_next();
        let cache = Arc::new(OutlineCache::new());
        let worker_cache = Arc::clone(&cache);
        let worker_path = first_path.clone();
        let worker = std::thread::spawn(move || worker_cache.get_or_parse(&worker_path));

        entered
            .recv_timeout(std::time::Duration::from_secs(5))
            .unwrap();
        let independent = cache.get_or_parse(&second_path).unwrap();
        assert_eq!(independent.content().as_str(), second_source);
        assert_eq!(second_parse.count(), 1);
        resume.send(()).unwrap();
        assert!(worker.join().unwrap().is_some());
        assert_eq!((reads.count(), first_parse.count()), (1, 1));
    }

    #[test]
    fn stale_reader_reservation_cannot_replace_a_newer_entry() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("reader-race.rs");
        let old = format!("// {}\nfn old() {{}}\n", dir.path().display());
        let new = old.replace("old", "new");
        std::fs::write(&path, &old).unwrap();
        let new_parses = crate::lang::treesitter::ParseWitness::new(&new);
        let (entered, resume) = pause_next_reservation(&path);
        let cache = Arc::new(OutlineCache::new());
        let worker_cache = Arc::clone(&cache);
        let worker_path = path.clone();
        let stale = std::thread::spawn(move || worker_cache.get_or_parse(&worker_path));

        entered
            .recv_timeout(std::time::Duration::from_secs(5))
            .unwrap();
        crate::util::atomic_write_bytes(&path, new.as_bytes()).unwrap();
        let newer = cache.get_or_parse(&path).unwrap();
        resume.send(()).unwrap();
        let raced = stale.join().unwrap().unwrap();

        assert!(Arc::ptr_eq(&newer, &raced));
        assert!(Arc::ptr_eq(&newer, &cache.get_or_parse(&path).unwrap()));
        assert_eq!(new_parses.count(), 1);
    }

    #[test]
    fn stale_writer_publication_cannot_replace_a_newer_entry() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("writer-race.rs");
        let old = format!("// {}\nfn old() {{}}\n", dir.path().display());
        let new = old.replace("old", "new");
        std::fs::write(&path, &old).unwrap();
        let cache = Arc::new(OutlineCache::new());
        let old_revision = FileRevision::of(&path).unwrap();
        let old_snapshot = cache.get_or_parse(&path).unwrap();
        let (entered, resume) = pause_next_reservation(&path);
        let worker_cache = Arc::clone(&cache);
        let worker_path = path.clone();
        let stale = std::thread::spawn(move || {
            worker_cache.publish_or_reuse_if_current(&worker_path, &old_revision, old_snapshot)
        });

        entered
            .recv_timeout(std::time::Duration::from_secs(5))
            .unwrap();
        crate::util::atomic_write_bytes(&path, new.as_bytes()).unwrap();
        let newer = cache.get_or_parse(&path).unwrap();
        resume.send(()).unwrap();

        assert!(stale.join().unwrap().is_none());
        assert!(Arc::ptr_eq(&newer, &cache.get_or_parse(&path).unwrap()));
    }
    #[test]
    fn invalidation_replaces_a_loading_entry_by_identity() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("identity.rs");
        let source = format!("// {}\nfn identity() {{}}\n", dir.path().display());
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
        cache.invalidate(&path);
        let replacement = cache.get_or_parse(&path).unwrap();
        resume.send(()).unwrap();
        let stale = worker.join().unwrap().unwrap();

        assert!(!Arc::ptr_eq(&stale, &replacement));
        assert!(Arc::ptr_eq(
            &replacement,
            &cache.get_or_parse(&path).unwrap()
        ));
        assert_eq!(witness.count(), 2);
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
        cache.invalidate(&path);
        crate::util::atomic_write_bytes(&path, b"fn after() {}\n").unwrap();
        let current = cache.get_or_parse(&path).unwrap();
        resume.send(()).unwrap();
        let retained = worker.join().unwrap().unwrap();
        assert!(!Arc::ptr_eq(&retained, &current));
        assert_eq!(retained.content().as_str(), source);
        assert_eq!(retained.outline_entries()[0].name, "before");
        assert_eq!(
            retained
                .tree()
                .root_node()
                .utf8_text(retained.content().as_bytes())
                .unwrap(),
            source
        );
        assert!(Arc::ptr_eq(&current, &cache.get_or_parse(&path).unwrap()));
        assert_eq!(current.outline_entries()[0].name, "after");
        assert_eq!(witness.count(), 1);
    }

    #[test]
    fn documents_revision_change_during_real_parse_replaces_loading_entry() {
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
        // No invalidation: the new revision must replace the loading entry by identity.
        crate::util::atomic_write_bytes(&path, b"fn after() {}\n").unwrap();
        let current = cache.get_or_parse(&path).unwrap();
        resume.send(()).unwrap();
        let retained = worker.join().unwrap().unwrap();
        assert!(!Arc::ptr_eq(&retained, &current));
        assert_eq!(retained.content().as_str(), source);
        assert!(Arc::ptr_eq(&current, &cache.get_or_parse(&path).unwrap()));
        assert_eq!(current.outline_entries()[0].name, "after");
        assert_eq!(witness.count(), 1);
    }

    #[test]
    fn publication_does_not_wait_for_an_in_flight_load() {
        let dir = tempfile::tempdir().unwrap();
        let path = Arc::new(dir.path().join("in-flight.rs"));
        let source = format!("// {}\nfn in_flight() {{}}\n", dir.path().display());
        std::fs::write(&*path, &source).unwrap();
        let reads = ReadWitness::new(&path);
        let (read_entered, resume_read) = reads.pause_next();
        let cache = Arc::new(OutlineCache::new());
        let loader = {
            let cache = Arc::clone(&cache);
            let path = Arc::clone(&path);
            std::thread::spawn(move || cache.get_or_parse(&path))
        };
        read_entered
            .recv_timeout(std::time::Duration::from_secs(5))
            .unwrap();

        let revision = FileRevision::of(&path).unwrap();
        let (done, published) = std::sync::mpsc::channel();
        {
            let cache = Arc::clone(&cache);
            let path = Arc::clone(&path);
            let source = source.clone();
            std::thread::spawn(move || {
                let _ = done.send(cache.parse_with_revision(&path, source, Some(revision)));
            });
        }
        let own = published
            .recv_timeout(std::time::Duration::from_secs(2))
            .expect("publication must not wait for another caller's load")
            .unwrap();
        resume_read.send(()).unwrap();
        let loaded = loader.join().unwrap().unwrap();

        assert_eq!(own.content().as_str(), source);
        assert!(Arc::ptr_eq(&loaded, &cache.get_or_parse(&path).unwrap()));
        assert_eq!(reads.count(), 1, "the publisher must not read the file");
    }

    #[test]
    fn repeated_reservation_refusals_parse_once_without_caching() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("churn.rs");
        let source = format!("// {}\nfn churn() {{}}\n", dir.path().display());
        std::fs::write(&path, &source).unwrap();
        RESERVATION_REFUSALS
            .lock()
            .unwrap()
            .insert(path.clone(), 100);
        let cache = OutlineCache::new();

        let file = cache.get_or_parse(&path).unwrap();
        let remaining = RESERVATION_REFUSALS.lock().unwrap().remove(&path).unwrap();

        assert_eq!(file.content().as_str(), source);
        assert_eq!(100 - remaining, MAX_LOAD_ATTEMPTS);
        assert!(!cache.has_parsed(&path));
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
                .tree()
                .root_node()
                .utf8_text(retained.content().as_bytes())
                .unwrap(),
            "fn before() {}"
        );
        let supplied = cache.parse_source(&path, retained.content()).unwrap();
        assert_eq!(supplied.outline_entries()[0].name, "before");
        assert!(Arc::ptr_eq(&current, &cache.get_or_parse(&path).unwrap()));
        std::fs::remove_file(&path).unwrap();
        assert!(cache.get_or_parse(&path).is_none());
        assert_eq!(retained.outline_entries()[0].name, "before");
    }

    #[test]
    fn loading_and_ready_entries_share_the_hard_capacity() {
        let dir = tempfile::tempdir().unwrap();
        let cache = Arc::new(OutlineCache::new());
        let first_path = dir.path().join("first.rs");
        let source = format!("// {}\nfn retained() {{}}\n", dir.path().display());
        std::fs::write(&first_path, &source).unwrap();
        let witness = crate::lang::treesitter::ParseWitness::new(&source);
        let (entered, resume) = witness.pause_next();
        let worker_cache = Arc::clone(&cache);
        let worker_path = first_path.clone();
        let loading = std::thread::spawn(move || worker_cache.get_or_parse(&worker_path));
        entered
            .recv_timeout(std::time::Duration::from_secs(5))
            .unwrap();

        for index in 0..MAX_PARSED_ENTRIES {
            let path = dir.path().join(format!("file_{index}.rs"));
            std::fs::write(&path, format!("fn entry_{index}() {{}}")).unwrap();
            assert!(cache.get_or_parse(&path).is_some());
        }
        assert_eq!(cache.parsed.lock().unwrap().len(), MAX_PARSED_ENTRIES);
        assert!(!cache.parsed.lock().unwrap().contains(&first_path));
        resume.send(()).unwrap();
        let retained = loading.join().unwrap().unwrap();
        assert_eq!(retained.outline_entries()[0].name, "retained");
        assert_eq!(cache.parsed.lock().unwrap().len(), MAX_PARSED_ENTRIES);
        assert!(!cache.parsed.lock().unwrap().contains(&first_path));

        let neighbor = dir
            .path()
            .join(format!("file_{}.rs", MAX_PARSED_ENTRIES - 1));
        let unchanged = cache.get_or_parse(&neighbor).unwrap();
        assert!(cache.get_or_parse(&dir.path().join("missing.rs")).is_none());
        assert!(Arc::ptr_eq(
            &unchanged,
            &cache.get_or_parse(&neighbor).unwrap()
        ));
        assert_eq!(cache.parsed.lock().unwrap().len(), MAX_PARSED_ENTRIES);
    }

    #[test]
    fn parse_with_revision_publishes_once_and_get_or_parse_reuses_it() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("walked.rs");
        let source = format!("// {}\nfn walked() {{}}\n", dir.path().display());
        std::fs::write(&path, &source).unwrap();
        let witness = crate::lang::treesitter::ParseWitness::new(&source);
        let cache = OutlineCache::new();
        let revision = FileRevision::of(&path);

        let cold = cache.parse_with_revision(&path, &source, revision).unwrap();
        assert_eq!(witness.count(), 1);
        let warm = cache.get_or_parse(&path).unwrap();
        assert!(Arc::ptr_eq(&cold, &warm));
        assert_eq!(witness.count(), 1, "get_or_parse reuses the walker parse");
    }

    #[test]
    fn parse_with_revision_moves_owned_string_into_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("owned.rs");
        std::fs::write(&path, "fn owned() {}\n").unwrap();
        let owned = std::fs::read_to_string(&path).unwrap();
        let before = owned.as_ptr();
        let cache = OutlineCache::new();
        let revision = FileRevision::of(&path);

        let parsed = cache.parse_with_revision(&path, owned, revision).unwrap();
        assert_eq!(parsed.content().as_ptr(), before);
    }

    #[test]
    fn parse_with_revision_refuses_to_publish_bytes_older_than_disk() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("walked.rs");
        std::fs::write(&path, "fn old() {}").unwrap();
        let cache = OutlineCache::new();
        let old_revision = FileRevision::of(&path);
        crate::util::atomic_write_bytes(&path, b"fn new() {}").unwrap();

        let stale = cache
            .parse_with_revision(&path, "fn old() {}", old_revision)
            .unwrap();
        assert_eq!(stale.content().as_str(), "fn old() {}");
        let current = cache.get_or_parse(&path).unwrap();
        assert_eq!(current.content().as_str(), "fn new() {}");
        assert!(!Arc::ptr_eq(&stale, &current));
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
            let mode = if capped {
                OutlineMode::Capped
            } else {
                OutlineMode::Full
            };
            cache.get_or_compute(&path, source.as_bytes(), mode, || {
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
