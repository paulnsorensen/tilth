//! Construct completion for line-range reads. A range whose end cuts a
//! construct grows to the construct's end when the response stays within its
//! limit. Otherwise the range stops before that construct, or keeps its end,
//! and names the lines that continue it. `tilth_read` and the CLI `--section`
//! read share this logic; each measures its numbered lines in its own format.

use std::cell::OnceCell;
use std::marker::PhantomData;
use std::path::{Path, PathBuf};

use crate::cache::OutlineCache;
use crate::lang::outline;
use crate::types::OutlineEntry;

/// The response bytes of one numbered line.
pub(crate) trait LineCost {
    /// Bytes of line `number` of a file with `lines` lines, with its number
    /// and its line end.
    fn bytes(number: usize, lines: usize, line: &str) -> usize;
}

/// The text of one file and the response size of each run of its lines. The
/// outline parses only when a range ends before the last line, since only
/// then can it cut a construct.
pub(crate) struct FileView<C> {
    path: PathBuf,
    content: String,
    /// `sizes[n]` is the response size of numbered lines `1..=n`.
    sizes: Vec<usize>,
    entries: OnceCell<Vec<OutlineEntry>>,
    cost: PhantomData<fn() -> C>,
}

/// How a section treats the construct that its end cuts.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Completion {
    /// The section grows to this line, the construct's last line.
    Grow(u32),
    /// The section stops on `end`, the line before the construct, and `next`
    /// names the whole construct.
    Stop { end: u32, next: (u32, u32) },
    /// The section keeps its end. These lines continue the construct, as
    /// many as one response at the limit shows.
    Rest(u32, u32),
}

impl<C: LineCost> FileView<C> {
    pub(crate) fn load(path: &Path) -> Option<Self> {
        let content = std::fs::read_to_string(path).ok()?;
        let lines = content.lines().count();
        let mut sizes = Vec::with_capacity(lines + 1);
        sizes.push(0);
        for (index, line) in content.lines().enumerate() {
            sizes.push(sizes[index] + C::bytes(index + 1, lines, line));
        }
        Some(Self {
            path: path.to_path_buf(),
            content,
            sizes,
            entries: OnceCell::new(),
            cost: PhantomData,
        })
    }

    pub(crate) fn total(&self) -> u32 {
        u32::try_from(self.sizes.len() - 1).unwrap_or(u32::MAX)
    }

    /// Response bytes of numbered lines `start..=end`.
    pub(crate) fn size(&self, start: u32, end: u32) -> usize {
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
    pub(crate) fn cut_at(&self, line: u32, cache: &OutlineCache) -> Option<(u32, u32)> {
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

    /// Complete a section that shows lines `start..=end`. `used` is the
    /// response size so far, these lines included. `asked(line)` tells
    /// whether a request of the section starts at or after `line`; such a
    /// request asked for the construct's lines, so the section does not stop
    /// before it. Returns the completion and the new response size, or
    /// `None` when the end cuts no construct or the section overflows
    /// `limit` even after it stops. The section then keeps its end.
    pub(crate) fn complete(
        &self,
        (start, end): (u32, u32),
        used: usize,
        limit: usize,
        asked: impl Fn(u32) -> bool,
        cache: &OutlineCache,
    ) -> Option<(Completion, usize)> {
        let (first, last) = self.cut_at(end, cache)?;
        let grow = self.size(end + 1, last);
        if used + grow <= limit {
            return Some((Completion::Grow(last), used + grow));
        }
        let stop = first > start && !asked(first) && self.size(first, last) <= limit;
        let kept = if stop {
            used - self.size(first, end)
        } else {
            used
        };
        if kept > limit {
            return None;
        }
        let completion = if stop {
            Completion::Stop {
                end: first - 1,
                next: (first, last),
            }
        } else {
            let from = end + 1;
            Completion::Rest(from, self.fit_end(from, last, limit))
        };
        Some((completion, kept))
    }
}

/// Why a section ends with a continuation.
pub(crate) fn continuation_reason(stopped_before: bool) -> &'static str {
    if stopped_before {
        "stopped before a construct that does not fit this response."
    } else {
        "the construct continues past this section and does not fit this response."
    }
}

/// A CLI numbered line: the number padded to the widest number that a range
/// of the file can show, two spaces, the line, and `\n`.
struct Plain;

impl LineCost for Plain {
    fn bytes(_number: usize, lines: usize, line: &str) -> usize {
        let width = (lines + 1).ilog10() as usize + 1;
        width + 2 + line.len() + 1
    }
}

/// Bytes that the header, the blank lines, and the continuation note of a CLI
/// section can use, apart from the two copies of the path.
const CLI_FRAME_BYTES: usize = 200;

/// A CLI `--section` read after construct completion.
pub(crate) struct CliSection {
    /// The `N-M` range to read.
    pub range: String,
    /// The line that names the next CLI call, if the read leaves part of a
    /// construct out.
    pub note: Option<String>,
}

/// Complete a CLI `--section N-M` read of `path` as `tilth_read` completes
/// `path#N-M`. With `budget` tokens, the response stays within that budget.
/// Without it, the range grows to the end of the construct it cuts. Returns
/// `None` when the section is not a line range, the file does not load as
/// text, or the end cuts no construct.
pub(crate) fn cli_section(
    path: &Path,
    section: &str,
    budget: Option<u64>,
    cache: &OutlineCache,
) -> Option<CliSection> {
    let (start, end) = super::parse_range(section)?;
    let view = FileView::<Plain>::load(path)?;
    let start = u32::try_from(start).ok()?;
    let end = u32::try_from(end).unwrap_or(u32::MAX).min(view.total());
    if start > end {
        return None;
    }
    let shown = path.display().to_string();
    let limit = budget.map_or(usize::MAX, |budget| {
        let bytes = usize::try_from(budget.saturating_mul(4)).unwrap_or(usize::MAX);
        bytes.saturating_sub(CLI_FRAME_BYTES + 2 * shown.len())
    });
    let used = view.size(start, end);
    let (completion, _) = view.complete((start, end), used, limit, |_| false, cache)?;
    let (end, next) = match completion {
        Completion::Grow(end) => (end, None),
        Completion::Stop { end, next } => (end, Some((next, true))),
        Completion::Rest(from, to) => (end, Some(((from, to), false))),
    };
    let note = next.map(|((from, to), stopped_before)| {
        let why = continuation_reason(stopped_before);
        format!("... {why} Next: tilth {shown} --section {from}-{to}")
    });
    Some(CliSection {
        range: format!("{start}-{end}"),
        note,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Line 1: `fn head`. Lines 3-7: `fn body`. Line 9: `fn tail`.
    const SOURCE: &str = "fn head() {}\n\nfn body() {\n    let a = 1;\n    let b = 2;\n    let c = 3;\n}\n\nfn tail() {}\n";

    fn fixture() -> (tempfile::TempDir, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("f.rs");
        std::fs::write(&path, SOURCE).unwrap();
        (dir, path)
    }

    #[test]
    fn complete_grows_stops_or_names_the_rest() {
        let (_dir, path) = fixture();
        let view = FileView::<Plain>::load(&path).unwrap();
        let cache = OutlineCache::new();
        let used = view.size(1, 4);
        assert_eq!(
            view.complete((1, 4), used, usize::MAX, |_| false, &cache),
            Some((Completion::Grow(7), view.size(1, 7)))
        );
        let limit = view.size(1, 7) - 1;
        assert_eq!(
            view.complete((1, 4), used, limit, |_| false, &cache),
            Some((
                Completion::Stop {
                    end: 2,
                    next: (3, 7)
                },
                view.size(1, 2)
            ))
        );
        // A request that starts inside the construct keeps the section's end.
        assert_eq!(
            view.complete((1, 4), used, limit, |line| line <= 3, &cache),
            Some((Completion::Rest(5, 7), used))
        );
        // A section that still overflows after it stops keeps its request.
        assert_eq!(
            view.complete((1, 4), used, view.size(1, 2) - 1, |_| false, &cache),
            None
        );
    }

    #[test]
    fn cli_section_completes_line_ranges_only() {
        let (_dir, path) = fixture();
        let cache = OutlineCache::new();
        let grown = cli_section(&path, "1-4", None, &cache).unwrap();
        assert_eq!((grown.range.as_str(), grown.note), ("1-7", None));
        assert!(cli_section(&path, "## Heading", None, &cache).is_none());
        assert!(cli_section(&path, "8-9", None, &cache).is_none());
    }
}
