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
    // `.min.<ext>` — stem itself ends in `.min`. The `secondary > 0` guard
    // skips hidden-file forms like `.min.config` where the leading dot is
    // a POSIX hidden-file marker, not an extension separator.
    if let Some(secondary) = stem.rfind('.') {
        if secondary > 0 && stem[secondary + 1..].eq_ignore_ascii_case("min") {
            return true;
        }
    }
    // `-min.<ext>` (e.g. `bundle-min.js`). Compare in place to avoid the
    // per-call lowercase allocation.  Use char-boundary-safe slicing:
    // `stem.len() - 4` is a byte offset that may land inside a multi-byte
    // UTF-8 character, so use `char_indices().nth_back()` instead.
    if let Some(start) = stem.char_indices().nth_back(3).map(|(i, _)| i) {
        if stem[start..].eq_ignore_ascii_case("-min") {
            return true;
        }
    }
    false
}

/// Heuristic: does this content look minified? Samples first 2KB and counts
/// newlines. Real source code is line-oriented; minified bundles cram
/// thousands of bytes onto a single line.
///
/// Only call this on files >= [`MINIFIED_CHECK_THRESHOLD`] — for small files
/// the cost of parsing is bounded regardless and false positives are noisier
/// than just letting them through. Threshold of `< 2` newlines (i.e., 0 or 1)
/// in 2KB is a strong signal: real source has line breaks every ~80 bytes,
/// while minified bundles routinely have zero. A threshold of `< 4` would
/// flag legitimate single-block license headers and compact one-line JSON.
pub fn is_minified_by_content(buf: &[u8]) -> bool {
    let sample = &buf[..buf.len().min(2048)];
    let newlines = memchr::memchr_iter(b'\n', sample).count();
    newlines < 2
}

/// Built-in secrets denylist, matched against a file's basename. Search may
/// still list a matching path as a hit, but the result formatter never inlines
/// the file's contents (matched lines, outline, or expanded body) — the agent
/// must `tilth_read` it deliberately. This is defense-in-depth on top of the
/// repo-author `.tilthignore` knob: it protects `.env`/key material even when a
/// repo has no `.tilthignore`.
///
/// Conservative by design: false positives only cost an inline preview (the
/// path is still findable and readable), so the bias is toward redacting
/// anything that commonly holds credentials.
pub fn is_secret_file(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    let ext = lower.rsplit_once('.').map(|(_, e)| e);

    // Public keys are safe to inline; their private counterparts are not.
    if ext == Some("pub") {
        return false;
    }

    // `.env` family — but allow committed, secret-free templates.
    if lower == ".env" {
        return true;
    }
    if let Some(variant) = lower.strip_prefix(".env.") {
        return !matches!(
            variant,
            "example" | "sample" | "template" | "dist" | "defaults"
        );
    }

    // Exact-name credential stores.
    if matches!(
        lower.as_str(),
        ".netrc" | ".npmrc" | ".pgpass" | ".htpasswd"
    ) {
        return true;
    }

    // `credentials`, `credentials.json`, `aws_credentials`, …
    if lower.starts_with("credentials") {
        return true;
    }

    // Private key / certificate material by extension.
    if matches!(
        ext,
        Some("pem" | "key" | "p12" | "pfx" | "pkcs12" | "keystore" | "jks")
    ) {
        return true;
    }

    // SSH/PGP private keys: `id_rsa`, `id_ed25519`, `server_dsa`, … The `.pub`
    // companions already returned false above.
    ["_rsa", "_dsa", "_ecdsa", "_ed25519"]
        .iter()
        .any(|marker| lower.contains(marker))
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

    /// Regression: multi-byte UTF-8 filenames must not panic when slicing
    /// the stem suffix. `stem.len() - 4` is a byte offset, not a char
    /// offset — it can land inside a CJK or emoji character.
    #[test]
    fn minified_filename_multibyte_utf8() {
        // CJK: each character is 3 bytes in UTF-8.
        assert!(!is_minified_by_name("日本語ファイル.js"));
        assert!(is_minified_by_name("パネル-min.js"));
        assert!(is_minified_by_name("データ.min.css"));
        // Short name: stem < 4 chars, `nth_back(3)` returns None.
        assert!(!is_minified_by_name("設定.md"));
        // Emoji: 4 bytes each in UTF-8.
        assert!(!is_minified_by_name("🚀📦🎯📋.js"));
    }

    /// A leading dot is a hidden-file marker, not an extension separator —
    /// `.min.config` is a config file named `min`, not a minified file.
    #[test]
    fn minified_filename_hidden_files_not_flagged() {
        assert!(!is_minified_by_name(".min.config"));
        assert!(!is_minified_by_name(".min.env"));
        assert!(!is_minified_by_name(".min.json"));
    }

    #[test]
    fn secret_env_family() {
        // `.ENV` confirms case-insensitive matching.
        for name in [".env", ".env.local", ".env.production", ".ENV"] {
            assert!(is_secret_file(name), "{name} should be a secret");
        }
        // Templates are committed and secret-free.
        for name in [".env.example", ".env.sample", ".env.template"] {
            assert!(
                !is_secret_file(name),
                "{name} template should not be a secret"
            );
        }
    }

    #[test]
    fn secret_key_material() {
        for name in [
            "server.key",
            "tls.pem",
            "cert.p12",
            "store.jks",
            "id_rsa",
            "id_ed25519",
            "deploy_dsa",
        ] {
            assert!(is_secret_file(name), "{name} should be a secret");
        }
        // Public keys are safe to inline; their private counterparts are not.
        for name in ["id_rsa.pub", "id_ed25519.pub"] {
            assert!(
                !is_secret_file(name),
                "{name} (public key) should not be a secret"
            );
        }
    }

    #[test]
    fn secret_credential_stores() {
        for name in [
            ".netrc",
            ".npmrc",
            ".pgpass",
            ".htpasswd",
            "credentials",
            "credentials.json",
        ] {
            assert!(is_secret_file(name), "{name} should be a secret");
        }
    }

    #[test]
    fn secret_negatives() {
        for name in [
            "main.rs",
            "config.yaml",
            "README.md",
            ".gitignore",
            "Cargo.toml",
        ] {
            assert!(!is_secret_file(name), "{name} should not be a secret");
        }
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
