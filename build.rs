//! Sets `TILTH_VERSION`: the package version plus the short git commit when the build has git
//! metadata, so `tilth --version` shows a stale binary. A build without git (source tarball,
//! Nix sandbox) gets the package version alone.

use std::path::Path;
use std::process::Command;

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    let package = env!("CARGO_PKG_VERSION");
    let version = match short_head() {
        Some(sha) => format!("{package}+{sha}"),
        None => package.to_owned(),
    };
    println!("cargo:rustc-env=TILTH_VERSION={version}");
}

fn short_head() -> Option<String> {
    let sha = git(&["rev-parse", "--short", "HEAD"])?;
    watch_head();
    Some(sha)
}

/// Rerun when HEAD moves. `--git-path` resolves each file in a linked worktree too, where
/// `.git` is a file. It prints a path relative to the package root or an absolute path;
/// Cargo accepts both. A commit on a branch rewrites its loose ref or `packed-refs`; a
/// checkout rewrites `HEAD`.
fn watch_head() {
    let mut files = vec!["HEAD".to_owned(), "packed-refs".to_owned()];
    if let Some(branch) = git(&["symbolic-ref", "-q", "HEAD"]) {
        files.push(branch);
    }
    for file in files {
        let Some(path) = git(&["rev-parse", "--git-path", &file]) else {
            continue;
        };
        // Cargo reruns on every build for a path that does not exist, so skip absent refs.
        if Path::new(&path).exists() {
            println!("cargo:rerun-if-changed={path}");
        }
    }
}

fn git(args: &[&str]) -> Option<String> {
    let output = Command::new("git").args(args).output().ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8(output.stdout).ok()?;
    let text = text.trim();
    (!text.is_empty()).then(|| text.to_owned())
}
