use grep_matcher::Flow;
use grep_printer::{JSON, Standard, SummaryBuilder, SummaryKind};
use grep_regex::RegexMatcher;
use grep_searcher::{Searcher, Sink, SinkContext, SinkFinish, SinkMatch};

const HAYSTACK: &str = "foo\nbar\nfoo\nbaz\nfoo\n";

/// Wraps a sink and records every flow value it returns.
struct Spy<S> {
    inner: S,
    matched: Vec<Flow>,
    context: Vec<Flow>,
    begin: Vec<Flow>,
}

impl<S> Spy<S> {
    fn new(inner: S) -> Spy<S> {
        Spy { inner, matched: vec![], context: vec![], begin: vec![] }
    }
}

impl<S: Sink> Sink for Spy<S> {
    type Error = S::Error;

    fn matched(
        &mut self,
        searcher: &Searcher,
        mat: &SinkMatch<'_>,
    ) -> Result<Flow, S::Error> {
        let flow = self.inner.matched(searcher, mat)?;
        self.matched.push(flow);
        Ok(flow)
    }

    fn context(
        &mut self,
        searcher: &Searcher,
        context: &SinkContext<'_>,
    ) -> Result<Flow, S::Error> {
        let flow = self.inner.context(searcher, context)?;
        self.context.push(flow);
        Ok(flow)
    }

    fn context_break(&mut self, searcher: &Searcher) -> Result<Flow, S::Error> {
        self.inner.context_break(searcher)
    }

    fn begin(&mut self, searcher: &Searcher) -> Result<Flow, S::Error> {
        let flow = self.inner.begin(searcher)?;
        self.begin.push(flow);
        Ok(flow)
    }

    fn finish(
        &mut self,
        searcher: &Searcher,
        finish: &SinkFinish,
    ) -> Result<(), S::Error> {
        self.inner.finish(searcher, finish)
    }
}

fn matcher() -> RegexMatcher {
    RegexMatcher::new("foo").unwrap()
}

#[test]
fn standard_sink_continues_after_every_match() {
    let mut printer = Standard::new_no_color(vec![]);
    let mut spy = Spy::new(printer.sink(matcher()));
    Searcher::new()
        .search_slice(matcher(), HAYSTACK.as_bytes(), &mut spy)
        .unwrap();
    assert_eq!(spy.begin, vec![Flow::Continue]);
    assert_eq!(spy.matched, vec![Flow::Continue; 3]);
    drop(spy);
    let out = String::from_utf8(printer.into_inner().into_inner()).unwrap();
    assert_eq!(out, "1:foo\n3:foo\n5:foo\n");
}

#[test]
fn standard_sink_continues_through_context_lines() {
    let mut printer = Standard::new_no_color(vec![]);
    let mut spy = Spy::new(printer.sink(matcher()));
    let mut searcher =
        grep_searcher::SearcherBuilder::new().after_context(1).build();
    searcher.search_slice(matcher(), HAYSTACK.as_bytes(), &mut spy).unwrap();
    assert_eq!(spy.matched, vec![Flow::Continue; 3]);
    assert_eq!(spy.context, vec![Flow::Continue; 2]);
}

#[test]
fn json_sink_continues_after_every_match() {
    let mut printer = JSON::new(vec![]);
    let mut spy = Spy::new(printer.sink(matcher()));
    Searcher::new()
        .search_slice(matcher(), HAYSTACK.as_bytes(), &mut spy)
        .unwrap();
    assert_eq!(spy.begin, vec![Flow::Continue]);
    assert_eq!(spy.matched, vec![Flow::Continue; 3]);
    drop(spy);
    let out = String::from_utf8(printer.into_inner()).unwrap();
    assert_eq!(out.matches("\"type\":\"match\"").count(), 3, "{out}");
}

#[test]
fn quiet_summary_stops_at_the_first_match() {
    let mut printer = SummaryBuilder::new()
        .kind(SummaryKind::QuietWithMatch)
        .build_no_color(vec![]);
    let mut spy = Spy::new(printer.sink(matcher()));
    Searcher::new()
        .search_slice(matcher(), HAYSTACK.as_bytes(), &mut spy)
        .unwrap();
    assert_eq!(spy.matched, vec![Flow::Stop]);
}

#[test]
fn counting_summary_never_stops_early() {
    let mut printer = SummaryBuilder::new()
        .kind(SummaryKind::Count)
        .build_no_color(vec![]);
    let mut spy = Spy::new(printer.sink(matcher()));
    Searcher::new()
        .search_slice(matcher(), HAYSTACK.as_bytes(), &mut spy)
        .unwrap();
    assert_eq!(spy.matched, vec![Flow::Continue; 3]);
    drop(spy);
    let out = String::from_utf8(printer.into_inner().into_inner()).unwrap();
    assert_eq!(out, "3\n");
}
