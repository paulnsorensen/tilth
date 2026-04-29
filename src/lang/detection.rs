/// Any null byte in the first 512 bytes → binary.
/// Uses memchr for the scan — single SIMD pass, no branching.
pub fn is_binary(buf: &[u8]) -> bool {
    let window = &buf[..buf.len().min(512)];
    memchr::memchr(0, window).is_some()
}

/// Check filename against known generated/lock files.
pub fn is_generated_by_name(name: &str) -> bool {
    matches!(
        name,
        "package-lock.json"
            | "yarn.lock"
            | "pnpm-lock.yaml"
            | "Cargo.lock"
            | "composer.lock"
            | "Gemfile.lock"
            | "poetry.lock"
            | "go.sum"
            | "bun.lockb"
    )
}

const GENERATED_MARKERS: &[&[u8]] = &[
    b"@generated",
    b"DO NOT EDIT",
    b"Do not edit",
    b"do not edit",
    b"auto-generated",
    b"Auto-generated",
    b"AUTO-GENERATED",
    b"this file is generated",
    b"This file is generated",
    b"THIS FILE IS GENERATED",
    b"automatically generated",
    b"Automatically generated",
];

/// Scan first 512 bytes for generated-file markers using SIMD memmem.
pub fn is_generated_by_content(buf: &[u8]) -> bool {
    let window = &buf[..buf.len().min(512)];
    GENERATED_MARKERS
        .iter()
        .any(|m| memchr::memmem::find(window, m).is_some())
}

/// Below this size minification doesn't matter — parsing is cheap regardless.
pub const MINIFIED_CHECK_THRESHOLD: u64 = 100_000;

/// Check filename for the `.min.` / `-min.` minification convention.
/// Strong, decade-old industry convention — `.min.js`, `app.min.css`,
/// `bundle-min.js`. Bundler defaults like `vendor.js` or `bundle.js`
/// are not flagged here; the content heuristic catches those.
pub fn is_minified_by_name(name: &str) -> bool {
    let Some(stem_end) = name.rfind('.') else {
        return false;
    };
    let stem = &name[..stem_end];
    // `.min.<ext>` — stem itself ends in `.min`
    if let Some(secondary) = stem.rfind('.') {
        if stem[secondary + 1..].eq_ignore_ascii_case("min") {
            return true;
        }
    }
    // `-min.<ext>` (e.g. `bundle-min.js`)
    let lower = stem.to_ascii_lowercase();
    lower.ends_with("-min")
}

/// Heuristic: does this content look minified? Samples first 2KB and counts
/// newlines. Real source code is line-oriented; minified bundles cram
/// thousands of bytes onto a single line.
///
/// Only call this on files >= [`MINIFIED_CHECK_THRESHOLD`] — for small files
/// the cost of parsing is bounded regardless and false positives are noisier
/// than just letting them through.
pub fn is_minified_by_content(buf: &[u8]) -> bool {
    let sample = &buf[..buf.len().min(2048)];
    let newlines = memchr::memchr_iter(b'\n', sample).count();
    newlines < 4
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn minified_filename_dot_min() {
        assert!(is_minified_by_name("app.min.js"));
        assert!(is_minified_by_name("vendor.MIN.css"));
        assert!(is_minified_by_name("foo.bar.min.js"));
    }

    #[test]
    fn minified_filename_dash_min() {
        assert!(is_minified_by_name("bundle-min.js"));
        assert!(is_minified_by_name("app-MIN.css"));
    }

    #[test]
    fn minified_filename_negatives() {
        assert!(!is_minified_by_name("app.js"));
        assert!(!is_minified_by_name("vendor.js"));
        assert!(!is_minified_by_name("bundle.js"));
        assert!(!is_minified_by_name("admin.js"));
        assert!(!is_minified_by_name("README.md"));
        assert!(!is_minified_by_name("noext"));
    }

    #[test]
    fn minified_content_dense() {
        // Single long line — typical minified output.
        let bundle = "var a=1,b=2,c=3;function f(x){return x+1}var d=4;".repeat(80);
        assert!(is_minified_by_content(bundle.as_bytes()));
    }

    #[test]
    fn minified_content_normal_source() {
        let src = "fn main() {\n    let x = 1;\n    let y = 2;\n    println!(\"{x} {y}\");\n}\n"
            .repeat(20);
        assert!(!is_minified_by_content(src.as_bytes()));
    }
}
