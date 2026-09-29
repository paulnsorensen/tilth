//! Test-only capture of the Tilth extraction baseline for the extraction
//! reuse comparison experiment (see `.cheese/specs/tilth-extraction-reuse-comparison.md`).
//!
//! Reads the frozen fixture manifest under `benchmark/extraction/fixtures/`,
//! runs the existing crate-visible outline seams, and produces raw and
//! normalized capture records validated against the versioned JSON schemas
//! under `benchmark/extraction/schema/`. Generated output is written outside
//! tracked source, under `benchmark/extraction/.generated/`.
//!
//! This module is `#[cfg(test)]`-only: it changes no production behavior,
//! export, or visibility.

#![cfg(test)]

use std::fs;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use twox_hash::XxHash32;

use crate::lang::outline::{
    extract_import_source, get_deep_outline_entries, get_deep_outline_tree, get_outline_entries,
};
use crate::types::{Lang, OutlineEntry, OutlineKind};

const SUPPORTED_MANIFEST_VERSION: u64 = 1;
const RECORD_VERSION: u32 = 1;

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn fixtures_root() -> PathBuf {
    repo_root().join("benchmark/extraction/fixtures")
}

fn schema_root() -> PathBuf {
    repo_root().join("benchmark/extraction/schema")
}

fn generated_root() -> PathBuf {
    repo_root().join("benchmark/extraction/.generated")
}

// ---- manifest ----

#[derive(Debug, Deserialize)]
struct Manifest {
    fixtures: Vec<FixtureEntry>,
}

#[derive(Debug, Deserialize)]
struct FixtureEntry {
    id: String,
    source: String,
    language: String,
    hash: FixtureHash,
    capabilities: Capabilities,
}

#[derive(Debug, Deserialize)]
struct FixtureHash {
    algorithm: String,
    value: String,
}

#[derive(Debug, Deserialize)]
struct Capabilities {
    definitions: CapabilityExpectation,
    nesting: CapabilityExpectation,
    signatures: CapabilityExpectation,
    imports: CapabilityExpectation,
    edit_spans: CapabilityExpectation,
}

#[derive(Debug, Deserialize)]
struct CapabilityExpectation {
    applicability: String,
    #[serde(default)]
    expected: Option<serde_json::Value>,
    evidence: String,
}

/// Parse and validate a manifest document. Rejects an unknown `version` and
/// any record that does not match the fixture shape, distinguishing the two
/// failure reasons in the returned message.
fn parse_manifest(raw: &str) -> Result<Manifest, String> {
    let value: serde_json::Value =
        serde_json::from_str(raw).map_err(|e| format!("invalid JSON: {e}"))?;
    let version = value
        .get("version")
        .and_then(serde_json::Value::as_u64)
        .ok_or_else(|| "manifest missing integer version field".to_string())?;
    if version != SUPPORTED_MANIFEST_VERSION {
        return Err(format!(
            "unsupported manifest version {version}: this capture module supports version {SUPPORTED_MANIFEST_VERSION} only"
        ));
    }
    serde_json::from_value(value).map_err(|e| format!("malformed manifest: {e}"))
}

fn load_manifest() -> Manifest {
    let raw = fs::read_to_string(fixtures_root().join("manifest.json"))
        .expect("fixture manifest must exist");
    parse_manifest(&raw).expect("fixture manifest must be a valid version-1 record")
}

fn parse_lang(name: &str) -> Lang {
    match name {
        "rust" => Lang::Rust,
        "typescript" => Lang::TypeScript,
        "python" => Lang::Python,
        other => panic!("fixture manifest names an unsupported language: {other}"),
    }
}

/// xxHash32 (seed 0) of the raw file bytes, matching the hashing already used
/// for tilth's own content tags (`crate::edit::tag`). Not cryptographic;
/// sufficient to detect fixture drift against the frozen manifest.
fn hash_bytes(bytes: &[u8]) -> String {
    format!("{:08x}", XxHash32::oneshot(0, bytes))
}

// ---- raw record: preserves the native outline entry tree ----

#[derive(Debug, Serialize)]
struct RawEntry {
    kind: String,
    name: String,
    start_line: u32,
    span_start_line: u32,
    end_line: u32,
    signature: Option<String>,
    doc: Option<String>,
    children: Vec<RawEntry>,
}

impl RawEntry {
    fn from_outline(entry: &OutlineEntry) -> RawEntry {
        RawEntry {
            kind: format!("{:?}", entry.kind),
            name: entry.name.clone(),
            start_line: entry.start_line,
            span_start_line: entry.span_start_line,
            end_line: entry.end_line,
            signature: entry.signature.clone(),
            doc: entry.doc.clone(),
            children: entry.children.iter().map(RawEntry::from_outline).collect(),
        }
    }
}

#[derive(Debug, Serialize)]
struct RawRecord {
    version: u32,
    fixture_id: String,
    language: String,
    entries: Vec<RawEntry>,
}

// ---- normalized record: per-capability result, `unsupported` distinct from empty-success and error ----

#[derive(Debug, Serialize)]
#[serde(tag = "status", content = "value", rename_all = "snake_case")]
enum CapabilityResult {
    Supported(serde_json::Value),
    Unsupported,
    Error(String),
}

#[derive(Debug, Serialize)]
struct NormalizedRecord {
    version: u32,
    fixture_id: String,
    definitions: CapabilityResult,
    nesting: CapabilityResult,
    signatures: CapabilityResult,
    imports: CapabilityResult,
    edit_spans: CapabilityResult,
}

// ---- expected record: one per fixture x capability, projected from the manifest ----

#[derive(Debug, Serialize)]
struct ExpectedRecord {
    version: u32,
    fixture_id: String,
    capability: &'static str,
    applicability: String,
    expected: Option<serde_json::Value>,
    evidence: String,
}

// ---- capture ----

/// Distinguishes a capture that ran (even to an empty outline) from one that
/// failed before extraction could run at all (drifted hash, unreadable file,
/// non-UTF-8 bytes).
enum Capture {
    Ok {
        content: String,
        entries: Vec<OutlineEntry>,
        tree_entries: Vec<OutlineEntry>,
        top_level_entries: Vec<OutlineEntry>,
    },
    Error(String),
}

fn capture_fixture(entry: &FixtureEntry) -> Capture {
    let path = fixtures_root().join(&entry.source);
    let bytes = match fs::read(&path) {
        Ok(b) => b,
        Err(e) => return Capture::Error(format!("read {}: {e}", path.display())),
    };
    assert_eq!(
        entry.hash.algorithm, "xxh32",
        "fixture {} declares an unsupported hash algorithm",
        entry.id
    );
    let actual_hash = hash_bytes(&bytes);
    assert_eq!(
        actual_hash, entry.hash.value,
        "fixture {} source has drifted from its frozen manifest hash",
        entry.id
    );
    let content = match String::from_utf8(bytes) {
        Ok(s) => s,
        Err(e) => return Capture::Error(format!("non-utf8 fixture {}: {e}", path.display())),
    };
    let lang = parse_lang(&entry.language);
    let entries = get_deep_outline_entries(&content, lang);
    let tree_entries = get_deep_outline_tree(&content, lang);
    let top_level_entries = get_outline_entries(&content, lang);
    Capture::Ok {
        content,
        entries,
        tree_entries,
        top_level_entries,
    }
}

// ---- per-capability normalization ----

fn definitions_of(entries: &[OutlineEntry]) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for entry in entries {
        out.push((format!("{:?}", entry.kind), entry.name.clone()));
        out.extend(definitions_of(&entry.children));
    }
    out
}

fn nesting_of(entries: &[OutlineEntry], parent: Option<&str>) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for entry in entries {
        if let Some(parent_name) = parent {
            out.push((parent_name.to_string(), entry.name.clone()));
        }
        out.extend(nesting_of(&entry.children, Some(&entry.name)));
    }
    out
}

fn signatures_of(entries: &[OutlineEntry]) -> Vec<(String, Option<String>)> {
    let mut out = Vec::new();
    for entry in entries {
        out.push((entry.name.clone(), entry.signature.clone()));
        out.extend(signatures_of(&entry.children));
    }
    out
}

fn line_text(content: &str, line: u32) -> &str {
    content
        .split('\n')
        .nth((line as usize).saturating_sub(1))
        .unwrap_or("")
}

/// Imports are shallow, top-level declarations: walk `get_outline_entries`'
/// entries (which, unlike `get_deep_outline_entries`, retain `Import`-kind
/// entries) rather than the deep entry set.
fn imports_of(top_level_entries: &[OutlineEntry], lang: Lang, content: &str) -> Vec<String> {
    let mut out = Vec::new();
    for entry in top_level_entries {
        if entry.kind == OutlineKind::Import {
            let text = line_text(content, entry.start_line);
            out.push(extract_import_source(text, Some(lang)));
        }
        out.extend(imports_of(&entry.children, lang, content));
    }
    out
}

/// The exact owned source text for `span_start_line..=end_line` — the
/// semantic ownership span, not the display anchor — so decorators, doc
/// comments, and export wrappers that precede a declaration stay attached.
fn owned_source(content: &str, span_start_line: u32, end_line: u32) -> Option<String> {
    if span_start_line == 0 || end_line < span_start_line {
        return None;
    }
    let lines: Vec<&str> = content.split('\n').collect();
    let start_idx = (span_start_line - 1) as usize;
    let end_idx = end_line as usize;
    if start_idx >= lines.len() || end_idx > lines.len() {
        return None;
    }
    Some(lines[start_idx..end_idx].join("\n"))
}

fn edit_spans_of(entries: &[OutlineEntry], content: &str) -> Vec<(String, Option<String>)> {
    let mut out = Vec::new();
    for entry in entries {
        out.push((
            entry.name.clone(),
            owned_source(content, entry.span_start_line, entry.end_line),
        ));
        out.extend(edit_spans_of(&entry.children, content));
    }
    out
}

fn capability_result(
    capture: &Capture,
    applicability: &str,
    value: impl FnOnce() -> serde_json::Value,
) -> CapabilityResult {
    match capture {
        Capture::Error(msg) => CapabilityResult::Error(msg.clone()),
        Capture::Ok { .. } if applicability == "inapplicable" => CapabilityResult::Unsupported,
        Capture::Ok { .. } => CapabilityResult::Supported(value()),
    }
}

fn normalize(entry: &FixtureEntry, capture: &Capture) -> NormalizedRecord {
    let lang = parse_lang(&entry.language);
    let (content, entries, tree_entries, top_level_entries): (
        &str,
        &[OutlineEntry],
        &[OutlineEntry],
        &[OutlineEntry],
    ) = match capture {
        Capture::Ok {
            content,
            entries,
            tree_entries,
            top_level_entries,
        } => (
            content.as_str(),
            entries.as_slice(),
            tree_entries.as_slice(),
            top_level_entries.as_slice(),
        ),
        Capture::Error(_) => ("", &[], &[], &[]),
    };

    NormalizedRecord {
        version: RECORD_VERSION,
        fixture_id: entry.id.clone(),
        definitions: capability_result(
            capture,
            &entry.capabilities.definitions.applicability,
            || serde_json::to_value(definitions_of(entries)).expect("serialize definitions"),
        ),
        nesting: capability_result(capture, &entry.capabilities.nesting.applicability, || {
            serde_json::to_value(nesting_of(tree_entries, None)).expect("serialize nesting")
        }),
        signatures: capability_result(
            capture,
            &entry.capabilities.signatures.applicability,
            || serde_json::to_value(signatures_of(entries)).expect("serialize signatures"),
        ),
        imports: capability_result(capture, &entry.capabilities.imports.applicability, || {
            serde_json::to_value(imports_of(top_level_entries, lang, content))
                .expect("serialize imports")
        }),
        edit_spans: capability_result(
            capture,
            &entry.capabilities.edit_spans.applicability,
            || serde_json::to_value(edit_spans_of(entries, content)).expect("serialize edit spans"),
        ),
    }
}

fn expected_records(entry: &FixtureEntry) -> Vec<ExpectedRecord> {
    let caps: [(&'static str, &CapabilityExpectation); 5] = [
        ("definitions", &entry.capabilities.definitions),
        ("nesting", &entry.capabilities.nesting),
        ("signatures", &entry.capabilities.signatures),
        ("imports", &entry.capabilities.imports),
        ("edit_spans", &entry.capabilities.edit_spans),
    ];
    caps.into_iter()
        .map(|(name, cap)| ExpectedRecord {
            version: RECORD_VERSION,
            fixture_id: entry.id.clone(),
            capability: name,
            applicability: cap.applicability.clone(),
            expected: cap.expected.clone(),
            evidence: cap.evidence.clone(),
        })
        .collect()
}

// ---- schema validation ----

fn load_schema(schema_file: &str) -> serde_json::Value {
    let raw = fs::read_to_string(schema_root().join(schema_file))
        .unwrap_or_else(|e| panic!("read schema {schema_file}: {e}"));
    serde_json::from_str(&raw).unwrap_or_else(|e| panic!("parse schema {schema_file}: {e}"))
}

fn schema_accepts(schema_file: &str, instance: &serde_json::Value) -> bool {
    let schema = load_schema(schema_file);
    let compiled = jsonschema::JSONSchema::compile(&schema)
        .unwrap_or_else(|e| panic!("compile schema {schema_file}: {e}"));
    compiled.is_valid(instance)
}

fn assert_matches_schema(schema_file: &str, instance: &serde_json::Value) {
    let schema = load_schema(schema_file);
    let compiled = jsonschema::JSONSchema::compile(&schema)
        .unwrap_or_else(|e| panic!("compile schema {schema_file}: {e}"));
    let result = compiled.validate(instance);
    if let Err(errors) = result {
        let messages: Vec<String> = errors.map(|e| e.to_string()).collect();
        panic!("{schema_file} rejected instance: {}", messages.join("; "));
    }
}

fn write_generated(relative: &str, value: &serde_json::Value) {
    let path = generated_root().join(relative);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).expect("create generated output dir");
    }
    let pretty = serde_json::to_string_pretty(value).expect("serialize generated record");
    fs::write(&path, pretty).expect("write generated record");
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The full pipeline: every fixture in the frozen manifest captures
    /// without a hash-drift panic, its raw and normalized records validate
    /// against the versioned schemas, and generated output lands outside
    /// tracked source.
    #[test]
    fn baseline_capture_matches_versioned_record_format() {
        let manifest = load_manifest();
        assert!(
            !manifest.fixtures.is_empty(),
            "manifest must declare at least one fixture"
        );

        for entry in &manifest.fixtures {
            let capture = capture_fixture(entry);

            let raw_entries = match &capture {
                Capture::Ok { entries, .. } => entries.iter().map(RawEntry::from_outline).collect(),
                Capture::Error(_) => Vec::new(),
            };
            let raw = RawRecord {
                version: RECORD_VERSION,
                fixture_id: entry.id.clone(),
                language: entry.language.clone(),
                entries: raw_entries,
            };
            let raw_value = serde_json::to_value(&raw).expect("serialize raw record");
            assert_matches_schema("raw-record.v1.schema.json", &raw_value);
            write_generated(&format!("raw/{}.json", entry.id), &raw_value);

            let normalized = normalize(entry, &capture);
            let normalized_value =
                serde_json::to_value(&normalized).expect("serialize normalized record");
            assert_matches_schema("normalized-record.v1.schema.json", &normalized_value);
            write_generated(&format!("normalized/{}.json", entry.id), &normalized_value);

            for expected in expected_records(entry) {
                let expected_value =
                    serde_json::to_value(&expected).expect("serialize expected record");
                assert_matches_schema("expected-record.v1.schema.json", &expected_value);
            }

            if let Capture::Error(msg) = &capture {
                assert!(
                    !msg.is_empty(),
                    "capture error for fixture {} must carry a reason",
                    entry.id
                );
            }
        }
    }

    /// Every fixture's declared hash matches its current file bytes — the
    /// manifest freeze has not drifted from the tracked source.
    #[test]
    fn all_fixture_hashes_match_frozen_manifest() {
        let manifest = load_manifest();
        for entry in &manifest.fixtures {
            let path = fixtures_root().join(&entry.source);
            let bytes =
                fs::read(&path).unwrap_or_else(|e| panic!("read fixture {}: {e}", path.display()));
            assert_eq!(
                hash_bytes(&bytes),
                entry.hash.value,
                "fixture {} hash drifted from the frozen manifest",
                entry.id
            );
        }
    }

    #[test]
    fn manifest_rejects_unknown_version() {
        let raw = r#"{"version": 2, "fixtures": []}"#;
        let err = parse_manifest(raw).expect_err("version 2 manifest must be rejected");
        assert!(
            err.contains("unsupported manifest version"),
            "unexpected error: {err}"
        );
    }

    #[test]
    fn manifest_rejects_malformed_record() {
        let raw = r#"{"version": 1, "fixtures": [{"id": "x"}]}"#;
        let err =
            parse_manifest(raw).expect_err("fixture missing required fields must be rejected");
        assert!(
            err.contains("malformed manifest"),
            "unexpected error: {err}"
        );
    }

    #[test]
    fn manifest_matches_versioned_schema() {
        let raw = fs::read_to_string(fixtures_root().join("manifest.json"))
            .expect("fixture manifest must exist");
        let value: serde_json::Value =
            serde_json::from_str(&raw).expect("fixture manifest must be valid JSON");
        assert_matches_schema("manifest.v1.schema.json", &value);
    }

    #[test]
    fn normalized_record_schema_rejects_unknown_version() {
        let mut record = serde_json::json!({
            "version": 2,
            "fixture_id": "x",
            "definitions": {"status": "unsupported"},
            "nesting": {"status": "unsupported"},
            "signatures": {"status": "unsupported"},
            "imports": {"status": "unsupported"},
            "edit_spans": {"status": "unsupported"}
        });
        assert!(
            !schema_accepts("normalized-record.v1.schema.json", &record),
            "a version-2 normalized record must be rejected"
        );
        record["version"] = serde_json::json!(1);
        assert!(
            schema_accepts("normalized-record.v1.schema.json", &record),
            "a version-1 normalized record with unsupported statuses must be accepted"
        );
    }

    #[test]
    fn normalized_record_schema_rejects_unknown_capability_status() {
        let record = serde_json::json!({
            "version": 1,
            "fixture_id": "x",
            "definitions": {"status": "bogus"},
            "nesting": {"status": "unsupported"},
            "signatures": {"status": "unsupported"},
            "imports": {"status": "unsupported"},
            "edit_spans": {"status": "unsupported"}
        });
        assert!(
            !schema_accepts("normalized-record.v1.schema.json", &record),
            "an unrecognized capability status must be rejected"
        );
    }

    /// An `error` capability result must carry its reason as `value`; a
    /// record that omits it is malformed.
    #[test]
    fn normalized_record_schema_requires_error_value() {
        let record = serde_json::json!({
            "version": 1,
            "fixture_id": "x",
            "definitions": {"status": "error"},
            "nesting": {"status": "unsupported"},
            "signatures": {"status": "unsupported"},
            "imports": {"status": "unsupported"},
            "edit_spans": {"status": "unsupported"}
        });
        assert!(
            !schema_accepts("normalized-record.v1.schema.json", &record),
            "an error status without a reason must be rejected"
        );
    }

    fn fixture_by_id<'a>(manifest: &'a Manifest, id: &str) -> &'a FixtureEntry {
        manifest
            .fixtures
            .iter()
            .find(|f| f.id == id)
            .unwrap_or_else(|| panic!("manifest must declare fixture {id}"))
    }

    fn supported_value(result: &CapabilityResult) -> &serde_json::Value {
        match result {
            CapabilityResult::Supported(value) => value,
            other => panic!("expected a supported capability result, got {other:?}"),
        }
    }

    /// `get_deep_outline_entries` clears every entry's `children`, so a
    /// nesting capability sourced from it can only ever produce an empty
    /// list. Nesting must instead walk `get_deep_outline_tree`'s real
    /// parent-child hierarchy: guard against regressing back onto the flat
    /// seam by asserting real nested fixtures capture non-empty pairs.
    #[test]
    fn nesting_capability_reflects_real_hierarchy_not_flat_seam() {
        let manifest = load_manifest();

        let rust_entry = fixture_by_id(&manifest, "rust-core");
        let rust_capture = capture_fixture(rust_entry);
        let rust_normalized = normalize(rust_entry, &rust_capture);
        let rust_nesting = supported_value(&rust_normalized.nesting);
        assert_eq!(
            rust_nesting,
            &serde_json::json!([["inner", "compute"], ["inner", "deep"], ["deep", "compute"]]),
            "rust-core nesting must reflect its real mod/fn hierarchy"
        );

        let ts_entry = fixture_by_id(&manifest, "typescript-core");
        let ts_capture = capture_fixture(ts_entry);
        let ts_normalized = normalize(ts_entry, &ts_capture);
        let ts_nesting = supported_value(&ts_normalized.nesting);
        assert_eq!(
            ts_nesting,
            &serde_json::json!([
                ["Service", "run"],
                ["inner", "compute"],
                ["inner", "deep"],
                ["deep", "compute"]
            ]),
            "typescript-core nesting must reflect its real class/namespace hierarchy"
        );

        let py_entry = fixture_by_id(&manifest, "python-core");
        let py_capture = capture_fixture(py_entry);
        let py_normalized = normalize(py_entry, &py_capture);
        let py_nesting = supported_value(&py_normalized.nesting);
        assert_eq!(
            py_nesting,
            &serde_json::json!([
                ["<module>", "compute"],
                ["<module>", "Inner"],
                ["Inner", "compute"],
                ["Inner", "Deep"],
                ["Deep", "compute"]
            ]),
            "python-core nesting must reflect its real class hierarchy"
        );
    }

    /// `get_deep_outline_entries` filters to `is_path_line_entry_kind`, which
    /// excludes `OutlineKind::Import`, so an imports capability sourced from
    /// it can only ever produce an empty list. Imports must instead walk
    /// `get_outline_entries`, the shallow seam that retains Import-kind
    /// entries: guard against regressing back onto the filtered seam by
    /// asserting the declared import sources surface.
    #[test]
    fn imports_capability_reflects_declared_sources_not_filtered_seam() {
        let manifest = load_manifest();

        let rust_entry = fixture_by_id(&manifest, "rust-imports_exports");
        let rust_capture = capture_fixture(rust_entry);
        let rust_normalized = normalize(rust_entry, &rust_capture);
        let rust_imports = supported_value(&rust_normalized.imports);
        assert_eq!(
            rust_imports,
            &serde_json::json!([
                "std::collections::HashMap as Map",
                "std::fmt",
                "reexported_compute"
            ]),
            "rust-imports_exports imports must surface its declared use sources"
        );

        let ts_entry = fixture_by_id(&manifest, "typescript-imports_exports");
        let ts_capture = capture_fixture(ts_entry);
        let ts_normalized = normalize(ts_entry, &ts_capture);
        let ts_imports = supported_value(&ts_normalized.imports);
        assert_eq!(
            ts_imports,
            &serde_json::json!(["fs", "path"]),
            "typescript-imports_exports imports must surface its declared import sources"
        );

        let py_entry = fixture_by_id(&manifest, "python-imports_exports");
        let py_capture = capture_fixture(py_entry);
        let py_normalized = normalize(py_entry, &py_capture);
        let py_imports = supported_value(&py_normalized.imports);
        assert_eq!(
            py_imports,
            &serde_json::json!(["sys as system"]),
            "python-imports_exports imports must surface the plain `import` source it can capture"
        );
    }
}
