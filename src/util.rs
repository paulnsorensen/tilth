//! Shared utilities used by both `edit` and `install`.

use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

/// A cheap disk revision. Unix identity detects replacement and restored mtimes.
/// Coarse or unreliable filesystem change times cannot prove snapshot consistency.
/// Other platforms verify content because mtime and length alone are insufficient.
#[derive(serde::Serialize, serde::Deserialize, Clone, PartialEq, Eq, Debug)]
pub(crate) struct FileRevision {
    pub(crate) mtime_nanos: i128,
    pub(crate) len: u64,
    #[serde(default)]
    identity: Option<[i128; 4]>,
    #[serde(default)]
    content_hash: Option<u64>,
}

fn system_time_nanos(mtime: std::time::SystemTime) -> i128 {
    match mtime.duration_since(std::time::UNIX_EPOCH) {
        Ok(duration) => duration.as_nanos() as i128,
        Err(error) => -(error.duration().as_nanos() as i128),
    }
}

impl FileRevision {
    pub(crate) fn of(path: &Path) -> Option<Self> {
        let meta = std::fs::metadata(path).ok()?;
        Self::from_metadata(path, &meta)
    }

    pub(crate) fn matches_mtime(&self, mtime: std::time::SystemTime) -> bool {
        system_time_nanos(mtime) == self.mtime_nanos
    }

    pub(crate) fn from_metadata(_path: &Path, meta: &std::fs::Metadata) -> Option<Self> {
        let mtime_nanos = system_time_nanos(meta.modified().ok()?);
        #[cfg(unix)]
        let (identity, content_hash) = {
            use std::os::unix::fs::MetadataExt;
            (
                Some([
                    i128::from(meta.dev()),
                    i128::from(meta.ino()),
                    i128::from(meta.ctime()),
                    i128::from(meta.ctime_nsec()),
                ]),
                None,
            )
        };
        #[cfg(not(unix))]
        let (identity, content_hash) = (None, Some(file_fingerprint(_path).ok()?));
        Some(Self {
            mtime_nanos,
            len: meta.len(),
            identity,
            content_hash,
        })
    }
}

/// Fingerprint bytes already available to the caller, without another disk read.
pub(crate) fn content_fingerprint(content: &[u8]) -> u64 {
    use std::hash::{DefaultHasher, Hasher};
    let mut hasher = DefaultHasher::new();
    hasher.write(content);
    hasher.finish()
}

/// Stream fallback verification with bounded allocation.
/// Non-Unix warm hits still read the file because metadata alone is insufficient.
#[cfg(any(not(unix), test))]
fn file_fingerprint(path: &Path) -> std::io::Result<u64> {
    use std::hash::{DefaultHasher, Hasher};
    use std::io::Read;
    let mut file = std::fs::File::open(path)?;
    let mut hasher = DefaultHasher::new();
    let mut buffer = [0; 8192];
    loop {
        let count = match file.read(&mut buffer) {
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            result => result?,
        };
        if count == 0 {
            return Ok(hasher.finish());
        }
        hasher.write(&buffer[..count]);
    }
}

/// Write `bytes` to `path` atomically: write to a same-directory temp file,
/// preserve the original file's permissions, then persist it at `path`.
/// A crash mid-write leaves the original intact.
pub(crate) fn atomic_write_bytes(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write as _;

    let dir = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    let mut builder = tempfile::Builder::new();
    builder.prefix(".tilth-tmp.");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        builder.permissions(std::fs::Permissions::from_mode(0o666));
    }
    let mut temp = builder.tempfile_in(dir)?;
    temp.as_file_mut().write_all(bytes)?;

    // Preserve original file permissions so the rename does not widen or strip
    // the mode. Ignore errors because the write already succeeded.
    if let Ok(meta) = std::fs::metadata(path) {
        let _ = temp.as_file().set_permissions(meta.permissions());
    }

    match temp.persist(path) {
        Ok(_) => Ok(()),
        Err(error) => {
            let _ = error.file.close();
            Err(error.error)
        }
    }
}

/// Create `path` from `bytes` without replacing an existing destination, and
/// without ever exposing a partially-written file at that path.
///
/// Stage into a same-directory temp opened `O_CREAT|O_EXCL` — exclusive
/// creation is what makes the staging write safe, since a symlink planted at
/// the predictable temp name is refused rather than followed — then commit with
/// `hard_link`, which fails on an existing destination instead of replacing it.
/// Both guarantees hold: a failed or interrupted write leaves the destination
/// absent, and an existing destination is never clobbered.
pub(crate) fn atomic_create_bytes_no_replace(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write as _;
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    let dir = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    let tmp = dir.join(format!(".tilth-create-tmp.{}.{n}", std::process::id()));

    let staged = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&tmp)
        .and_then(|mut f| f.write_all(bytes));
    if let Err(e) = staged {
        let _ = std::fs::remove_file(&tmp);
        return Err(e);
    }

    let result = std::fs::hard_link(&tmp, path);
    let _ = std::fs::remove_file(&tmp);
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timestamp_nanos_preserves_epoch_and_fractional_pre_epoch_times() {
        use std::time::{Duration, SystemTime};

        assert_eq!(system_time_nanos(SystemTime::UNIX_EPOCH), 0);
        let before_epoch = SystemTime::UNIX_EPOCH
            .checked_sub(Duration::new(1, 500_000_000))
            .expect("1968 timestamp is representable");
        assert_eq!(system_time_nanos(before_epoch), -1_500_000_000);
    }

    #[test]
    fn freshness_streamed_fingerprint_matches_supplied_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("source");
        for size in [0, 8191, 8192, 8193, 500_001] {
            let bytes = vec![b'x'; size];
            std::fs::write(&path, &bytes).unwrap();
            assert_eq!(
                file_fingerprint(&path).unwrap(),
                content_fingerprint(&bytes)
            );
        }
    }

    /// The destination must never appear unless it appears complete. A create
    /// that fails has to leave nothing behind, or the agent's retry hits
    /// "target already exists" for a file tilth half-created.
    #[test]
    fn failed_create_leaves_no_destination_and_no_temp() {
        let dir = tempfile::tempdir().unwrap();
        // A directory at the target makes hard_link fail at the commit step,
        // after staging has already written the bytes.
        let target = dir.path().join("occupied");
        std::fs::create_dir(&target).unwrap();

        assert!(atomic_create_bytes_no_replace(&target, b"payload").is_err());
        assert!(target.is_dir(), "must not have replaced the destination");

        let leftovers: Vec<_> = std::fs::read_dir(dir.path())
            .unwrap()
            .filter_map(Result::ok)
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| n.starts_with(".tilth-create-tmp."))
            .collect();
        assert!(
            leftovers.is_empty(),
            "temp files left behind: {leftovers:?}"
        );
    }

    #[test]
    fn create_writes_exact_bytes_and_refuses_an_existing_file() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("new.rs");
        atomic_create_bytes_no_replace(&target, b"fn a() {}\n").expect("creates");
        assert_eq!(std::fs::read(&target).unwrap(), b"fn a() {}\n");

        let err = atomic_create_bytes_no_replace(&target, b"clobber").expect_err("no replace");
        assert_eq!(err.kind(), std::io::ErrorKind::AlreadyExists);
        assert_eq!(
            std::fs::read(&target).unwrap(),
            b"fn a() {}\n",
            "existing content must survive"
        );
    }

    #[test]
    fn atomic_write_overwrites_with_exact_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("target");
        std::fs::write(&target, b"old").unwrap();

        atomic_write_bytes(&target, b"new bytes").unwrap();

        assert_eq!(std::fs::read(&target).unwrap(), b"new bytes");
    }

    #[test]
    fn atomic_write_creates_missing_destination() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("target");

        atomic_write_bytes(&target, b"new bytes").unwrap();

        assert_eq!(std::fs::read(&target).unwrap(), b"new bytes");
    }

    #[cfg(unix)]
    #[test]
    fn atomic_write_preserves_existing_permissions() {
        use std::os::unix::fs::PermissionsExt;

        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("target");
        std::fs::write(&target, b"old").unwrap();
        std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o640)).unwrap();

        atomic_write_bytes(&target, b"new").unwrap();

        let mode = std::fs::metadata(&target).unwrap().permissions().mode() & 0o7777;
        assert_eq!(mode, 0o640);
    }

    #[cfg(unix)]
    #[test]
    fn atomic_write_uses_std_write_mode_for_missing_destination() {
        use std::os::unix::fs::PermissionsExt;

        let dir = tempfile::tempdir().unwrap();
        let expected = dir.path().join("expected");
        let actual = dir.path().join("actual");
        std::fs::write(&expected, b"bytes").unwrap();

        atomic_write_bytes(&actual, b"bytes").unwrap();

        let expected_mode = std::fs::metadata(&expected).unwrap().permissions().mode() & 0o7777;
        let actual_mode = std::fs::metadata(&actual).unwrap().permissions().mode() & 0o7777;
        assert_eq!(actual_mode, expected_mode);
    }

    #[cfg(unix)]
    #[test]
    fn atomic_write_replaces_target_symlink_without_changing_referent() {
        use std::os::unix::fs::symlink;

        let dir = tempfile::tempdir().unwrap();
        let victim = dir.path().join("victim");
        let target = dir.path().join("target");
        std::fs::write(&victim, b"victim").unwrap();
        symlink(&victim, &target).unwrap();

        atomic_write_bytes(&target, b"replacement").unwrap();

        assert_eq!(std::fs::read(&victim).unwrap(), b"victim");
        let metadata = std::fs::symlink_metadata(&target).unwrap();
        assert!(metadata.file_type().is_file());
        assert!(!metadata.file_type().is_symlink());
        assert_eq!(std::fs::read(&target).unwrap(), b"replacement");
    }

    #[test]
    fn atomic_write_keeps_existing_mmap_contents() {
        use memmap2::Mmap;

        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("target");
        std::fs::write(&target, b"old contents").unwrap();
        let file = std::fs::File::open(&target).unwrap();
        let map = unsafe { Mmap::map(&file).unwrap() };

        atomic_write_bytes(&target, b"new contents").unwrap();

        assert_eq!(&map[..], b"old contents");
        assert_eq!(std::fs::read(&target).unwrap(), b"new contents");
    }

    #[test]
    fn atomic_write_cleans_temp_after_failed_persist() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("occupied");
        std::fs::create_dir(&target).unwrap();

        assert!(atomic_write_bytes(&target, b"bytes").is_err());
        assert!(target.is_dir());

        let entries: Vec<_> = std::fs::read_dir(dir.path())
            .unwrap()
            .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        assert_eq!(entries, vec!["occupied"]);
    }

    #[cfg(unix)]
    #[test]
    fn atomic_write_rejects_predictable_temp_symlink() {
        use std::os::unix::fs::symlink;
        use std::process::Command;

        if std::env::var_os("TILTH_TEMP_SYMLINK_CHILD").is_some() {
            let dir = std::path::PathBuf::from(std::env::var_os("TILTH_TEMP_SYMLINK_DIR").unwrap());
            let target = dir.join("target");
            let victim = dir.join("victim");
            let temp = dir.join(format!(".tilth-tmp.{}.0", std::process::id()));
            symlink(&victim, &temp).unwrap();

            atomic_write_bytes(&target, b"replacement").unwrap();

            assert_eq!(std::fs::read(&victim).unwrap(), b"victim");
            let metadata = std::fs::symlink_metadata(&target).unwrap();
            assert!(metadata.file_type().is_file());
            assert!(!metadata.file_type().is_symlink());
            assert_eq!(std::fs::read(&target).unwrap(), b"replacement");
            return;
        }

        let dir = tempfile::tempdir().unwrap();
        let victim = dir.path().join("victim");
        std::fs::write(&victim, b"victim").unwrap();
        let status = Command::new(std::env::current_exe().unwrap())
            .arg("--exact")
            .arg("util::tests::atomic_write_rejects_predictable_temp_symlink")
            .arg("--nocapture")
            .env("TILTH_TEMP_SYMLINK_CHILD", "1")
            .env("TILTH_TEMP_SYMLINK_DIR", dir.path())
            .status()
            .unwrap();
        assert!(status.success(), "child process failed: {status}");
    }
}
