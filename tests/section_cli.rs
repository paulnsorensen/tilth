//! `tilth <file> --section N-M` completes a construct that the range end
//! cuts, the same way `tilth_read` completes `path#N-M`.

use std::fmt::Write as _;
use std::path::Path;
use std::process::Command;

/// Lines 1-18: `fn head` and a blank line. Lines 19-31: `fn body`.
/// Line 33: `fn tail`.
fn rust_source() -> String {
    let mut text = String::from("fn head() {\n");
    for _ in 0..15 {
        text.push_str("    let a = 1;\n");
    }
    text.push_str("}\n\nfn body() -> i32 {\n");
    for _ in 0..10 {
        text.push_str("    let x = 1;\n");
    }
    text.push_str("    x\n}\n\nfn tail() {}\n");
    text
}

fn tilth(dir: &Path, args: &[&str]) -> String {
    let output = Command::new(env!("CARGO_BIN_EXE_tilth"))
        .args(args)
        .current_dir(dir)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "tilth {args:?} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap()
}

fn fixture(name: &str, text: &str) -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join(name), text).unwrap();
    dir
}

/// The numbered lines of a CLI section read, as `(number, text)`.
fn numbered(output: &str) -> Vec<(u32, String)> {
    let mut lines = Vec::new();
    for line in output.lines() {
        let trimmed = line.trim_start();
        let Some((number, text)) = trimmed.split_once("  ") else {
            continue;
        };
        if let Ok(number) = number.parse() {
            lines.push((number, text.to_string()));
        }
    }
    lines
}

fn line_numbers(output: &str) -> Vec<u32> {
    numbered(output).into_iter().map(|(n, _)| n).collect()
}

#[test]
fn a_range_that_cuts_a_function_grows_to_its_end() {
    let dir = fixture("f.rs", &rust_source());
    let output = tilth(dir.path(), &["f.rs", "--section", "1-22"]);
    assert_eq!(
        line_numbers(&output),
        (1..=31).collect::<Vec<_>>(),
        "{output}"
    );
    assert!(!output.contains("Next:"), "{output}");
}

#[test]
fn a_small_budget_stops_before_the_function_and_names_the_next_call() {
    let dir = fixture("f.rs", &rust_source());
    let output = tilth(
        dir.path(),
        &["f.rs", "--section", "1-22", "--budget", "150"],
    );
    assert_eq!(
        line_numbers(&output),
        (1..=18).collect::<Vec<_>>(),
        "{output}"
    );
    // The note names the file as the header does: by its absolute path.
    let path = dir.path().canonicalize().unwrap().join("f.rs");
    let path = path.to_str().unwrap();
    let last = output.lines().last().unwrap_or_default();
    assert_eq!(
        last,
        format!(
            "... stopped before a construct that does not fit this response. \
             Next: tilth {path} --section 19-31"
        ),
        "{output}"
    );
    let next = tilth(dir.path(), &[path, "--section", "19-31"]);
    assert_eq!(line_numbers(&next), (19..=31).collect::<Vec<_>>(), "{next}");
}

#[test]
fn python_ranges_grow_to_a_function_end_but_never_to_the_module_end() {
    let mut text = String::from("def f():\n    a = 1\n    b = 2\n\n");
    for i in 1..=4 {
        let _ = writeln!(text, "v{i} = {i}");
    }
    let dir = fixture("m.py", &text);
    let grown = tilth(dir.path(), &["m.py", "--section", "1-2"]);
    assert_eq!(line_numbers(&grown), [1, 2, 3], "{grown}");
    let top_level = tilth(dir.path(), &["m.py", "--section", "5-6"]);
    assert_eq!(line_numbers(&top_level), [5, 6], "{top_level}");
}
