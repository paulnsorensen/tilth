use redb::{
    Database, DatabaseError, ReadableDatabase, ReadableTable, ReadableTableMetadata,
    TableDefinition,
};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

const TABLE: TableDefinition<&str, u64> = TableDefinition::new("spike");

fn spike_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn scratch_dir() -> PathBuf {
    let dir = std::env::temp_dir().join(format!("redb-deps-spike-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() >= 3 && args[1] == "child-writer" {
        std::process::exit(child_writer(&args[2]));
    }
    if args.len() >= 3 && args[1] == "child-crash" {
        child_crash(&args[2]);
        return;
    }

    let dir = scratch_dir();
    let mut gates = serde_json::Map::new();

    gates.insert("atomicity".into(), gate_atomicity(&dir));
    gates.insert("concurrency".into(), gate_concurrency(&dir));
    gates.insert("lock_fallback".into(), gate_lock_fallback(&dir));
    gates.insert("restart".into(), gate_restart(&dir));
    gates.insert("worktree".into(), gate_worktree(&dir));
    gates.insert("recovery".into(), gate_recovery(&dir));
    let (churn_gate, churn_dir) = gate_churn(&dir);
    gates.insert("churn".into(), churn_gate);
    gates.insert("size".into(), gate_size(&churn_dir));

    let latency = gate_latency(&dir);

    let overall = gates.values().all(|g| g["pass"].as_bool().unwrap_or(false));

    let verdict = json!({
        "gate": if overall { "pass" } else { "fail" },
        "overall": if overall { "pass" } else { "fail" },
        "gates": Value::Object(gates),
        "latency": latency,
    });

    let out_path = spike_dir().join("verdict.json");
    std::fs::write(&out_path, serde_json::to_string_pretty(&verdict).unwrap()).unwrap();
    println!("wrote {}", out_path.display());
    println!("{}", serde_json::to_string_pretty(&verdict).unwrap());

    let _ = std::fs::remove_dir_all(&dir);
}

// --- atomicity ---------------------------------------------------------

fn gate_atomicity(dir: &Path) -> Value {
    let path = dir.join("atomicity.redb");
    let db = Database::create(&path).unwrap();

    let write_txn = db.begin_write().unwrap();
    {
        let mut table = write_txn.open_table(TABLE).unwrap();
        table.insert("a", 1u64).unwrap();
    }
    write_txn.commit().unwrap();

    // Partial write: open a second write txn, mutate, then abort instead of committing.
    let write_txn = db.begin_write().unwrap();
    {
        let mut table = write_txn.open_table(TABLE).unwrap();
        table.insert("a", 999u64).unwrap();
        table.insert("b", 999u64).unwrap();
    }
    write_txn.abort().unwrap();

    let read_txn = db.begin_read().unwrap();
    let table = read_txn.open_table(TABLE).unwrap();
    let a = table.get("a").unwrap().unwrap().value();
    let b_absent = table.get("b").unwrap().is_none();

    let pass = a == 1 && b_absent;
    json!({"pass": pass, "measurement": {"value_after_abort": a, "aborted_key_absent": b_absent}})
}

// --- concurrency ---------------------------------------------------------

fn gate_concurrency(dir: &Path) -> Value {
    let path = dir.join("concurrency.redb");
    let db = Arc::new(Database::create(&path).unwrap());
    {
        let write_txn = db.begin_write().unwrap();
        {
            let mut table = write_txn.open_table(TABLE).unwrap();
            table.insert("counter", 0u64).unwrap();
        }
        write_txn.commit().unwrap();
    }

    const WRITERS: usize = 8;
    const INCREMENTS: usize = 200;
    const READERS: usize = 4;

    let mut handles = Vec::new();
    for _ in 0..WRITERS {
        let db = Arc::clone(&db);
        handles.push(std::thread::spawn(move || {
            for _ in 0..INCREMENTS {
                let write_txn = db.begin_write().unwrap();
                {
                    let mut table = write_txn.open_table(TABLE).unwrap();
                    let current = table.get("counter").unwrap().unwrap().value();
                    table.insert("counter", current + 1).unwrap();
                }
                write_txn.commit().unwrap();
            }
        }));
    }

    let mut reader_handles = Vec::new();
    for _ in 0..READERS {
        let db = Arc::clone(&db);
        reader_handles.push(std::thread::spawn(move || {
            let mut reads = 0u64;
            for _ in 0..(INCREMENTS * WRITERS / READERS) {
                let read_txn = db.begin_read().unwrap();
                let table = read_txn.open_table(TABLE).unwrap();
                let _ = table.get("counter").unwrap().unwrap().value();
                reads += 1;
            }
            reads
        }));
    }

    for h in handles {
        h.join().unwrap();
    }
    let total_reads: u64 = reader_handles.into_iter().map(|h| h.join().unwrap()).sum();

    let read_txn = db.begin_read().unwrap();
    let table = read_txn.open_table(TABLE).unwrap();
    let final_value = table.get("counter").unwrap().unwrap().value();
    let expected = (WRITERS * INCREMENTS) as u64;

    let pass = final_value == expected;
    json!({"pass": pass, "measurement": {"final_value": final_value, "expected": expected, "total_reads": total_reads}})
}

// --- lock-fallback ---------------------------------------------------------

fn child_writer(path: &str) -> i32 {
    match Database::open(path) {
        Ok(db) => match db.begin_write() {
            Ok(txn) => {
                {
                    let mut table = txn.open_table(TABLE).unwrap();
                    table.insert("child", 1u64).unwrap();
                }
                txn.commit().unwrap();
                0
            }
            Err(_) => 1,
        },
        Err(DatabaseError::DatabaseAlreadyOpen) => 2,
        Err(_) => 3,
    }
}

fn gate_lock_fallback(dir: &Path) -> Value {
    let path = dir.join("lock_fallback.redb");
    let exe = std::env::current_exe().unwrap();

    let db = Database::create(&path).unwrap();

    let blocked_status = std::process::Command::new(&exe)
        .args(["child-writer", path.to_str().unwrap()])
        .status()
        .unwrap();
    let blocked_code = blocked_status.code().unwrap_or(-1);
    let blocked_gracefully = blocked_code == 2;

    drop(db);

    let after_release_status = std::process::Command::new(&exe)
        .args(["child-writer", path.to_str().unwrap()])
        .status()
        .unwrap();
    let after_release_ok = after_release_status.code().unwrap_or(-1) == 0;

    let db = Database::open(&path).unwrap();
    let read_txn = db.begin_read().unwrap();
    let table = read_txn.open_table(TABLE).unwrap();
    let child_wrote = table.get("child").unwrap().map(|v| v.value()).unwrap_or(0);

    let pass = blocked_gracefully && after_release_ok && child_wrote == 1;
    json!({"pass": pass, "measurement": {
        "blocked_exit_code": blocked_code,
        "blocked_gracefully": blocked_gracefully,
        "after_release_ok": after_release_ok,
        "child_wrote": child_wrote,
    }})
}

// --- restart ---------------------------------------------------------

fn gate_restart(dir: &Path) -> Value {
    let path = dir.join("restart.redb");
    {
        let db = Database::create(&path).unwrap();
        let write_txn = db.begin_write().unwrap();
        {
            let mut table = write_txn.open_table(TABLE).unwrap();
            table.insert("persisted", 42u64).unwrap();
        }
        write_txn.commit().unwrap();
    } // db dropped/closed here

    let db = Database::open(&path).unwrap();
    let read_txn = db.begin_read().unwrap();
    let table = read_txn.open_table(TABLE).unwrap();
    let value = table.get("persisted").unwrap().map(|v| v.value()).unwrap_or(0);

    let pass = value == 42;
    json!({"pass": pass, "measurement": {"value_after_reopen": value}})
}

// --- worktree ---------------------------------------------------------

fn gate_worktree(dir: &Path) -> Value {
    let path_a = dir.join("worktree_a.redb");
    let path_b = dir.join("worktree_b.redb");

    let db_a = Database::create(&path_a).unwrap();
    let write_txn = db_a.begin_write().unwrap();
    {
        let mut table = write_txn.open_table(TABLE).unwrap();
        table.insert("x", 1u64).unwrap();
    }
    write_txn.commit().unwrap();

    let db_b = Database::create(&path_b).unwrap();
    let write_txn = db_b.begin_write().unwrap();
    {
        let mut table = write_txn.open_table(TABLE).unwrap();
        table.insert("y", 2u64).unwrap();
    }
    write_txn.commit().unwrap();

    let read_a = db_a.begin_read().unwrap();
    let table_a = read_a.open_table(TABLE).unwrap();
    let a_has_x = table_a.get("x").unwrap().is_some();
    let a_has_y = table_a.get("y").unwrap().is_some();

    let read_b = db_b.begin_read().unwrap();
    let table_b = read_b.open_table(TABLE).unwrap();
    let b_has_y = table_b.get("y").unwrap().is_some();
    let b_has_x = table_b.get("x").unwrap().is_some();

    let pass = a_has_x && !a_has_y && b_has_y && !b_has_x;
    json!({"pass": pass, "measurement": {
        "a_has_x": a_has_x, "a_has_y": a_has_y, "b_has_y": b_has_y, "b_has_x": b_has_x,
    }})
}

// --- recovery (simulated unclean process crash) ---------------------------

fn child_crash(path: &str) {
    let db = Database::create(path).unwrap();
    let write_txn = db.begin_write().unwrap();
    {
        let mut table = write_txn.open_table(TABLE).unwrap();
        table.insert("baseline", 7u64).unwrap();
    }
    write_txn.commit().unwrap();

    // Start a second write transaction and never commit it: hold the file
    // open and sleep so the parent can SIGKILL this process mid-transaction.
    let write_txn = db.begin_write().unwrap();
    {
        let mut table = write_txn.open_table(TABLE).unwrap();
        table.insert("uncommitted", 999u64).unwrap();
    }
    std::thread::sleep(Duration::from_secs(10));
    write_txn.commit().unwrap();
}

fn gate_recovery(dir: &Path) -> Value {
    let path = dir.join("recovery.redb");
    let exe = std::env::current_exe().unwrap();

    let mut child = std::process::Command::new(&exe)
        .args(["child-crash", path.to_str().unwrap()])
        .spawn()
        .unwrap();

    // Give the child time to commit the baseline and start the in-progress txn.
    std::thread::sleep(Duration::from_millis(500));

    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        let _ = std::process::Command::new("kill")
            .args(["-9", &child.id().to_string()])
            .status();
        let status = child.wait().unwrap();
        let killed = status.signal() == Some(9);
        let _ = killed;
    }
    #[cfg(not(unix))]
    {
        let _ = child.kill();
        let _ = child.wait();
    }

    let db = Database::open(&path).unwrap();
    let read_txn = db.begin_read().unwrap();
    let table = read_txn.open_table(TABLE).unwrap();
    let baseline = table.get("baseline").unwrap().map(|v| v.value()).unwrap_or(0);
    let uncommitted_absent = table.get("uncommitted").unwrap().is_none();

    let pass = baseline == 7 && uncommitted_absent;
    json!({"pass": pass, "measurement": {
        "baseline_survived": baseline,
        "uncommitted_absent": uncommitted_absent,
    }})
}

// --- churn ---------------------------------------------------------

fn gate_churn(dir: &Path) -> (Value, PathBuf) {
    let path = dir.join("churn.redb");
    let db = Database::create(&path).unwrap();

    const CYCLES: usize = 2000;
    for i in 0..CYCLES {
        let key = format!("churn-{}", i % 50);
        let write_txn = db.begin_write().unwrap();
        {
            let mut table = write_txn.open_table(TABLE).unwrap();
            table.insert(key.as_str(), i as u64).unwrap();
        }
        write_txn.commit().unwrap();

        if i % 3 == 0 {
            let write_txn = db.begin_write().unwrap();
            {
                let mut table = write_txn.open_table(TABLE).unwrap();
                table.remove(key.as_str()).unwrap();
            }
            write_txn.commit().unwrap();
        }
    }

    let read_txn = db.begin_read().unwrap();
    let table = read_txn.open_table(TABLE).unwrap();
    let count = table.len().unwrap();

    // Every key with (i % 50) whose *last* write in the cycle was a delete
    // ends absent; the rest hold their last-written value. Recompute the
    // expected final state directly to check against what's on disk.
    let mut expected_present = 0u64;
    let mut last_deleted = vec![false; 50];
    for i in 0..CYCLES {
        let slot = i % 50;
        last_deleted[slot] = i % 3 == 0;
    }
    for deleted in &last_deleted {
        if !deleted {
            expected_present += 1;
        }
    }

    let pass = count == expected_present;
    drop(table);
    drop(read_txn);
    drop(db);
    (
        json!({"pass": pass, "measurement": {"cycles": CYCLES, "final_count": count, "expected_count": expected_present}}),
        path,
    )
}

// --- size ---------------------------------------------------------

fn gate_size(churn_db_path: &Path) -> Value {
    let bytes = std::fs::metadata(churn_db_path).unwrap().len();
    // ~50 live keys of a few bytes each; redb pages are 4KiB, so a handful
    // of pages (a few hundred KiB) is reasonable, multi-megabyte is not.
    let max_reasonable_bytes = 4 * 1024 * 1024; // 4 MiB
    let pass = bytes <= max_reasonable_bytes;
    json!({"pass": pass, "measurement": {"bytes": bytes, "max_reasonable_bytes": max_reasonable_bytes}})
}

// --- latency ---------------------------------------------------------

fn gate_latency(dir: &Path) -> Value {
    let path = dir.join("latency.redb");
    {
        let db = Database::create(&path).unwrap();
        let write_txn = db.begin_write().unwrap();
        {
            let mut table = write_txn.open_table(TABLE).unwrap();
            for i in 0..500u64 {
                table.insert(format!("k{i}").as_str(), i).unwrap();
            }
        }
        write_txn.commit().unwrap();
    }

    let mut cold_ms = Vec::new();
    for _ in 0..10 {
        let start = Instant::now();
        let db = Database::open(&path).unwrap();
        cold_ms.push(start.elapsed().as_secs_f64() * 1000.0);
        drop(db);
    }

    let db = Database::open(&path).unwrap();
    let read_txn = db.begin_read().unwrap();
    let table = read_txn.open_table(TABLE).unwrap();
    let mut warm_ms = Vec::new();
    for i in 0..200u64 {
        let key = format!("k{}", i % 500);
        let start = Instant::now();
        let _ = table.get(key.as_str()).unwrap().unwrap().value();
        warm_ms.push(start.elapsed().as_secs_f64() * 1000.0);
    }

    json!({"cold_ms": cold_ms, "warm_ms": warm_ms})
}