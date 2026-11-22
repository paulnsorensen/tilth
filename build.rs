//! Sets `TILTH_VERSION`: the package version plus the short git commit when the build has git
//! metadata, so `tilth --version` shows a stale binary. A build without git (source tarball,
//! Nix sandbox) gets the package version alone. A package that is not the top level of its
//! repository (for example, a tarball inside another repository) also gets the package
//! version alone.

use std::path::Path;
use std::process::Command;

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-env-changed=GIT_DIR");
    let package = env!("CARGO_PKG_VERSION");
    let version = match short_head() {
        Some(sha) => format!("{package}+{sha}"),
        None => package.to_owned(),
    };
    println!("cargo:rustc-env=TILTH_VERSION={version}");
}

fn short_head() -> Option<String> {
    if !is_repository_root() {
        return None;
    }
    let sha = git(&["rev-parse", "--short", "HEAD"])?;
    watch_head();
    Some(sha)
}

/// True when the git top level is this package. `git rev-parse` walks up to any enclosing
/// repository and obeys `GIT_DIR`, so without this check an unrelated repository gives its
/// commit.
fn is_repository_root() -> bool {
    let Some(toplevel) = git(&["rev-parse", "--show-toplevel"]) else {
        return false;
    };
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    match (
        Path::new(&toplevel).canonicalize(),
        Path::new(manifest_dir).canonicalize(),
    ) {
        (Ok(toplevel), Ok(manifest_dir)) => toplevel == manifest_dir,
        _ => false,
    }
}

/// Rerun when HEAD moves. `--git-path` resolves each file in a linked worktree too, where
/// `.git` is a file. It prints a path relative to the package root or an absolute path;
/// Cargo accepts both. A checkout rewrites `HEAD`. A commit rewrites the loose branch ref,
/// but after `git pack-refs` the branch has no loose ref until the commit creates one.
/// The HEAD reflog `logs/HEAD` changes on every commit, checkout, reset, and rebase, so it
/// catches that case.
fn watch_head() {
    let mut files = vec![
        "HEAD".to_owned(),
        "logs/HEAD".to_owned(),
        "packed-refs".to_owned(),
    ];
    if let Some(branch) = git(&["symbolic-ref", "-q", "HEAD"]) {
        files.push(branch);
    }
    for file in files {
        let Some(path) = git(&["rev-parse", "--git-path", &file]) else {
            continue;
        };
        // Cargo reruns on every build for a path that does not exist, so skip absent files.
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
