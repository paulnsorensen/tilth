//! Request planning for `tilth_read`. The planner runs before any file
//! renders. It merges sections of one file that overlap or sit close together.
//! It grows a line range that ends inside a construct to the construct's end
//! when the response stays within budget. Otherwise the range stops before
//! that construct and carries an exact continuation.

use std::cell::OnceCell;
use std::path::{Path, PathBuf};

use super::Request;
use crate::cache::OutlineCache;
use crate::lang::outline;
use crate::mcp::path_suffix::PathSuffix;
use crate::types::OutlineEntry;

/// Sections of one file merge when at most this many lines lie between them.
/// A section costs about 25 tokens of framing (stats line, `[path#TAG]`,
/// blank lines), which is about three source lines. Larger gaps in the
/// gepa-v3 batches were deliberate picks, such as single lines 10 or 20 apart
/// in one test file, where a merge would double the output.
pub(super) const COALESCE_GAP: u32 = 3;

/// Bytes the stats line, the `[path#TAG]` line, and the blank lines of one
/// section can use.
const SECTION_FRAME_BYTES: usize = 160;

/// A planned request and the continuation its section ends with, if any.
#[derive(Debug)]
pub(super) struct Planned {
    pub request: Request,
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
        let paths = serde_json::json!([self.request]).to_string();
        let why = if self.stopped_before {
            "stopped before a construct that does not fit this response."
        } else {
            "the construct continues past this section and does not fit this response."
        };
        format!("... {why} {}", super::next_call(&paths, ""))
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
pub(super) struct FileView {
    path: PathBuf,
    content: String,
    /// `sizes[n]` is the JSON-escaped size of numbered lines `1..=n`.
    sizes: Vec<usize>,
    entries: OnceCell<Vec<OutlineEntry>>,
}

impl FileView {
    pub(super) fn load(path: &Path) -> Option<Self> {
        let content = std::fs::read_to_string(path).ok()?;
        let mut sizes = vec![0];
        for (index, line) in content.lines().enumerate() {
            let digits = (index + 1).ilog10() as usize + 1;
            // `N:`, the line, and the escaped `\n` that ends it.
            let numbered = digits + 1 + super::escaped_len(line) + 2;
            sizes.push(sizes[sizes.len() - 1] + numbered);
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
    pub(super) fn size(&self, start: u32, end: u32) -> usize {
        let end = (end as usize).min(self.sizes.len() - 1);
        let start = (start as usize).clamp(1, end.max(1));
        self.sizes[end] - self.sizes[start - 1]
    }

    /// The last line of `start..=end` that keeps the lines from `start` within
    /// `limit` bytes. The result is never less than `start`.
    fn fit_end(&self, start: u32, end: u32, limit: usize) -> u32 {
        let mut last = start;
        while last < end && self.size(start, last + 1) <= limit {
            last += 1;
        }
        last
    }

    /// The construct a section ending at `line` cuts, if any. A file larger
    /// than the parsed-file cache keeps would parse again on every call, so it
    /// gets no construct lookup.
    pub(super) fn cut_at(&self, line: u32, cache: &OutlineCache) -> Option<(u32, u32)> {
        let too_large = self.content.len() as u64 > crate::cache::MAX_PARSED_FILE_BYTES;
        if line >= self.total() || too_large {
            return None;
        }
        let entries = self.entries.get_or_init(|| {
            let parsed = cache.parse_source(&self.path, &self.content);
            let tree = parsed.as_ref().map(|parsed| parsed.tree());
            outline::deep_outline_for_path(&self.path, &self.content, tree).unwrap_or_default()
        });
        outline::cut_construct(entries, line)
    }
}

/// The line span a request shows when the file reads cleanly. A line range
/// stops at the last line, and only its end can cut a construct.
fn request_span(request: &Request, total: u32, cache: &OutlineCache) -> Option<(u32, u32, bool)> {
    let (start, end) = super::suffix_span(&request.path, &request.suffix, || Some(total), cache)?;
    let (end, open_end) = match request.suffix {
        PathSuffix::LineRange(..) => (end.min(total), true),
        PathSuffix::FromLine(_) => (end.min(total), false),
        PathSuffix::Symbol(_) | PathSuffix::Heading(_) | PathSuffix::None => (end, false),
    };
    (start >= 1 && start <= end).then_some((start, end, open_end))
}
/// Whether a section that starts at `start` lies within [`COALESCE_GAP`]
/// lines after a section that ends at `end`.
fn within_gap(end: u32, start: u32) -> bool {
    start <= end.saturating_add(COALESCE_GAP + 1)
}

/// Plan `requests` so the response fits `limit` escaped bytes. Requests that
/// show no line span (whole files, errors, ignored paths) pass through.
pub(super) fn plan(requests: &[Request], cache: &OutlineCache, limit: usize) -> Vec<Planned> {
    let frame = SECTION_FRAME_BYTES * requests.len();
    let limit = limit.saturating_sub(frame);
    // Files are keyed by their normalized path, so `a/../f.py` and `f.py`
    // are one file.
    let mut files: Vec<(String, Option<FileView>)> = Vec::new();
    let mut spans: Vec<Option<(u32, u32, bool, usize)>> = Vec::with_capacity(requests.len());
    let mut used = 0usize;
    for request in requests {
        let mut span = None;
        if !matches!(request.suffix, PathSuffix::None)
            && request.path.is_file()
            && !crate::read::tilthignore_denies(&request.path)
        {
            let key = crate::edit::normalize_path_key(&request.path);
            let file = files.iter().position(|(known, _)| *known == key);
            let file = file.unwrap_or_else(|| {
                files.push((key, FileView::load(&request.path)));
                files.len() - 1
            });
            if let Some(view) = &files[file].1 {
                span = request_span(request, view.total(), cache)
                    .map(|(start, end, open)| (start, end, open, file));
            }
        } else if matches!(request.suffix, PathSuffix::None)
            && !crate::read::would_outline(&request.path)
        {
            used += std::fs::metadata(&request.path).map_or(0, |m| m.len() as usize);
        }
        spans.push(span);
    }

    let mut clusters: Vec<(usize, Cluster)> = Vec::new();
    for (file, (_, view)) in files.iter().enumerate() {
        if view.is_none() {
            continue;
        }
        let mut members: Vec<(u32, u32, bool, usize)> = Vec::new();
        for (index, span) in spans.iter().enumerate() {
            if let Some((start, end, open, owner)) = *span {
                if owner == file {
                    members.push((start, end, open, index));
                }
            }
        }
        members.sort_unstable_by_key(|&(start, end, _, index)| (start, end, index));
        for (start, end, open, index) in members {
            match clusters.last_mut() {
                Some((owner, cluster)) if *owner == file && within_gap(cluster.end, start) => {
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
        // A member that starts inside the construct asked for its lines, so
        // the section does not stop before the construct.
        let asked = cluster
            .members
            .iter()
            .any(|&member| spans[member].is_some_and(|(first, ..)| first >= start));
        let stop = start > cluster.start && !asked && view.size(start, end) <= limit;
        let kept = if stop {
            used - view.size(start, cluster.end)
        } else {
            used
        };
        // A section that still overflows keeps its request. The host cut then
        // continues it up to the requested end.
        if kept > limit {
            continue;
        }
        used = kept;
        let shown = &requests[cluster.lead].shown;
        if stop {
            cluster.end = start - 1;
            cluster.next = Some(Continuation {
                request: format!("{shown}#{start}-{end}"),
                stopped_before: true,
            });
        } else {
            // Name only as many lines as one response at this limit shows.
            let first = cluster.end + 1;
            let last = view.fit_end(first, end, limit);
            cluster.next = Some(Continuation {
                request: format!("{shown}#{first}-{last}"),
                stopped_before: false,
            });
        }
    }

    // A grown end can now reach the next section of the same file.
    let mut merged: Vec<(usize, Cluster)> = Vec::with_capacity(clusters.len());
    for (file, cluster) in clusters {
        match merged.last_mut() {
            Some((owner, last))
                if *owner == file && last.next.is_none() && within_gap(last.end, cluster.start) =>
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

    let mut planned: Vec<Option<Planned>> = Vec::with_capacity(requests.len());
    for request in requests {
        planned.push(Some(Planned {
            request: request.clone(),
            next: None,
        }));
    }
    for (_, cluster) in merged {
        for &member in &cluster.members {
            planned[member] = None;
        }
        let lead = &requests[cluster.lead];
        let unchanged = cluster.members.len() == 1
            && cluster.next.is_none()
            && spans[cluster.lead]
                .is_some_and(|(start, end, ..)| (start, end) == (cluster.start, cluster.end));
        let request = if unchanged {
            lead.clone()
        } else {
            lead.with_range(cluster.start, cluster.end)
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
            .map(|raw| Request::parse(raw, dir).unwrap())
            .collect()
    }

    fn planned(dir: &Path, raws: &[&str], limit: usize) -> Vec<String> {
        sections(dir, raws, limit)
            .into_iter()
            .map(|(request, _)| request)
            .collect()
    }

    /// Each planned request as written, with its continuation.
    fn sections(dir: &Path, raws: &[&str], limit: usize) -> Vec<(String, Option<Continuation>)> {
        plan(&requests(dir, raws), &OutlineCache::new(), limit)
            .into_iter()
            .map(|item| (item.request.raw, item.next))
            .collect()
    }

    fn fixture(text: &str, name: &str) -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join(name), text).unwrap();
        dir
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
        assert_eq!(
            planned(dir.path(), &["f.py#5-10", "f.py#14-30"], 40_000),
            ["f.py#5-30"]
        );
        assert_eq!(
            planned(dir.path(), &["f.py#5-10", "f.py#15-30"], 40_000),
            ["f.py#5-10", "f.py#15-30"]
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
        let view = FileView::load(&dir.path().join("p.rs")).unwrap();
        let limit = SECTION_FRAME_BYTES + view.size(1, 12);
        assert_eq!(
            sections(dir.path(), &["p.rs#1-12"], limit),
            [(
                "p.rs#1-10".to_string(),
                Some(Continuation {
                    request: "p.rs#11-13".into(),
                    stopped_before: true,
                })
            )]
        );
    }

    #[test]
    fn a_section_that_overflows_after_the_stop_keeps_its_request() {
        let dir = fixture(RUST, "p.rs");
        let view = FileView::load(&dir.path().join("p.rs")).unwrap();
        let limit = SECTION_FRAME_BYTES + view.size(1, 10) - 1;
        assert_eq!(
            sections(dir.path(), &["p.rs#1-12"], limit),
            [("p.rs#1-12".to_string(), None)]
        );
    }

    #[test]
    fn mid_construct_range_names_the_rest_only_when_it_fits() {
        let dir = fixture(RUST, "p.rs");
        let view = FileView::load(&dir.path().join("p.rs")).unwrap();
        let fits = SECTION_FRAME_BYTES + view.size(8, 8);
        assert_eq!(
            sections(dir.path(), &["p.rs#8-8"], fits),
            [(
                "p.rs#8-8".to_string(),
                Some(Continuation {
                    request: "p.rs#9-9".into(),
                    stopped_before: false,
                })
            )]
        );
        assert_eq!(
            sections(dir.path(), &["p.rs#8-8"], fits - 1),
            [("p.rs#8-8".to_string(), None)]
        );
    }

    #[test]
    fn a_member_that_starts_inside_the_construct_is_not_dropped() {
        let dir = fixture(RUST, "p.rs");
        let view = FileView::load(&dir.path().join("p.rs")).unwrap();
        let limit = 2 * SECTION_FRAME_BYTES + view.size(1, 12);
        assert_eq!(
            sections(dir.path(), &["p.rs#1-9", "p.rs#11-12"], limit),
            [(
                "p.rs#1-12".to_string(),
                Some(Continuation {
                    request: "p.rs#13-13".into(),
                    stopped_before: false,
                })
            )]
        );
    }

    #[test]
    fn a_grown_end_merges_with_the_next_section() {
        let dir = fixture(RUST, "p.rs");
        assert_eq!(
            planned(dir.path(), &["p.rs#1-7", "p.rs#12-16"], 40_000),
            ["p.rs#1-16"]
        );
    }

    #[test]
    fn a_rewritten_request_parses_back_to_itself() {
        let dir = fixture(RUST, "p.rs");
        let plan = plan(
            &requests(dir.path(), &["p.rs:1-7", "p.rs#a"]),
            &OutlineCache::new(),
            40_000,
        );
        let [item] = plan.as_slice() else {
            panic!("the two sections merge into one");
        };
        let parsed = Request::parse(&item.request.raw, dir.path()).unwrap();
        assert_eq!(item.request.raw, "p.rs#1-9");
        assert_eq!(parsed.shown, item.request.shown);
        assert_eq!(parsed.path, item.request.path);
        assert!(matches!(item.request.suffix, PathSuffix::LineRange(1, 9)));
        assert!(matches!(parsed.suffix, PathSuffix::LineRange(1, 9)));
    }

    #[test]
    fn files_too_large_to_cache_are_not_parsed_for_constructs() {
        let mut text = String::from("def big():\n");
        while text.len() as u64 <= crate::cache::MAX_PARSED_FILE_BYTES {
            text.push_str("    value = 1\n");
        }
        text.push_str("    return value\n");
        let dir = fixture(&text, "big.py");
        assert_eq!(
            sections(dir.path(), &["big.py#1-2"], 40_000),
            [("big.py#1-2".to_string(), None)]
        );
    }

    #[test]
    fn a_continuation_names_only_lines_that_fit_the_limit() {
        let source = "fn f() {\n    let a = 1;\n    let b = 2;\n    let c = 3;\n}\n";
        let dir = fixture(source, "q.rs");
        let view = FileView::load(&dir.path().join("q.rs")).unwrap();
        let limit = SECTION_FRAME_BYTES + view.size(1, 2);
        assert!(view.size(3, 5) > view.size(1, 2), "the rest must not fit");
        assert_eq!(
            sections(dir.path(), &["q.rs#1-2"], limit),
            [(
                "q.rs#1-2".to_string(),
                Some(Continuation {
                    request: "q.rs#3-3".into(),
                    stopped_before: false,
                })
            )]
        );
    }

    #[test]
    fn spellings_of_one_file_merge() {
        let dir = fixture(&assignments(), "f.py");
        std::fs::create_dir(dir.path().join("sub")).unwrap();
        let dotted = format!("{}/sub/../f.py#12-20", dir.path().display());
        assert_eq!(
            planned(dir.path(), &["f.py#5-10", &dotted], 40_000),
            ["f.py#5-20"]
        );
    }

    #[test]
    fn file_view_sizes_match_the_escaped_numbered_lines() {
        let source = "a \"q\" \\ b\n\tc\u{1}\u{8}\u{c}\r\u{1f}\u{7f} é ☃\n\nlast";
        let dir = fixture(source, "s.txt");
        let view = FileView::load(&dir.path().join("s.txt")).unwrap();
        let mut expected = 0;
        for (index, line) in source.lines().enumerate() {
            let numbered = format!("{}:{line}\n", index + 1);
            expected += serde_json::to_string(&numbered).unwrap().len() - 2;
        }
        assert_eq!(view.size(1, 4), expected);
    }
}
