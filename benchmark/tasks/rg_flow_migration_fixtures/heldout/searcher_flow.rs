use std::io;

use grep_matcher::Flow;
use grep_regex::RegexMatcher;
use grep_searcher::{
    BinaryDetection, Searcher, SearcherBuilder, Sink, SinkContext, SinkFinish,
    SinkMatch, sinks,
};

const HAYSTACK: &str = "foo\nbar\nfoo\nbaz\nfoo\n";

fn matcher() -> RegexMatcher {
    RegexMatcher::new("foo").unwrap()
}

#[test]
fn utf8_closure_continue_sees_every_match() {
    let mut count = 0;
    Searcher::new()
        .search_slice(
            matcher(),
            HAYSTACK.as_bytes(),
            sinks::UTF8(|_, _| {
                count += 1;
                Ok(Flow::Continue)
            }),
        )
        .unwrap();
    assert_eq!(count, 3);
}

#[test]
fn utf8_closure_stop_ends_the_search() {
    let mut count = 0;
    Searcher::new()
        .search_slice(
            matcher(),
            HAYSTACK.as_bytes(),
            sinks::UTF8(|_, _| {
                count += 1;
                Ok(Flow::Stop)
            }),
        )
        .unwrap();
    assert_eq!(count, 1);
}

#[test]
fn lossy_and_bytes_closures_honor_stop() {
    let mut lossy = 0;
    Searcher::new()
        .search_slice(
            matcher(),
            HAYSTACK.as_bytes(),
            sinks::Lossy(|_, _| {
                lossy += 1;
                Ok(if lossy == 2 { Flow::Stop } else { Flow::Continue })
            }),
        )
        .unwrap();
    assert_eq!(lossy, 2);

    let mut bytes = 0;
    Searcher::new()
        .search_slice(
            matcher(),
            HAYSTACK.as_bytes(),
            sinks::Bytes(|_, _| {
                bytes += 1;
                Ok(Flow::Stop)
            }),
        )
        .unwrap();
    assert_eq!(bytes, 1);
}

#[test]
fn closure_error_still_propagates() {
    let result = Searcher::new().search_slice(
        matcher(),
        HAYSTACK.as_bytes(),
        sinks::UTF8(|_, _| Err(io::Error::other("boom"))),
    );
    assert!(result.is_err());
}

#[derive(Default)]
struct Spy {
    stop_matched_at: Option<usize>,
    stop_context_at: Option<usize>,
    stop_break_at: Option<usize>,
    stop_binary: bool,
    stop_begin: bool,
    begun: usize,
    matched: usize,
    context: usize,
    breaks: usize,
    binary: usize,
    finished: usize,
}

fn flow(stop: bool) -> Flow {
    if stop { Flow::Stop } else { Flow::Continue }
}

impl Sink for Spy {
    type Error = io::Error;

    fn matched(
        &mut self,
        _: &Searcher,
        _: &SinkMatch<'_>,
    ) -> Result<Flow, io::Error> {
        self.matched += 1;
        Ok(flow(self.stop_matched_at == Some(self.matched)))
    }

    fn context(
        &mut self,
        _: &Searcher,
        _: &SinkContext<'_>,
    ) -> Result<Flow, io::Error> {
        self.context += 1;
        Ok(flow(self.stop_context_at == Some(self.context)))
    }

    fn context_break(&mut self, _: &Searcher) -> Result<Flow, io::Error> {
        self.breaks += 1;
        Ok(flow(self.stop_break_at == Some(self.breaks)))
    }

    fn binary_data(&mut self, _: &Searcher, _: u64) -> Result<Flow, io::Error> {
        self.binary += 1;
        Ok(flow(self.stop_binary))
    }

    fn begin(&mut self, _: &Searcher) -> Result<Flow, io::Error> {
        self.begun += 1;
        Ok(flow(self.stop_begin))
    }

    fn finish(&mut self, _: &Searcher, _: &SinkFinish) -> Result<(), io::Error> {
        self.finished += 1;
        Ok(())
    }
}

fn run(searcher: &mut Searcher, spy: &mut Spy, haystack: &str) {
    searcher.search_slice(matcher(), haystack.as_bytes(), &mut *spy).unwrap();
}

#[test]
fn custom_sink_defaults_continue() {
    let mut spy = Spy::default();
    run(&mut Searcher::new(), &mut spy, HAYSTACK);
    assert_eq!((spy.begun, spy.matched, spy.finished), (1, 3, 1));
}

#[test]
fn matched_stop_calls_finish_once() {
    let mut spy = Spy { stop_matched_at: Some(2), ..Spy::default() };
    run(&mut Searcher::new(), &mut spy, HAYSTACK);
    assert_eq!((spy.matched, spy.finished), (2, 1));
}

#[test]
fn begin_stop_skips_all_matches() {
    let mut spy = Spy { stop_begin: true, ..Spy::default() };
    run(&mut Searcher::new(), &mut spy, HAYSTACK);
    assert_eq!(spy.matched, 0);
}

#[test]
fn context_stop_ends_the_search() {
    let mut searcher = SearcherBuilder::new().after_context(1).build();
    let mut all = Spy::default();
    run(&mut searcher, &mut all, HAYSTACK);
    assert_eq!((all.matched, all.context), (3, 2));

    let mut spy = Spy { stop_context_at: Some(1), ..Spy::default() };
    run(&mut searcher, &mut spy, HAYSTACK);
    assert_eq!((spy.matched, spy.context, spy.finished), (1, 1, 1));
}

#[test]
fn context_break_stop_ends_the_search() {
    let hay = "foo\nx\ny\nz\nfoo\n";
    let mut searcher = SearcherBuilder::new().after_context(1).build();
    let mut all = Spy::default();
    run(&mut searcher, &mut all, hay);
    assert_eq!((all.matched, all.breaks), (2, 1));

    let mut spy = Spy { stop_break_at: Some(1), ..Spy::default() };
    run(&mut searcher, &mut spy, hay);
    assert_eq!((spy.matched, spy.breaks, spy.finished), (1, 1, 1));
}

#[test]
fn binary_data_stop_ends_the_search() {
    let hay = "foo\nbar\0baz\nfoo\nfoo\n";
    let mut searcher =
        SearcherBuilder::new().binary_detection(BinaryDetection::convert(0)).build();
    let mut all = Spy::default();
    run(&mut searcher, &mut all, hay);
    assert_eq!(all.binary, 1);
    assert!(all.matched >= 2, "matched {}", all.matched);

    let mut spy = Spy { stop_binary: true, ..Spy::default() };
    run(&mut searcher, &mut spy, hay);
    assert_eq!((spy.binary, spy.finished), (1, 1));
    assert!(spy.matched < all.matched, "stop kept searching");
}

#[test]
fn stop_is_honored_by_every_search_entry_point() {
    let stop = || Spy { stop_matched_at: Some(1), ..Spy::default() };

    let mut spy = stop();
    let mut searcher = SearcherBuilder::new().multi_line(true).build();
    run(&mut searcher, &mut spy, HAYSTACK);
    assert_eq!(spy.matched, 1, "multi-line slice");

    let mut spy = stop();
    Searcher::new()
        .search_reader(matcher(), HAYSTACK.as_bytes(), &mut spy)
        .unwrap();
    assert_eq!(spy.matched, 1, "reader");

    let mut spy = stop();
    SearcherBuilder::new()
        .multi_line(true)
        .build()
        .search_reader(matcher(), HAYSTACK.as_bytes(), &mut spy)
        .unwrap();
    assert_eq!(spy.matched, 1, "multi-line reader");

    let mut spy = stop();
    Searcher::new()
        .search_slice(matcher(), HAYSTACK.as_bytes(), &mut spy)
        .unwrap();
    assert_eq!((spy.matched, spy.finished), (1, 1), "slice");
}
