use grep_matcher::{Flow, Match, Matcher};
use grep_regex::RegexMatcher;

const HAYSTACK: &[u8] = b"foo bar foo baz foo";

fn matcher() -> RegexMatcher {
    RegexMatcher::new("foo").unwrap()
}

fn collect<F>(mut each: F) -> Vec<Match>
where
    F: FnMut(usize) -> Flow,
{
    let mut found = vec![];
    matcher()
        .find_iter(HAYSTACK, |m| {
            found.push(m);
            each(found.len())
        })
        .unwrap();
    found
}

#[test]
fn find_iter_continue_visits_every_match() {
    assert_eq!(collect(|_| Flow::Continue).len(), 3);
}

#[test]
fn find_iter_stop_ends_iteration() {
    assert_eq!(collect(|_| Flow::Stop).len(), 1);
    assert_eq!(collect(|n| if n == 2 { Flow::Stop } else { Flow::Continue }).len(), 2);
}

#[test]
fn find_iter_at_stop_ends_iteration() {
    let mut seen = 0;
    matcher()
        .find_iter_at(HAYSTACK, 4, |_| {
            seen += 1;
            Flow::Stop
        })
        .unwrap();
    assert_eq!(seen, 1);
}

#[test]
fn try_find_iter_stop_and_error() {
    let mut seen = 0;
    let result = matcher()
        .try_find_iter(HAYSTACK, |_| -> Result<Flow, &'static str> {
            seen += 1;
            Ok(Flow::Stop)
        })
        .unwrap();
    assert_eq!((seen, result), (1, Ok(())));

    let result = matcher()
        .try_find_iter(HAYSTACK, |_| -> Result<Flow, &'static str> {
            Err("bad")
        })
        .unwrap();
    assert_eq!(result, Err("bad"));
}

#[test]
fn captures_iter_stop_ends_iteration() {
    let m = matcher();
    let mut caps = m.new_captures().unwrap();
    let mut seen = 0;
    m.captures_iter(HAYSTACK, &mut caps, |_| {
        seen += 1;
        Flow::Continue
    })
    .unwrap();
    assert_eq!(seen, 3);

    let mut seen = 0;
    m.captures_iter(HAYSTACK, &mut caps, |_| {
        seen += 1;
        Flow::Stop
    })
    .unwrap();
    assert_eq!(seen, 1);
}

#[test]
fn replace_stops_after_the_first_append() {
    let m = matcher();
    let mut dest = vec![];
    m.replace(HAYSTACK, &mut dest, |_, dest| {
        dest.extend_from_slice(b"X");
        Flow::Stop
    })
    .unwrap();
    assert_eq!(dest, b"X bar foo baz foo".to_vec());
}

#[test]
fn replace_continue_replaces_everything() {
    let m = matcher();
    let mut dest = vec![];
    m.replace(HAYSTACK, &mut dest, |_, dest| {
        dest.extend_from_slice(b"X");
        Flow::Continue
    })
    .unwrap();
    assert_eq!(dest, b"X bar X baz X".to_vec());
}

#[test]
fn replace_with_captures_stop_ends_replacement() {
    let m = matcher();
    let mut caps = m.new_captures().unwrap();
    let mut dest = vec![];
    m.replace_with_captures(HAYSTACK, &mut caps, &mut dest, |_, dest| {
        dest.extend_from_slice(b"Y");
        Flow::Continue
    })
    .unwrap();
    assert_eq!(dest, b"Y bar Y baz Y".to_vec());
}
