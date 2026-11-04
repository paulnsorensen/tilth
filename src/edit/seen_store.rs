//! Seen-line records that outlive one tilth process.
//!
//! The seen-lines gate lets `tilth_write` change only lines that a read or a
//! search displayed under the file's whole-file tag. The session snapshot store
//! keeps that record in memory, and a resumed agent session starts a new
//! `tilth --mcp` process. This store saves one entry per file version — the
//! normalized path key, the tag, a 64-bit content digest, and the displayed
//! line ranges — to `$XDG_STATE_HOME/tilth/seen/<hash>.json` (default
//! `~/.local/state`), one file per checkout. It never saves file text.
//!
//! A saved entry authorizes lines only for a live file whose tag and digest
//! both match it. Every failure is silent: a missing, unreadable, or corrupt
//! state file reads as empty, and a failed save keeps only the in-process
//! record. Two agents in one checkout share one record.

use std::collections::HashMap;
use std::fs;
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use serde::{Deserialize, Serialize};
use twox_hash::XxHash64;

const FORMAT_VERSION: u32 = 1;
/// Entries kept per checkout; the least recently saved drop first.
const MAX_ENTRIES: usize = 256;
/// Versions kept per file path; older contents drop first.
const MAX_VERSIONS_PER_PATH: usize = super::snapshots::DEFAULT_MAX_VERSIONS_PER_PATH;
/// Ranges kept per entry. A dropped range only makes fewer lines editable.
const MAX_RANGES_PER_ENTRY: usize = 256;
/// A larger state file is not read.
const MAX_STATE_BYTES: u64 = 4 * 1024 * 1024;
/// State files kept in the store directory; the least recently written drop first.
const MAX_STATE_FILES: usize = 128;
/// Checkouts and cwd resolutions cached in one process.
const MAX_CACHED: usize = 64;

/// The 64-bit digest that binds a saved entry to one exact file content.
pub(crate) fn content_digest(text: &str) -> u64 {
    XxHash64::oneshot(0, text.as_bytes())
}

/// The seen lines of one file version, ready to save.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SeenRecord {
    pub key: String,
    pub tag: u16,
    pub digest: u64,
    pub ranges: Vec<(u32, u32)>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct Entry {
    path: String,
    tag: u16,
    digest: u64,
    ranges: Vec<(u32, u32)>,
    seq: u64,
}

#[derive(Deserialize)]
struct StateFile {
    version: u32,
    root: String,
    entries: Vec<Entry>,
}

#[derive(Serialize)]
struct StateFileRef<'a> {
    version: u32,
    root: &'a str,
    entries: &'a [Entry],
}

/// (length, mtime) of a state file, to see a write from another process.
type Stamp = (u64, SystemTime);

/// One checkout's state file as this process last read or wrote it.
struct Checkout {
    file: PathBuf,
    root: String,
    stamp: Option<Stamp>,
    entries: Vec<Entry>,
}

/// Per-checkout seen-line records on disk. `dir: None` disables the store.
pub(crate) struct SeenStore {
    dir: Option<PathBuf>,
    roots: HashMap<PathBuf, PathBuf>,
    checkouts: HashMap<PathBuf, Checkout>,
}

impl SeenStore {
    /// The store under the XDG state directory. Unit tests get a disabled
    /// store, so they never touch the real home.
    pub(crate) fn from_env() -> Self {
        if cfg!(test) {
            return Self::new(None);
        }
        Self::new(state_dir().map(|d| d.join("tilth").join("seen")))
    }

    pub(crate) fn new(dir: Option<PathBuf>) -> Self {
        Self {
            dir,
            roots: HashMap::new(),
            checkouts: HashMap::new(),
        }
    }

    /// Merge `records` into the state file of the checkout that holds `cwd`.
    pub(crate) fn save(&mut self, cwd: &Path, records: Vec<SeenRecord>) {
        if records.is_empty() {
            return;
        }
        let Some(dir) = self.dir.clone() else {
            return;
        };
        let root = self.root_of(cwd);
        let checkout = self.checkout(&dir, &root);
        let mut seq = checkout.entries.iter().map(|e| e.seq).max().unwrap_or(0);
        for record in records {
            seq += 1;
            merge(&mut checkout.entries, record, seq);
        }
        enforce_caps(&mut checkout.entries);
        let existed = checkout.stamp.is_some();
        if write_state(checkout).is_ok() && !existed {
            prune(&dir, &checkout.file);
        }
    }

    /// The saved ranges for `key` under `tag`, when the saved digest equals
    /// the digest of `text`. Empty when no entry matches.
    pub(crate) fn lookup(
        &mut self,
        cwd: &Path,
        key: &str,
        tag: u16,
        text: &str,
    ) -> Vec<(u32, u32)> {
        let Some(dir) = self.dir.clone() else {
            return Vec::new();
        };
        let root = self.root_of(cwd);
        let checkout = self.checkout(&dir, &root);
        let Some(entry) = checkout
            .entries
            .iter()
            .find(|e| e.path == key && e.tag == tag)
        else {
            return Vec::new();
        };
        if entry.digest != content_digest(text) {
            return Vec::new();
        }
        entry.ranges.clone()
    }

    /// The checkout root of `cwd`: its first ancestor with a `.git` entry,
    /// else `cwd` itself. Canonical, so symlinked spellings share a root.
    fn root_of(&mut self, cwd: &Path) -> PathBuf {
        if let Some(root) = self.roots.get(cwd) {
            return root.clone();
        }
        let base = fs::canonicalize(cwd).unwrap_or_else(|_| cwd.to_path_buf());
        let root = base
            .ancestors()
            .find(|d| d.join(".git").exists())
            .unwrap_or(&base)
            .to_path_buf();
        if self.roots.len() >= MAX_CACHED {
            self.roots.clear();
        }
        self.roots.insert(cwd.to_path_buf(), root.clone());
        root
    }

    /// The cached state of `root`, re-read when the file changed on disk.
    fn checkout(&mut self, dir: &Path, root: &Path) -> &mut Checkout {
        if self.checkouts.len() >= MAX_CACHED && !self.checkouts.contains_key(root) {
            self.checkouts.clear();
        }
        let checkout = self.checkouts.entry(root.to_path_buf()).or_insert_with(|| {
            let root = root.to_string_lossy().into_owned();
            let name = format!("{:016x}.json", XxHash64::oneshot(0, root.as_bytes()));
            Checkout {
                file: dir.join(name),
                root,
                stamp: None,
                entries: Vec::new(),
            }
        });
        let stamp = file_stamp(&checkout.file);
        if stamp != checkout.stamp {
            checkout.entries = load(&checkout.file, &checkout.root);
            checkout.stamp = stamp;
        }
        checkout
    }
}

fn state_dir() -> Option<PathBuf> {
    if let Some(xdg) = std::env::var_os("XDG_STATE_HOME") {
        let xdg = PathBuf::from(xdg);
        // The XDG spec says to ignore a relative value.
        if xdg.is_absolute() {
            return Some(xdg);
        }
    }
    home::home_dir().map(|h| h.join(".local").join("state"))
}

fn file_stamp(file: &Path) -> Option<Stamp> {
    let meta = fs::metadata(file).ok()?;
    Some((meta.len(), meta.modified().ok()?))
}

/// The valid entries of a state file. Anything unreadable reads as empty.
fn load(file: &Path, root: &str) -> Vec<Entry> {
    if fs::metadata(file).map_or(true, |m| m.len() > MAX_STATE_BYTES) {
        return Vec::new();
    }
    let Ok(bytes) = fs::read(file) else {
        return Vec::new();
    };
    let Ok(state) = serde_json::from_slice::<StateFile>(&bytes) else {
        return Vec::new();
    };
    // A different root means a hash collision of two checkout paths.
    if state.version != FORMAT_VERSION || state.root != root {
        return Vec::new();
    }
    let mut entries: Vec<Entry> = state
        .entries
        .into_iter()
        .filter_map(|mut e| {
            e.ranges.retain(|&(lo, hi)| lo >= 1 && lo <= hi);
            e.ranges = coalesce(std::mem::take(&mut e.ranges));
            (!e.ranges.is_empty()).then_some(e)
        })
        .collect();
    enforce_caps(&mut entries);
    entries
}

fn merge(entries: &mut Vec<Entry>, record: SeenRecord, seq: u64) {
    let found = entries
        .iter_mut()
        .find(|e| e.path == record.key && e.tag == record.tag);
    let Some(entry) = found else {
        let mut ranges = coalesce(record.ranges);
        ranges.truncate(MAX_RANGES_PER_ENTRY);
        entries.push(Entry {
            path: record.key,
            tag: record.tag,
            digest: record.digest,
            ranges,
            seq,
        });
        return;
    };
    if entry.digest == record.digest {
        entry.ranges.extend(record.ranges);
    } else {
        // A 16-bit tag collision with other content: the newer content wins.
        entry.digest = record.digest;
        entry.ranges = record.ranges;
    }
    entry.ranges = coalesce(std::mem::take(&mut entry.ranges));
    entry.ranges.truncate(MAX_RANGES_PER_ENTRY);
    entry.seq = seq;
}

/// Keep the newest entries: at most `MAX_VERSIONS_PER_PATH` per path and
/// `MAX_ENTRIES` in total. Also truncates each entry's ranges.
fn enforce_caps(entries: &mut Vec<Entry>) {
    entries.sort_by_key(|e| std::cmp::Reverse(e.seq));
    let mut versions: HashMap<String, usize> = HashMap::new();
    entries.retain(|e| {
        let n = versions.entry(e.path.clone()).or_default();
        *n += 1;
        *n <= MAX_VERSIONS_PER_PATH
    });
    entries.truncate(MAX_ENTRIES);
    for e in entries.iter_mut() {
        e.ranges.truncate(MAX_RANGES_PER_ENTRY);
    }
}

/// Sorted, merged inclusive ranges; adjacent ranges join.
fn coalesce(mut ranges: Vec<(u32, u32)>) -> Vec<(u32, u32)> {
    ranges.sort_unstable();
    let mut out: Vec<(u32, u32)> = Vec::with_capacity(ranges.len());
    for (lo, hi) in ranges {
        match out.last_mut() {
            Some((_, last)) if lo <= last.saturating_add(1) => *last = (*last).max(hi),
            _ => out.push((lo, hi)),
        }
    }
    out
}

/// Replace the state file atomically: a 0600 temp file, then a rename.
fn write_state(checkout: &mut Checkout) -> std::io::Result<()> {
    let dir = checkout
        .file
        .parent()
        .ok_or_else(|| std::io::Error::other("state file has no parent"))?;
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;

        builder.mode(0o700);
    }
    builder.create(dir)?;
    let bytes = serde_json::to_vec(&StateFileRef {
        version: FORMAT_VERSION,
        root: &checkout.root,
        entries: &checkout.entries,
    })?;
    let mut temp = tempfile::Builder::new();
    temp.prefix(".seen-tmp.");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        temp.permissions(fs::Permissions::from_mode(0o600));
    }
    let mut temp = temp.tempfile_in(dir)?;
    temp.as_file_mut().write_all(&bytes)?;
    temp.persist(&checkout.file).map_err(|e| e.error)?;
    checkout.stamp = file_stamp(&checkout.file);
    Ok(())
}

/// True for a state file name: 16 lowercase hex digits and `.json`.
fn is_state_file(name: &str) -> bool {
    name.strip_suffix(".json")
        .is_some_and(|stem| stem.len() == 16 && stem.bytes().all(|b| b.is_ascii_hexdigit()))
}

/// Remove the least recently written state files over `MAX_STATE_FILES`.
/// Runs only when a save creates a new file.
fn prune(dir: &Path, keep: &Path) {
    let Ok(read) = fs::read_dir(dir) else {
        return;
    };
    let mut files: Vec<(SystemTime, PathBuf)> = read
        .filter_map(Result::ok)
        .filter(|e| e.file_name().to_str().is_some_and(is_state_file))
        .filter_map(|e| Some((e.metadata().ok()?.modified().ok()?, e.path())))
        .filter(|(_, path)| path != keep)
        .collect();
    // `keep` is one of the kept files.
    let Some(excess) = (files.len() + 1).checked_sub(MAX_STATE_FILES) else {
        return;
    };
    files.sort();
    for (_, path) in files.into_iter().take(excess) {
        let _ = fs::remove_file(path);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture {
        _temp: tempfile::TempDir,
        state: PathBuf,
        repo: PathBuf,
    }

    fn fixture() -> Fixture {
        let temp = tempfile::tempdir().unwrap();
        let state = temp.path().join("state");
        let repo = temp.path().join("repo");
        fs::create_dir_all(repo.join(".git")).unwrap();
        fs::create_dir_all(repo.join("sub")).unwrap();
        Fixture {
            _temp: temp,
            state,
            repo,
        }
    }

    fn record(key: &str, text: &str, ranges: &[(u32, u32)]) -> SeenRecord {
        SeenRecord {
            key: key.to_string(),
            tag: crate::edit::tag::compute_file_hash(text),
            digest: content_digest(text),
            ranges: ranges.to_vec(),
        }
    }

    fn state_files(dir: &Path) -> Vec<PathBuf> {
        let mut files: Vec<PathBuf> = fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().path())
            .filter(|p| p.extension().is_some_and(|x| x == "json"))
            .collect();
        files.sort();
        files
    }

    const TEXT: &str = "one\ntwo\nthree\nfour\n";
    const NONE: Vec<(u32, u32)> = Vec::new();

    #[test]
    fn saved_ranges_round_trip_through_a_new_store() {
        let f = fixture();
        let mut first = SeenStore::new(Some(f.state.clone()));
        first.save(&f.repo, vec![record("/r/a.rs", TEXT, &[(2, 3)])]);

        let mut second = SeenStore::new(Some(f.state.clone()));
        let tag = crate::edit::tag::compute_file_hash(TEXT);
        assert_eq!(second.lookup(&f.repo, "/r/a.rs", tag, TEXT), vec![(2, 3)]);
        // A subdirectory of the checkout resolves to the same state file.
        assert_eq!(
            second.lookup(&f.repo.join("sub"), "/r/a.rs", tag, TEXT),
            vec![(2, 3)]
        );
        assert_eq!(state_files(&f.state).len(), 1);
    }

    #[test]
    fn saves_merge_ranges_for_the_same_version() {
        let f = fixture();
        let mut store = SeenStore::new(Some(f.state.clone()));
        store.save(&f.repo, vec![record("/r/a.rs", TEXT, &[(1, 1)])]);
        store.save(&f.repo, vec![record("/r/a.rs", TEXT, &[(2, 2), (4, 4)])]);
        let tag = crate::edit::tag::compute_file_hash(TEXT);
        let mut fresh = SeenStore::new(Some(f.state.clone()));
        assert_eq!(
            fresh.lookup(&f.repo, "/r/a.rs", tag, TEXT),
            vec![(1, 2), (4, 4)]
        );
    }

    #[test]
    fn lookup_rejects_a_different_tag_or_content() {
        let f = fixture();
        let mut store = SeenStore::new(Some(f.state.clone()));
        store.save(&f.repo, vec![record("/r/a.rs", TEXT, &[(1, 4)])]);
        let tag = crate::edit::tag::compute_file_hash(TEXT);
        assert_eq!(store.lookup(&f.repo, "/r/a.rs", tag ^ 1, TEXT), NONE);
        // Same tag, other content: the digest refuses it.
        assert_eq!(
            store.lookup(&f.repo, "/r/a.rs", tag, "one\ntwo\nTHREE\nfour\n"),
            NONE
        );
        assert_eq!(store.lookup(&f.repo, "/r/b.rs", tag, TEXT), NONE);
    }

    #[test]
    fn another_checkout_has_its_own_record() {
        let f = fixture();
        let other = f.repo.parent().unwrap().join("other");
        fs::create_dir_all(other.join(".git")).unwrap();
        let mut store = SeenStore::new(Some(f.state.clone()));
        store.save(&f.repo, vec![record("/r/a.rs", TEXT, &[(1, 4)])]);
        let tag = crate::edit::tag::compute_file_hash(TEXT);
        assert_eq!(store.lookup(&other, "/r/a.rs", tag, TEXT), NONE);
    }

    #[test]
    fn entry_cap_drops_the_oldest() {
        let f = fixture();
        let mut store = SeenStore::new(Some(f.state.clone()));
        for i in 0..=MAX_ENTRIES {
            store.save(
                &f.repo,
                vec![record(&format!("/r/f{i}.rs"), TEXT, &[(1, 1)])],
            );
        }
        let tag = crate::edit::tag::compute_file_hash(TEXT);
        let mut fresh = SeenStore::new(Some(f.state.clone()));
        assert_eq!(fresh.lookup(&f.repo, "/r/f0.rs", tag, TEXT), NONE);
        assert_eq!(
            fresh.lookup(&f.repo, &format!("/r/f{MAX_ENTRIES}.rs"), tag, TEXT),
            vec![(1, 1)]
        );
        assert_eq!(fresh.checkout(&f.state, &f.repo).entries.len(), MAX_ENTRIES);
    }

    #[test]
    fn version_cap_keeps_the_newest_contents_of_a_path() {
        let f = fixture();
        let mut store = SeenStore::new(Some(f.state.clone()));
        let texts: Vec<String> = (0..=MAX_VERSIONS_PER_PATH)
            .map(|i| format!("v{i}\n"))
            .collect();
        for text in &texts {
            store.save(&f.repo, vec![record("/r/a.rs", text, &[(1, 1)])]);
        }
        let oldest = &texts[0];
        let newest = &texts[MAX_VERSIONS_PER_PATH];
        let tag_of = crate::edit::tag::compute_file_hash;
        assert_eq!(
            store.lookup(&f.repo, "/r/a.rs", tag_of(oldest), oldest),
            NONE
        );
        assert_eq!(
            store.lookup(&f.repo, "/r/a.rs", tag_of(newest), newest),
            vec![(1, 1)]
        );
    }

    #[test]
    fn corrupt_state_reads_as_empty_and_the_next_save_repairs_it() {
        let f = fixture();
        let mut store = SeenStore::new(Some(f.state.clone()));
        store.save(&f.repo, vec![record("/r/a.rs", TEXT, &[(1, 4)])]);
        let file = state_files(&f.state).pop().unwrap();
        fs::write(&file, b"{not json").unwrap();

        let tag = crate::edit::tag::compute_file_hash(TEXT);
        let mut fresh = SeenStore::new(Some(f.state.clone()));
        assert_eq!(fresh.lookup(&f.repo, "/r/a.rs", tag, TEXT), NONE);
        fresh.save(&f.repo, vec![record("/r/b.rs", TEXT, &[(2, 2)])]);
        let saved: serde_json::Value = serde_json::from_slice(&fs::read(&file).unwrap()).unwrap();
        assert_eq!(saved["entries"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn invalid_ranges_on_disk_are_dropped() {
        let f = fixture();
        let mut store = SeenStore::new(Some(f.state.clone()));
        store.save(&f.repo, vec![record("/r/a.rs", TEXT, &[(1, 1)])]);
        let file = state_files(&f.state).pop().unwrap();
        let mut saved: serde_json::Value =
            serde_json::from_slice(&fs::read(&file).unwrap()).unwrap();
        saved["entries"][0]["ranges"] = serde_json::json!([[0, 2], [3, 1], [4, 4]]);
        fs::write(&file, serde_json::to_vec(&saved).unwrap()).unwrap();

        let tag = crate::edit::tag::compute_file_hash(TEXT);
        let mut fresh = SeenStore::new(Some(f.state.clone()));
        assert_eq!(fresh.lookup(&f.repo, "/r/a.rs", tag, TEXT), vec![(4, 4)]);
    }

    #[test]
    fn a_save_keeps_entries_another_process_wrote() {
        let f = fixture();
        let mut first = SeenStore::new(Some(f.state.clone()));
        let mut second = SeenStore::new(Some(f.state.clone()));
        first.save(&f.repo, vec![record("/r/a.rs", TEXT, &[(1, 1)])]);
        second.save(&f.repo, vec![record("/r/b.rs", TEXT, &[(2, 2)])]);
        first.save(&f.repo, vec![record("/r/c.rs", TEXT, &[(3, 3)])]);
        let tag = crate::edit::tag::compute_file_hash(TEXT);
        let mut fresh = SeenStore::new(Some(f.state.clone()));
        for (key, line) in [("/r/a.rs", 1), ("/r/b.rs", 2), ("/r/c.rs", 3)] {
            assert_eq!(
                fresh.lookup(&f.repo, key, tag, TEXT),
                vec![(line, line)],
                "{key}"
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn state_file_is_private_to_the_user() {
        use std::os::unix::fs::PermissionsExt;

        let f = fixture();
        let mut store = SeenStore::new(Some(f.state.clone()));
        store.save(&f.repo, vec![record("/r/a.rs", TEXT, &[(1, 1)])]);
        let file = state_files(&f.state).pop().unwrap();
        assert_eq!(
            fs::metadata(&file).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(
            fs::metadata(&f.state).unwrap().permissions().mode() & 0o777,
            0o700
        );
    }

    #[test]
    fn state_file_holds_no_file_text() {
        let f = fixture();
        let mut store = SeenStore::new(Some(f.state.clone()));
        let text = "secret_marker_line\n";
        store.save(&f.repo, vec![record("/r/a.rs", text, &[(1, 1)])]);
        let file = state_files(&f.state).pop().unwrap();
        let raw = fs::read_to_string(file).unwrap();
        assert!(!raw.contains("secret_marker_line"), "{raw}");
    }

    #[test]
    fn creating_a_state_file_prunes_the_oldest_over_the_cap() {
        let f = fixture();
        fs::create_dir_all(&f.state).unwrap();
        for i in 0..MAX_STATE_FILES {
            fs::write(f.state.join(format!("{i:016x}.json")), b"{}").unwrap();
        }
        fs::write(f.state.join("unrelated.txt"), b"keep").unwrap();
        let mut store = SeenStore::new(Some(f.state.clone()));
        store.save(&f.repo, vec![record("/r/a.rs", TEXT, &[(1, 1)])]);
        assert_eq!(state_files(&f.state).len(), MAX_STATE_FILES);
        assert!(f.state.join("unrelated.txt").exists());
        let tag = crate::edit::tag::compute_file_hash(TEXT);
        assert_eq!(store.lookup(&f.repo, "/r/a.rs", tag, TEXT), vec![(1, 1)]);
    }

    #[test]
    fn a_disabled_store_saves_nothing() {
        let f = fixture();
        let mut store = SeenStore::new(None);
        store.save(&f.repo, vec![record("/r/a.rs", TEXT, &[(1, 1)])]);
        let tag = crate::edit::tag::compute_file_hash(TEXT);
        assert_eq!(store.lookup(&f.repo, "/r/a.rs", tag, TEXT), NONE);
        assert!(!f.state.exists());
    }

    #[test]
    fn coalesce_joins_adjacent_and_overlapping_ranges() {
        assert_eq!(
            coalesce(vec![(5, 6), (1, 2), (3, 3), (6, 9), (11, 11)]),
            vec![(1, 3), (5, 9), (11, 11)]
        );
        assert_eq!(coalesce(vec![(1, u32::MAX), (2, 3)]), vec![(1, u32::MAX)]);
    }
}
