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
        Ok(sha) => format!("{package}+{sha}"),
        Err(reason) => {
            retry_on_next_build(&reason);
            package.to_owned()
        }
    };
    println!("cargo:rustc-env=TILTH_VERSION={version}");
}

/// The short commit of this package's repository, or the reason git gave none.
fn short_head() -> Result<String, String> {
    // `git rev-parse` walks up to any enclosing repository and obeys `GIT_DIR`, so an
    // unrelated repository would give its commit. Use the commit only when the git top
    // level is this package.
    let toplevel = git(&["rev-parse", "--show-toplevel"])?;
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    match (
        Path::new(&toplevel).canonicalize(),
        Path::new(manifest_dir).canonicalize(),
    ) {
        (Ok(toplevel), Ok(manifest_dir)) if toplevel == manifest_dir => {}
        _ => return Err(format!("the git top level is {toplevel}")),
    }
    let sha = git(&["rev-parse", "--short", "HEAD"])?;
    watch_head();
    Ok(sha)
}

/// A checkout with git metadata that gave no commit must not keep that result. The cause can
/// be one failed git call, a sandbox that hides the repository, or no git on `PATH`. Without
/// a watched git file, Cargo would not run this script again, and each later build would
/// lack the commit. A watched path that does not exist makes Cargo run the script on each
/// build until git gives the commit. A package without git metadata stays quiet.
fn retry_on_next_build(reason: &str) {
    if !Path::new(env!("CARGO_MANIFEST_DIR")).join(".git").exists() {
        return;
    }
    println!("cargo:rerun-if-changed=.git/tilth-version-retry");
    println!("cargo:warning=`tilth --version` has no commit suffix: {reason}");
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
    if let Ok(branch) = git(&["symbolic-ref", "-q", "HEAD"]) {
        files.push(branch);
    }
    for file in files {
        let Ok(path) = git(&["rev-parse", "--git-path", &file]) else {
            continue;
        };
        // Cargo reruns on every build for a path that does not exist, so skip absent files.
        if Path::new(&path).exists() {
            println!("cargo:rerun-if-changed={path}");
        }
    }
}

/// The trimmed output of one git command, or one line that says why there is none.
fn git(args: &[&str]) -> Result<String, String> {
    let command = format!("git {}", args.join(" "));
    let output = Command::new("git")
        .args(args)
        .output()
        .map_err(|error| format!("{command} did not start ({error})"))?;
    let text = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    if output.status.success() && !text.is_empty() {
        return Ok(text);
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    let detail = stderr.lines().next().unwrap_or("no output").trim();
    Err(format!("{command} failed ({detail})"))
}
