//! Redb table layout: per-file dependency shards + reverse (dependent) edges.
//!
//! Two tables:
//! - `files`: relative path → `FileShard` (mtime/len signature + resolved local deps).
//! - `reverse`: relative path (a dependency target) → list of relative paths that depend on it.
//!
//! Both are `&str → &[u8]` (JSON-encoded values) so the redb types stay
//! private to this module.

use std::collections::HashMap;
use std::path::Path;

use redb::{Database, ReadableDatabase, ReadableTable, TableDefinition};
use serde::{Deserialize, Serialize};

use super::DepsError;

const FILES: TableDefinition<&str, &[u8]> = TableDefinition::new("files");
const REVERSE: TableDefinition<&str, &[u8]> = TableDefinition::new("reverse");

/// Cheap change signature for a file: mtime + length. Cheaper than hashing
/// content on every reconcile scan; a mismatch triggers a real re-derive.
#[derive(Serialize, Deserialize, Clone, PartialEq, Eq, Debug)]
pub(super) struct FileSignature {
    pub(super) mtime_nanos: i128,
    pub(super) len: u64,
}

/// A file's own resolved local dependencies (its "shard" of the index).
#[derive(Serialize, Deserialize, Clone, Debug)]
pub(super) struct FileShard {
    pub(super) signature: FileSignature,
    /// Worktree-relative paths this file depends on.
    pub(super) deps: Vec<String>,
}

/// Current on-disk signature for `path`, or `None` if it cannot be read.
pub(super) fn signature_of(path: &Path) -> Option<FileSignature> {
    let meta = std::fs::metadata(path).ok()?;
    let mtime = meta.modified().ok()?;
    let nanos = mtime.duration_since(std::time::UNIX_EPOCH).ok()?.as_nanos() as i128;
    Some(FileSignature {
        mtime_nanos: nanos,
        len: meta.len(),
    })
}

fn redb_err(e: impl std::fmt::Display) -> DepsError {
    DepsError::Redb(e.to_string())
}

pub(super) fn read_shard(db: &Database, rel: &str) -> Result<Option<FileShard>, DepsError> {
    let txn = db.begin_read().map_err(redb_err)?;
    let table = match txn.open_table(FILES) {
        Ok(t) => t,
        Err(redb::TableError::TableDoesNotExist(_)) => return Ok(None),
        Err(e) => return Err(redb_err(e)),
    };
    match table.get(rel).map_err(redb_err)? {
        Some(v) => Ok(Some(serde_json::from_slice(v.value()).map_err(redb_err)?)),
        None => Ok(None),
    }
}

pub(super) fn read_reverse(db: &Database, rel: &str) -> Result<Vec<String>, DepsError> {
    let txn = db.begin_read().map_err(redb_err)?;
    let table = match txn.open_table(REVERSE) {
        Ok(t) => t,
        Err(redb::TableError::TableDoesNotExist(_)) => return Ok(Vec::new()),
        Err(e) => return Err(redb_err(e)),
    };
    match table.get(rel).map_err(redb_err)? {
        Some(v) => Ok(serde_json::from_slice(v.value()).map_err(redb_err)?),
        None => Ok(Vec::new()),
    }
}

/// All relative paths currently tracked in the `files` table.
pub(super) fn all_file_keys(db: &Database) -> Result<Vec<String>, DepsError> {
    let txn = db.begin_read().map_err(redb_err)?;
    let table = match txn.open_table(FILES) {
        Ok(t) => t,
        Err(redb::TableError::TableDoesNotExist(_)) => return Ok(Vec::new()),
        Err(e) => return Err(redb_err(e)),
    };
    let mut keys = Vec::new();
    for entry in table.iter().map_err(redb_err)? {
        let (k, _) = entry.map_err(redb_err)?;
        keys.push(k.value().to_string());
    }
    Ok(keys)
}

/// All (relative path -> signature) pairs currently tracked in the `files`
/// table, read in one transaction — avoids opening one read transaction per
/// walked file during `reconcile`'s unchanged-file check.
pub(super) fn all_signatures(db: &Database) -> Result<HashMap<String, FileSignature>, DepsError> {
    let txn = db.begin_read().map_err(redb_err)?;
    let table = match txn.open_table(FILES) {
        Ok(t) => t,
        Err(redb::TableError::TableDoesNotExist(_)) => return Ok(HashMap::new()),
        Err(e) => return Err(redb_err(e)),
    };
    let mut map = HashMap::new();
    for entry in table.iter().map_err(redb_err)? {
        let (k, v) = entry.map_err(redb_err)?;
        let shard: FileShard = serde_json::from_slice(v.value()).map_err(redb_err)?;
        map.insert(k.value().to_string(), shard.signature);
    }
    Ok(map)
}

/// One atomic reconcile write: upsert changed shards, remove deleted ones,
/// and keep `reverse` consistent with only the affected edges.
pub(super) struct ReconcileWrite {
    pub(super) upserts: Vec<(String, FileShard)>,
    pub(super) deletes: Vec<String>,
}

pub(super) fn apply_reconcile(db: &Database, write: &ReconcileWrite) -> Result<(), DepsError> {
    let txn = db.begin_write().map_err(redb_err)?;
    {
        let mut files = txn.open_table(FILES).map_err(redb_err)?;
        let mut reverse = txn.open_table(REVERSE).map_err(redb_err)?;

        for rel in &write.deletes {
            if let Some(v) = files.get(rel.as_str()).map_err(redb_err)? {
                let old: FileShard = serde_json::from_slice(v.value()).map_err(redb_err)?;
                drop(v);
                for dep in &old.deps {
                    remove_reverse_edge(&mut reverse, dep, rel)?;
                }
            }
            files.remove(rel.as_str()).map_err(redb_err)?;
        }

        for (rel, shard) in &write.upserts {
            let old_deps: Vec<String> = match files.get(rel.as_str()).map_err(redb_err)? {
                Some(v) => {
                    let old: FileShard = serde_json::from_slice(v.value()).map_err(redb_err)?;
                    old.deps
                }
                None => Vec::new(),
            };
            for dep in &old_deps {
                if !shard.deps.contains(dep) {
                    remove_reverse_edge(&mut reverse, dep, rel)?;
                }
            }
            for dep in &shard.deps {
                if !old_deps.contains(dep) {
                    add_reverse_edge(&mut reverse, dep, rel)?;
                }
            }
            let bytes = serde_json::to_vec(shard).map_err(redb_err)?;
            files
                .insert(rel.as_str(), bytes.as_slice())
                .map_err(redb_err)?;
        }
    }
    txn.commit().map_err(redb_err)?;
    Ok(())
}

fn remove_reverse_edge(
    table: &mut redb::Table<&str, &[u8]>,
    target: &str,
    dependent: &str,
) -> Result<(), DepsError> {
    let mut list: Vec<String> = match table.get(target).map_err(redb_err)? {
        Some(v) => serde_json::from_slice(v.value()).map_err(redb_err)?,
        None => return Ok(()),
    };
    list.retain(|p| p != dependent);
    if list.is_empty() {
        table.remove(target).map_err(redb_err)?;
    } else {
        let bytes = serde_json::to_vec(&list).map_err(redb_err)?;
        table.insert(target, bytes.as_slice()).map_err(redb_err)?;
    }
    Ok(())
}

fn add_reverse_edge(
    table: &mut redb::Table<&str, &[u8]>,
    target: &str,
    dependent: &str,
) -> Result<(), DepsError> {
    let mut list: Vec<String> = match table.get(target).map_err(redb_err)? {
        Some(v) => serde_json::from_slice(v.value()).map_err(redb_err)?,
        None => Vec::new(),
    };
    if !list.iter().any(|p| p == dependent) {
        list.push(dependent.to_string());
    }
    let bytes = serde_json::to_vec(&list).map_err(redb_err)?;
    table.insert(target, bytes.as_slice()).map_err(redb_err)?;
    Ok(())
}
