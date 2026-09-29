//! Capture entrypoint for the ast-grep-outline candidate in the Tilth
//! extraction reuse comparison experiment. See
//! `benchmark/extraction/README.md` for setup and usage.
//!
//! All tree-sitter grammars ast-grep-outline needs (rust/typescript/python)
//! are statically compiled into the crate: there is no network provisioning
//! step. `capture` (offline) reads the frozen fixture manifest, runs the
//! bundled `DEFAULT_OUTLINE_RULES` for each fixture's language through
//! `ast-grep-outline`'s combined extractor, and emits versioned
//! raw/normalized records.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

use ast_grep_core::tree_sitter::LanguageExt;
use ast_grep_language::SupportLang;
use ast_grep_outline::combined_extractor::CombinedExtractors;
use ast_grep_outline::extractor::parse_outline_rules;
use ast_grep_outline::model::{OutlineEntry, SymbolType};
use ast_grep_outline::DEFAULT_OUTLINE_RULES;
use clap::{Parser as ClapArgs, Subcommand};
use serde::{Deserialize, Serialize};
use twox_hash::XxHash32;

const RECORD_VERSION: u32 = 1;
const SUPPORTED_MANIFEST_VERSION: u64 = 1;

#[derive(ClapArgs)]
#[command(name = "ast-grep-outline-candidate")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
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
        Command::Capture { manifest, out } => capture(manifest, out),
    }
}

// ---- manifest (mirrors the tree-sitter-language-pack candidate's contract) ----

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

fn support_lang(name: &str) -> Result<SupportLang, String> {
    match name {
        "rust" => Ok(SupportLang::Rust),
        "typescript" => Ok(SupportLang::TypeScript),
        "python" => Ok(SupportLang::Python),
        other => Err(format!("unsupported language {other}")),
    }
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
    #[allow(dead_code)]
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

// ---- captured outline: owned copies of the borrowed OutlineItem/OutlineMember data ----

struct CapturedEntry {
    symbol_type: String,
    name: String,
    start_line: u32,
    end_line: u32,
    signature: String,
    /// Exact owned source text for `entry.range.byte_offset`.
    text: String,
}

struct CapturedMember {
    entry: CapturedEntry,
}

struct CapturedItem {
    entry: CapturedEntry,
    is_import: bool,
    /// Direct children under this item. ast-grep-outline's model stops at this
    /// item/member boundary: members never carry further nested members, so
    /// this is a real one-level nesting limitation, not a capture gap.
    members: Vec<CapturedMember>,
}

enum Capture {
    Ok(Vec<CapturedItem>),
    Error(String),
}

fn symbol_type_str(symbol_type: SymbolType) -> String {
    serde_json::to_value(symbol_type)
        .ok()
        .and_then(|v| v.as_str().map(str::to_string))
        .unwrap_or_default()
}

fn to_captured_entry(entry: &OutlineEntry, content: &str) -> CapturedEntry {
    let text = content
        .get(entry.range.byte_offset.clone())
        .unwrap_or_default()
        .to_string();
    CapturedEntry {
        symbol_type: symbol_type_str(entry.symbol_type),
        name: entry.name.to_string(),
        start_line: entry.range.start.line as u32 + 1,
        end_line: entry.range.end.line as u32 + 1,
        signature: entry.signature.to_string(),
        text,
    }
}

/// Filters the bundled `DEFAULT_OUTLINE_RULES` to `lang` and compiles them
/// into a combined extractor. `lang` must already have a static grammar
/// built in (rust/typescript/python here) — this never downloads anything.
fn build_extractors(lang: SupportLang) -> Result<CombinedExtractors<SupportLang>, String> {
    let rules = parse_outline_rules::<SupportLang>(DEFAULT_OUTLINE_RULES)
        .map_err(|e| format!("parse bundled outline rules: {e}"))?
        .into_iter()
        .filter(|rule| rule.common().language == lang)
        .collect::<Vec<_>>();
    CombinedExtractors::try_from(rules, &Default::default())
        .map_err(|e| format!("compile outline rules: {e}"))
}

fn capture_fixture(
    lang: SupportLang,
    content: &str,
    extractors: &CombinedExtractors<SupportLang>,
) -> Capture {
    let ast_grep_root = lang.ast_grep(content);
    let root = ast_grep_root.root();
    let items = extractors
        .extract(root)
        .map(|item| CapturedItem {
            entry: to_captured_entry(&item.entry, content),
            is_import: item.is_import,
            members: item
                .members
                .iter()
                .map(|member| CapturedMember {
                    entry: to_captured_entry(&member.entry, content),
                })
                .collect(),
        })
        .collect();
    Capture::Ok(items)
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
        Capture::Ok(items) => {
            let mut definitions: Vec<[String; 2]> = Vec::new();
            let mut nesting: Vec<[String; 2]> = Vec::new();
            let mut signatures: Vec<[String; 2]> = Vec::new();
            let mut imports: Vec<[String; 2]> = Vec::new();
            let mut edit_spans: Vec<[String; 2]> = Vec::new();

            for item in items {
                definitions.push([item.entry.symbol_type.clone(), item.entry.name.clone()]);
                signatures.push([item.entry.name.clone(), item.entry.signature.clone()]);
                edit_spans.push([item.entry.name.clone(), item.entry.text.clone()]);
                if item.is_import {
                    imports.push([item.entry.name.clone(), item.entry.signature.clone()]);
                }
                for member in &item.members {
                    definitions.push([member.entry.symbol_type.clone(), member.entry.name.clone()]);
                    signatures.push([member.entry.name.clone(), member.entry.signature.clone()]);
                    edit_spans.push([member.entry.name.clone(), member.entry.text.clone()]);
                    nesting.push([item.entry.name.clone(), member.entry.name.clone()]);
                }
            }

            NormalizedRecord {
                version: RECORD_VERSION,
                fixture_id: fixture_id.to_string(),
                definitions: CapabilityResult::Supported(
                    serde_json::to_value(definitions).expect("serialize definitions"),
                ),
                // Real one-level limit: item->member is the only nesting depth
                // the model exposes (see CapturedItem::members doc comment).
                nesting: CapabilityResult::Supported(
                    serde_json::to_value(nesting).expect("serialize nesting"),
                ),
                signatures: CapabilityResult::Supported(
                    serde_json::to_value(signatures).expect("serialize signatures"),
                ),
                imports: CapabilityResult::Supported(
                    serde_json::to_value(imports).expect("serialize imports"),
                ),
                edit_spans: CapabilityResult::Supported(
                    serde_json::to_value(edit_spans).expect("serialize edit spans"),
                ),
            }
        }
    }
}

fn raw_entry(entry: &CapturedEntry, children: Vec<RawEntry>) -> RawEntry {
    RawEntry {
        kind: entry.symbol_type.clone(),
        name: entry.name.clone(),
        start_line: entry.start_line,
        span_start_line: entry.start_line,
        end_line: entry.end_line,
        signature: if entry.signature.is_empty() {
            None
        } else {
            Some(entry.signature.clone())
        },
        doc: None,
        children,
    }
}

fn raw_record(fixture_id: &str, language: &str, capture: &Capture) -> RawRecord {
    let entries = match capture {
        Capture::Ok(items) => items
            .iter()
            .map(|item| {
                let children = item
                    .members
                    .iter()
                    .map(|member| raw_entry(&member.entry, Vec::new()))
                    .collect();
                raw_entry(&item.entry, children)
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
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.generated/candidates/ast-grep-outline")
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

    let mut extractors: BTreeMap<String, Result<CombinedExtractors<SupportLang>, String>> =
        BTreeMap::new();
    for name in ["rust", "typescript", "python"] {
        let built = support_lang(name).and_then(build_extractors);
        extractors.insert(name.to_string(), built);
    }

    let mut blocked: BTreeMap<String, String> = BTreeMap::new();
    let mut extraction_seconds = 0.0f64;

    for entry in &manifest.fixtures {
        let source_path = fixtures_root.join(&entry.source);
        let bytes = match fs::read(&source_path) {
            Ok(b) => b,
            Err(e) => {
                let msg = format!("read {}: {e}", source_path.display());
                blocked.insert(entry.id.clone(), msg.clone());
                emit(&out_dir, entry, &Capture::Error(msg));
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
                emit(&out_dir, entry, &Capture::Error(msg));
                continue;
            }
        };

        // Time only the parse+extract call: file IO, hashing, and record
        // serialization/write are excluded from the AC-4 in-process figure.
        let extraction_start = Instant::now();
        let capture = match extractors.get(&entry.language) {
            Some(Ok(combined)) => {
                let lang = support_lang(&entry.language).expect("language already validated");
                capture_fixture(lang, &content, combined)
            }
            Some(Err(msg)) => Capture::Error(msg.clone()),
            None => Capture::Error(format!("unsupported language {}", entry.language)),
        };
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
