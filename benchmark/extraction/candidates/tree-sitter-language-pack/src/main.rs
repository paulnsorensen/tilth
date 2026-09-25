//! Capture entrypoint for the tree-sitter-language-pack candidate in the
//! Tilth extraction reuse comparison experiment. See
//! `benchmark/extraction/README.md` for setup and usage.
//!
//! `provision` (network) warms the runtime grammar cache under
//! `~/Library/Caches/tree-sitter-language-pack/`. `capture` (offline) reads
//! the frozen fixture manifest, runs each language's bundled `tags.scm`
//! query via the `tree-sitter` crate's own `Query`/`QueryCursor`, and emits
//! versioned raw/normalized records. `capture` never downloads a grammar:
//! `get_language` only reuses what `provision` already cached.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

use clap::{Parser as ClapArgs, Subcommand};
use serde::{Deserialize, Serialize};
use tree_sitter::{Parser as TsParser, Query, QueryCursor, StreamingIterator};
use tree_sitter_language_pack::get_language;
use twox_hash::XxHash32;

const RECORD_VERSION: u32 = 1;
const SUPPORTED_MANIFEST_VERSION: u64 = 1;
const PROVISION_LANGUAGES: [&str; 3] = ["rust", "typescript", "python"];

#[derive(ClapArgs)]
#[command(name = "tsl-pack-candidate")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Network: warm the runtime grammar cache for rust/typescript/python.
    Provision,
    /// Offline: capture raw+normalized v1 records from the frozen fixture manifest.
    Capture {
        #[arg(long)]
        manifest: Option<PathBuf>,
        #[arg(long)]
        out: Option<PathBuf>,
    },
}

fn main() {
    let cli = Cli::parse();
    match cli.command {
        Command::Provision => provision(),
        Command::Capture { manifest, out } => capture(manifest, out),
    }
}

fn provision() {
    let mut failed = false;
    for lang in PROVISION_LANGUAGES {
        match get_language(lang) {
            Ok(_) => println!("provisioned {lang}: ok"),
            Err(e) => {
                failed = true;
                println!("provisioned {lang}: failed: {e}");
            }
        }
    }
    if failed {
        std::process::exit(1);
    }
}

// ---- manifest (mirrors src/extraction_probe.rs's frozen-manifest contract) ----

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
}

#[derive(Debug, Deserialize)]
struct FixtureHash {
    algorithm: String,
    value: String,
}

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

fn hash_bytes(bytes: &[u8]) -> String {
    format!("{:08x}", XxHash32::oneshot(0, bytes))
}

// ---- raw record (schema: raw-record.v1.schema.json) ----

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

#[derive(Debug, Serialize)]
struct RawRecord {
    version: u32,
    fixture_id: String,
    language: String,
    entries: Vec<RawEntry>,
}

// ---- normalized record (schema: normalized-record.v1.schema.json) ----

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

// ---- captured tag: one @definition.* match from the bundled tags.scm ----

struct TagDefinition {
    /// The suffix after "definition.", e.g. "function", "class", "module".
    kind: String,
    name: String,
    start_line: u32,
    end_line: u32,
    text: String,
}

enum Capture {
    Ok { definitions: Vec<TagDefinition> },
    Error(String),
}

/// Runs the bundled `tags.scm` for `lang` over `content` and collects every
/// `@definition.*` match paired with its sibling `@name` capture. `lang` must
/// already be cached by `provision` (or a static build) — this never
/// downloads. Reference captures (`@reference.*`) are ignored: only
/// `@definition.*` maps to Tilth's definitions/edit_spans capabilities.
fn capture_fixture(lang: &str, content: &str) -> Capture {
    let language = match get_language(lang) {
        Ok(l) => l,
        Err(e) => return Capture::Error(format!("get_language({lang}): {e}")),
    };
    let Some(tags_scm) = tree_sitter_language_pack::get_tags_query(lang) else {
        return Capture::Error(format!("no bundled tags.scm for {lang}"));
    };
    let query = match Query::new(&language, tags_scm) {
        Ok(q) => q,
        Err(e) => return Capture::Error(format!("compile tags.scm for {lang}: {e}")),
    };
    let mut parser = TsParser::new();
    if let Err(e) = parser.set_language(&language) {
        return Capture::Error(format!("set_language({lang}): {e}"));
    }
    let Some(tree) = parser.parse(content, None) else {
        return Capture::Error(format!("parse failed for {lang}"));
    };
    let source_bytes = content.as_bytes();
    let capture_names = query.capture_names();
    let mut definitions = Vec::new();
    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(&query, tree.root_node(), source_bytes);
    while let Some(m) = matches.next() {
        let mut def_node = None;
        let mut def_kind = None;
        let mut name_text = None;
        for cap in m.captures {
            let cap_name = capture_names[cap.index as usize];
            if let Some(kind) = cap_name.strip_prefix("definition.") {
                def_node = Some(cap.node);
                def_kind = Some(kind.to_string());
            } else if cap_name == "name" {
                name_text = cap.node.utf8_text(source_bytes).ok().map(str::to_string);
            }
        }
        if let (Some(node), Some(kind)) = (def_node, def_kind) {
            let name = name_text.unwrap_or_default();
            let text = node.utf8_text(source_bytes).unwrap_or_default().to_string();
            definitions.push(TagDefinition {
                kind,
                name,
                start_line: node.start_position().row as u32 + 1,
                end_line: node.end_position().row as u32 + 1,
                text,
            });
        }
    }
    Capture::Ok { definitions }
}

fn normalize(fixture_id: &str, capture: &Capture) -> NormalizedRecord {
    match capture {
        Capture::Error(msg) => NormalizedRecord {
            version: RECORD_VERSION,
            fixture_id: fixture_id.to_string(),
            definitions: CapabilityResult::Error(msg.clone()),
            nesting: CapabilityResult::Error(msg.clone()),
            signatures: CapabilityResult::Error(msg.clone()),
            imports: CapabilityResult::Error(msg.clone()),
            edit_spans: CapabilityResult::Error(msg.clone()),
        },
        Capture::Ok { definitions } => {
            let defs_value: Vec<[String; 2]> = definitions
                .iter()
                .map(|d| [d.kind.clone(), d.name.clone()])
                .collect();
            let spans_value: Vec<(String, String)> = definitions
                .iter()
                .map(|d| (d.name.clone(), d.text.clone()))
                .collect();
            NormalizedRecord {
                version: RECORD_VERSION,
                fixture_id: fixture_id.to_string(),
                definitions: CapabilityResult::Supported(
                    serde_json::to_value(defs_value).expect("serialize definitions"),
                ),
                // The bundled tags.scm emits only flat @definition.*/@reference.*
                // captures with no parent-child structure: nesting data does not
                // exist to normalize, so this is unsupported, not empty-success.
                nesting: CapabilityResult::Unsupported,
                // tags.scm captures a definition's name node, never a parameter
                // list or return type: no signature data exists to normalize.
                signatures: CapabilityResult::Unsupported,
                // None of rust/typescript/python's bundled tags.scm declare an
                // @definition.import or import-reference capture.
                imports: CapabilityResult::Unsupported,
                edit_spans: CapabilityResult::Supported(
                    serde_json::to_value(spans_value).expect("serialize edit spans"),
                ),
            }
        }
    }
}

fn raw_record(fixture_id: &str, language: &str, capture: &Capture) -> RawRecord {
    let entries = match capture {
        Capture::Ok { definitions } => definitions
            .iter()
            .map(|d| RawEntry {
                kind: d.kind.clone(),
                name: d.name.clone(),
                start_line: d.start_line,
                span_start_line: d.start_line,
                end_line: d.end_line,
                signature: None,
                doc: None,
                children: Vec::new(),
            })
            .collect(),
        Capture::Error(_) => Vec::new(),
    };
    RawRecord {
        version: RECORD_VERSION,
        fixture_id: fixture_id.to_string(),
        language: language.to_string(),
        entries,
    }
}

fn write_json(path: &Path, value: &serde_json::Value) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).expect("create output dir");
    }
    let pretty = serde_json::to_string_pretty(value).expect("serialize record");
    fs::write(path, pretty).expect("write record");
}

fn default_manifest_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/manifest.json")
}

fn default_out_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../.generated/candidates/tree-sitter-language-pack")
}

fn capture(manifest_path: Option<PathBuf>, out_dir: Option<PathBuf>) {
    let manifest_path = manifest_path.unwrap_or_else(default_manifest_path);
    let out_dir = out_dir.unwrap_or_else(default_out_dir);

    let raw_manifest = fs::read_to_string(&manifest_path)
        .unwrap_or_else(|e| panic!("read manifest {}: {e}", manifest_path.display()));
    let manifest =
        parse_manifest(&raw_manifest).expect("manifest must be a valid version-1 record");
    assert!(
        !manifest.fixtures.is_empty(),
        "manifest must declare at least one fixture"
    );

    let fixtures_root = manifest_path
        .parent()
        .expect("manifest path must have a parent directory")
        .to_path_buf();

    let mut blocked: BTreeMap<String, String> = BTreeMap::new();
    let mut extraction_seconds = 0.0f64;

    for entry in &manifest.fixtures {
        let source_path = fixtures_root.join(&entry.source);
        let bytes = match fs::read(&source_path) {
            Ok(b) => b,
            Err(e) => {
                let msg = format!("read {}: {e}", source_path.display());
                blocked.insert(entry.id.clone(), msg.clone());
                let capture = Capture::Error(msg);
                emit(&out_dir, entry, &capture);
                continue;
            }
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
            Err(e) => {
                let msg = format!("non-utf8 fixture {}: {e}", source_path.display());
                blocked.insert(entry.id.clone(), msg.clone());
                let capture = Capture::Error(msg);
                emit(&out_dir, entry, &capture);
                continue;
            }
        };
        // Time only the parse+extract call: file IO, hashing, and record
        // serialization/write are excluded from the AC-4 in-process figure.
        let extraction_start = Instant::now();
        let capture = capture_fixture(&entry.language, &content);
        extraction_seconds += extraction_start.elapsed().as_secs_f64();
        if let Capture::Error(msg) = &capture {
            blocked.insert(entry.id.clone(), msg.clone());
        }
        emit(&out_dir, entry, &capture);
    }

    // AC-4: report in-process extraction time separately from process
    // startup and IO. Printed before exit so a blocked run still reports it.
    println!("extraction_seconds={extraction_seconds}");

    if blocked.is_empty() {
        println!("captured {} fixtures", manifest.fixtures.len());
    } else {
        for (id, msg) in &blocked {
            println!("blocked {id}: {msg}");
        }
        std::process::exit(1);
    }
}

fn emit(out_dir: &Path, entry: &FixtureEntry, capture: &Capture) {
    let raw = raw_record(&entry.id, &entry.language, capture);
    let raw_value = serde_json::to_value(&raw).expect("serialize raw record");
    write_json(&out_dir.join(format!("raw/{}.json", entry.id)), &raw_value);

    let normalized = normalize(&entry.id, capture);
    let normalized_value = serde_json::to_value(&normalized).expect("serialize normalized record");
    write_json(
        &out_dir.join(format!("normalized/{}.json", entry.id)),
        &normalized_value,
    );
}
