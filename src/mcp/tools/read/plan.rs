//! Request planning for `tilth_read`. Before any file is rendered, sections of
//! one file that overlap or sit close together merge into one section, and a
//! line range that ends inside a construct grows to the construct's end when
//! the response stays within budget. Otherwise the range stops before that
//! construct and carries an exact continuation.

use std::cell::OnceCell;
use std::path::{Path, PathBuf};

use crate::cache::OutlineCache;
use crate::mcp::path_suffix::PathSuffix;
use crate::types::{OutlineEntry, OutlineKind};

/// Sections of one file merge when at most this many lines lie between them.
/// A section costs about 25 tokens of framing (stats line, `[path#TAG]`,
/// blank lines), which is about three source lines. Larger gaps in the
/// gepa-v3 batches were deliberate picks, such as single lines 10 or 20 apart
/// in one test file, where a merge would double the output.
pub(super) const COALESCE_GAP: u32 = 3;

/// Bytes the stats line, the `[path#TAG]` line, and the blank lines of one
/// section can use.
const SECTION_FRAME_BYTES: usize = 160;

/// One request of a batch, already resolved by the caller.
pub(super) struct Request {
    /// The request as the caller wrote it.
    pub raw: String,
    /// The path part of `raw`, used to spell a rewritten request.
    pub shown: String,
    pub path: PathBuf,
    pub suffix: PathSuffix,
}

/// A planned request and the continuation its section ends with, if any.
#[derive(Debug, PartialEq, Eq)]
pub(super) struct Planned {
    pub request: String,
    pub next: Option<Continuation>,
}

/// The lines a section leaves out of an unfinished construct.
#[derive(Debug, PartialEq, Eq)]
pub(super) struct Continuation {
    /// The exact `path#start-end` request that shows the lines.
    pub request: String,
    /// `true` when the section stops before the construct begins.
    pub stopped_before: bool,
}

impl Continuation {
    /// The line a section ends with to name the continuation.
    pub(super) fn note(&self) -> String {
        let request = serde_json::Value::String(self.request.clone());
        let why = if self.stopped_before {
            "stopped before a construct that does not fit this response."
        } else {
            "the construct continues past this section and does not fit this response."
        };
        format!("... {why} Next: tilth_read(paths: [{request}])")
    }
}

/// A run of lines that one planned section shows.
struct Cluster {
    /// Index of the first request the cluster serves.
    lead: usize,
    members: Vec<usize>,
    start: u32,
    end: u32,
    /// The end came from an explicit `#n-m` range, so it may cut a construct.
    open_end: bool,
    next: Option<Continuation>,
}

/// The text of one file, loaded once per plan. The outline parses only when a
/// range ends before the last line, since only then can it cut a construct.
struct FileView {
    path: PathBuf,
    content: String,
    /// `sizes[n]` is the JSON-escaped size of numbered lines `1..=n`.
    sizes: Vec<usize>,
    entries: OnceCell<Vec<OutlineEntry>>,
}

impl FileView {
    fn load(path: &Path) -> Option<Self> {
        let content = std::fs::read_to_string(path).ok()?;
        let mut sizes = vec![0];
        for (index, line) in content.lines().enumerate() {
            let numbered = format!("{}:{line}\n", index + 1);
            sizes.push(sizes[sizes.len() - 1] + super::escaped_len(&numbered));
        }
        Some(Self {
            path: path.to_path_buf(),
            content,
            sizes,
            entries: OnceCell::new(),
        })
    }

    fn total(&self) -> u32 {
        u32::try_from(self.sizes.len() - 1).unwrap_or(u32::MAX)
    }

    /// Escaped bytes of numbered lines `start..=end`.
    fn size(&self, start: u32, end: u32) -> usize {
        let end = (end as usize).min(self.sizes.len() - 1);
        let start = (start as usize).clamp(1, end.max(1));
        self.sizes[end] - self.sizes[start - 1]
    }

    /// The construct a section ending at `line` cuts, if any.
    fn cut_at(&self, line: u32, cache: &OutlineCache) -> Option<(u32, u32)> {
        if line >= self.total() {
            return None;
        }
        let entries = self
            .entries
            .get_or_init(|| outline(&self.path, &self.content, cache));
        cut_construct(entries, line)
    }
}

/// Deep outline of `content`, or nothing for a file without a grammar.
pub(super) fn outline(path: &Path, content: &str, cache: &OutlineCache) -> Vec<OutlineEntry> {
    let crate::types::FileType::Code(lang) = crate::lang::detect_file_type(path) else {
        return Vec::new();
    };
    match cache.parse_source(path, content) {
        Some(parsed) => {
            crate::lang::outline::deep_outline_tree_from_tree(content, lang, parsed.tree())
        }
        None => crate::lang::outline::get_deep_outline_tree(content, lang),
    }
}

/// Kinds whose body holds other definitions, so a line between two members
/// is a clean place to stop.
fn is_container(kind: OutlineKind) -> bool {
    matches!(
        kind,
        OutlineKind::Class
            | OutlineKind::Struct
            | OutlineKind::Interface
            | OutlineKind::Enum
            | OutlineKind::Module
            | OutlineKind::TestSuite
    )
}

/// The span of the construct that a stop after `line` cuts in two, or `None`
/// when `line` and `line + 1` do not lie inside one construct. A stop between
/// two members of a class or impl does not cut it. A module never counts:
/// Python's whole-file root parses as one, and a module body is a member list.
pub(super) fn cut_construct(entries: &[OutlineEntry], line: u32) -> Option<(u32, u32)> {
    let next = line.checked_add(1)?;
    let mut level = entries;
    let mut found: Option<&OutlineEntry> = None;
    while let Some(entry) = level.iter().find(|entry| {
        crate::lang::outline::is_path_line_entry_kind(entry.kind)
            && entry.span_start_line <= line
            && next <= entry.end_line
    }) {
        found = Some(entry);
        level = &entry.children;
    }
    let entry = found?;
    if entry.kind == OutlineKind::Module || (is_container(entry.kind) && !entry.children.is_empty())
    {
        return None;
    }
    Some((entry.span_start_line, entry.end_line))
}

/// The line span a request shows when the file reads cleanly.
fn request_span(request: &Request, total: u32, cache: &OutlineCache) -> Option<(u32, u32, bool)> {
    let cast = |n: usize| u32::try_from(n).unwrap_or(u32::MAX);
    let (start, end, open_end) = match &request.suffix {
        PathSuffix::LineRange(start, end) => (cast(*start), cast(*end).min(total), true),
        PathSuffix::FromLine(start) => (cast(*start), total, false),
        PathSuffix::Symbol(name) => match super::resolve_symbol(&request.path, name, cache) {
            super::SymbolLookup::Found(start, end) => (cast(start), cast(end), false),
            _ => return None,
        },
        PathSuffix::Heading(heading) => {
            let (start, end) = crate::read::resolve_heading_span(&request.path, heading)?;
            (start, end, false)
        }
        PathSuffix::None => return None,
    };
    (start >= 1 && start <= end).then_some((start, end, open_end))
}

/// Plan `requests` so the response fits `limit` escaped bytes. Requests that
/// show no line span (whole files, errors, ignored paths) pass through.
pub(super) fn plan(requests: &[Request], cache: &OutlineCache, limit: usize) -> Vec<Planned> {
    let frame = SECTION_FRAME_BYTES * requests.len();
    let limit = limit.saturating_sub(frame);
    let mut files: Vec<(PathBuf, Option<FileView>)> = Vec::new();
    let mut spans: Vec<Option<(u32, u32, bool)>> = Vec::with_capacity(requests.len());
    let mut used = 0usize;
    for request in requests {
        let mut span = None;
        if !matches!(request.suffix, PathSuffix::None)
            && request.path.is_file()
            && !crate::read::tilthignore_denies(&request.path)
        {
            if !files.iter().any(|(path, _)| *path == request.path) {
                files.push((request.path.clone(), FileView::load(&request.path)));
            }
            let view = files.iter().find(|(path, _)| *path == request.path);
            if let Some((_, Some(view))) = view {
                span = request_span(request, view.total(), cache);
            }
        } else if matches!(request.suffix, PathSuffix::None)
            && !crate::read::would_outline(&request.path)
        {
            used += std::fs::metadata(&request.path).map_or(0, |m| m.len() as usize);
        }
        spans.push(span);
    }

    let mut clusters: Vec<(usize, Cluster)> = Vec::new();
    for (file, (path, view)) in files.iter().enumerate() {
        if view.is_none() {
            continue;
        }
        let mut members: Vec<(u32, u32, bool, usize)> = requests
            .iter()
            .zip(&spans)
            .enumerate()
            .filter_map(|(index, (request, span))| {
                let (start, end, open) = (*span)?;
                (request.path == *path).then_some((start, end, open, index))
            })
            .collect();
        members.sort_unstable_by_key(|&(start, end, _, index)| (start, end, index));
        for (start, end, open, index) in members {
            match clusters.last_mut() {
                Some((owner, cluster))
                    if *owner == file && start <= cluster.end.saturating_add(COALESCE_GAP + 1) =>
                {
                    cluster.lead = cluster.lead.min(index);
                    cluster.members.push(index);
                    if end > cluster.end {
                        cluster.end = end;
                        cluster.open_end = open;
                    } else if end == cluster.end {
                        cluster.open_end |= open;
                    }
                }
                _ => clusters.push((
                    file,
                    Cluster {
                        lead: index,
                        members: vec![index],
                        start,
                        end,
                        open_end: open,
                        next: None,
                    },
                )),
            }
        }
    }
    for (file, cluster) in &clusters {
        if let (_, Some(view)) = &files[*file] {
            used += view.size(cluster.start, cluster.end);
        }
    }

    // Grow or stop open ends in request order, so earlier requests win room.
    let mut order: Vec<usize> = (0..clusters.len()).collect();
    order.sort_unstable_by_key(|&index| clusters[index].1.lead);
    for index in order {
        let (file, cluster) = &mut clusters[index];
        let (_, Some(view)) = &files[*file] else {
            continue;
        };
        if !cluster.open_end {
            continue;
        }
        let Some((start, end)) = view.cut_at(cluster.end, cache) else {
            continue;
        };
        let grow = view.size(cluster.end + 1, end);
        if used + grow <= limit {
            used += grow;
            cluster.end = end;
            continue;
        }
        let shown = &requests[cluster.lead].shown;
        if start > cluster.start && view.size(start, end) <= limit {
            used -= view.size(start, cluster.end);
            cluster.end = start - 1;
            cluster.next = Some(Continuation {
                request: format!("{shown}#{start}-{end}"),
                stopped_before: true,
            });
        } else {
            cluster.next = Some(Continuation {
                request: format!("{shown}#{}-{end}", cluster.end + 1),
                stopped_before: false,
            });
        }
    }

    // A grown end can now reach the next section of the same file.
    let mut merged: Vec<(usize, Cluster)> = Vec::with_capacity(clusters.len());
    for (file, cluster) in clusters {
        match merged.last_mut() {
            Some((owner, last))
                if *owner == file
                    && last.next.is_none()
                    && cluster.start <= last.end.saturating_add(COALESCE_GAP + 1) =>
            {
                last.lead = last.lead.min(cluster.lead);
                last.members.extend(cluster.members);
                if cluster.end >= last.end {
                    last.end = cluster.end;
                    last.next = cluster.next;
                }
            }
            _ => merged.push((file, cluster)),
        }
    }

    let mut planned: Vec<Option<Planned>> = requests
        .iter()
        .map(|request| {
            Some(Planned {
                request: request.raw.clone(),
                next: None,
            })
        })
        .collect();
    for (_, cluster) in merged {
        for &member in &cluster.members {
            planned[member] = None;
        }
        let lead = &requests[cluster.lead];
        let unchanged = cluster.members.len() == 1
            && cluster.next.is_none()
            && spans[cluster.lead]
                .is_some_and(|(start, end, _)| (start, end) == (cluster.start, cluster.end));
        let request = if unchanged {
            lead.raw.clone()
        } else {
            format!("{}#{}-{}", lead.shown, cluster.start, cluster.end)
        };
        planned[cluster.lead] = Some(Planned {
            request,
            next: cluster.next,
        });
    }
    planned.into_iter().flatten().collect()
}

#[cfg(test)]
mod tests {
    use std::fmt::Write as _;

    use super::*;

    const RUST: &str = "\
struct Point {
    x: i32,
    y: i32,
}

impl Point {
    fn a(&self) -> i32 {
        self.x
    }

    fn b(&self) -> i32 {
        self.y
    }
}

fn tail() {}
";

    fn requests(dir: &Path, raws: &[&str]) -> Vec<Request> {
        raws.iter()
            .map(|raw| {
                let (shown, suffix) = crate::mcp::path_suffix::parse_path_with_suffix(raw);
                Request {
                    raw: (*raw).to_string(),
                    shown: shown.to_string_lossy().into_owned(),
                    path: dir.join(&shown),
                    suffix,
                }
            })
            .collect()
    }

    fn planned(dir: &Path, raws: &[&str], limit: usize) -> Vec<String> {
        plan(&requests(dir, raws), &OutlineCache::new(), limit)
            .into_iter()
            .map(|item| item.request)
            .collect()
    }

    fn fixture(text: &str, name: &str) -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join(name), text).unwrap();
        dir
    }

    #[test]
    fn cut_construct_names_the_innermost_cut_definition() {
        let path = Path::new("p.rs");
        let entries = outline(path, RUST, &OutlineCache::new());
        assert_eq!(cut_construct(&entries, 2), Some((1, 4)), "struct body");
        assert_eq!(cut_construct(&entries, 7), Some((7, 9)), "method body");
        assert_eq!(cut_construct(&entries, 10), None, "between methods");
        assert_eq!(cut_construct(&entries, 4), None, "after the struct");
        assert_eq!(cut_construct(&entries, 15), None, "last line");
    }

    #[test]
    fn python_root_module_is_not_a_construct() {
        let source = "a = 1\nb = 2\nc = 3\n";
        let entries = outline(Path::new("m.py"), source, &OutlineCache::new());
        assert_eq!(cut_construct(&entries, 1), None);
    }

    /// Sixty plain assignments, a file with no construct to cut.
    fn assignments() -> String {
        let mut text = String::new();
        for i in 1..=60 {
            let _ = writeln!(text, "v{i} = {i}");
        }
        text
    }

    #[test]
    fn sections_merge_up_to_the_gap_and_no_further() {
        let dir = fixture(&assignments(), "f.py");
        let gap = COALESCE_GAP;
        let close = format!("f.py#{}-{}", 10 + gap + 1, 30);
        let far = format!("f.py#{}-{}", 10 + gap + 2, 30);
        assert_eq!(
            planned(dir.path(), &["f.py#5-10", &close], 40_000),
            ["f.py#5-30"]
        );
        assert_eq!(
            planned(dir.path(), &["f.py#5-10", &far], 40_000),
            ["f.py#5-10".to_string(), far]
        );
    }

    #[test]
    fn merge_is_order_independent_and_keeps_the_first_slot() {
        let dir = fixture(&assignments(), "f.py");
        std::fs::write(dir.path().join("g.py"), "x = 1\n").unwrap();
        assert_eq!(
            planned(dir.path(), &["f.py#35-50", "g.py", "f.py#10-40"], 40_000),
            ["f.py#10-50", "g.py"]
        );
        assert_eq!(
            planned(dir.path(), &["g.py", "f.py#10-40", "f.py#35-50"], 40_000),
            ["g.py", "f.py#10-50"]
        );
    }

    #[test]
    fn adjacent_symbols_merge_into_one_range() {
        let dir = fixture(RUST, "p.rs");
        assert_eq!(
            planned(dir.path(), &["p.rs#a", "p.rs#b"], 40_000),
            ["p.rs#7-13"]
        );
        assert_eq!(planned(dir.path(), &["p.rs#a"], 40_000), ["p.rs#a"]);
    }

    #[test]
    fn open_range_end_grows_to_the_construct_end() {
        let dir = fixture(RUST, "p.rs");
        assert_eq!(planned(dir.path(), &["p.rs#1-8"], 40_000), ["p.rs#1-9"]);
        assert_eq!(planned(dir.path(), &["p.rs#1-10"], 40_000), ["p.rs#1-10"]);
    }

    #[test]
    fn over_limit_growth_stops_before_the_construct() {
        let dir = fixture(RUST, "p.rs");
        let requests = requests(dir.path(), &["p.rs#1-12"]);
        let frame = SECTION_FRAME_BYTES;
        let view = FileView::load(&dir.path().join("p.rs")).unwrap();
        let limit = frame + view.size(1, 12);
        let plan = plan(&requests, &OutlineCache::new(), limit);
        assert_eq!(
            plan,
            [Planned {
                request: "p.rs#1-10".into(),
                next: Some(Continuation {
                    request: "p.rs#11-13".into(),
                    stopped_before: true,
                }),
            }]
        );
    }
}
