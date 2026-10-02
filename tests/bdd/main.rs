#![expect(
    clippy::needless_pass_by_value,
    reason = "Cucumber captures require FromStr"
)]

mod fixture_catalog;
mod session;

use cucumber::{given, then, when, World};
use fixture_catalog::{Fixture, MatchKind};
use serde_json::{json, Value};
use std::fs;

#[derive(Debug, Default, World)]
struct TilthWorld {
    session: Option<session::Session>,
    response: Value,
    read_path: String,
    read_tag: String,
    before_write: Vec<u8>,
    fixture: Option<Fixture>,
    fixture_path: String,
}

impl TilthWorld {
    fn session(&mut self) -> &mut session::Session {
        self.session.as_mut().expect("Given an example workspace")
    }

    fn call(&mut self, tool: &str, arguments: Value) {
        self.response = self.session().tool(tool, arguments);
    }

    fn successful_text(&self) -> &str {
        assert_ne!(self.response["isError"], true, "{self:?}");
        self.response["content"][0]["text"]
            .as_str()
            .expect("MCP text result")
    }

    fn search_payload(&self) -> Value {
        serde_json::from_str(self.successful_text()).expect("search JSON envelope")
    }

    fn write_fixture(&mut self, path: &str, source: &str) {
        let path = self.session().workspace().join(path);
        fs::create_dir_all(path.parent().expect("fixture parent")).expect("create fixture parent");
        fs::write(path, source).expect("write fixture");
    }
}

#[given("an example workspace")]
fn example_workspace(world: &mut TilthWorld) {
    world.session = Some(session::Session::start());
}

#[given(expr = "a {string} fixture at {string}")]
fn language_fixture(world: &mut TilthWorld, language: String, path: String) {
    let fixture = fixture_catalog::fixture(&language);
    world.write_fixture(&path, fixture.source);
    world.fixture = Some(fixture);
    world.fixture_path = path;
}

#[given(expr = "the file {string} contains exactly")]
fn file_fixture(world: &mut TilthWorld, path: String, step: &cucumber::gherkin::Step) {
    let source = format!("{}\n", docstring(step));
    world.write_fixture(&path, &source);
}

#[when(expr = "I search for {string}")]
fn search(world: &mut TilthWorld, query: String) {
    world.call("tilth_search", json!({"queries": [{"query": query}]}));
}

#[when(expr = "I inspect dependencies of {string}")]
fn dependencies(world: &mut TilthWorld, path: String) {
    world.call("tilth_deps", json!({"path": path}));
}

#[when("I search for the fixture marker")]
fn search_fixture(world: &mut TilthWorld) {
    search(
        world,
        world.fixture.expect("language fixture").query.to_owned(),
    );
}

#[when("I search for the updated fixture marker")]
fn search_updated_fixture(world: &mut TilthWorld) {
    search(
        world,
        world
            .fixture
            .expect("language fixture")
            .replacement
            .to_owned(),
    );
}

#[then(expr = "the search resolves {string} in {string} at line {int}")]
fn resolved(
    world: &mut TilthWorld,
    name: String,
    path: String,
    line: u64,
    step: &cucumber::gherkin::Step,
) {
    assert_symbol_result(world, &name, &path, line, &docstring(step));
}

#[then("the search resolves the fixture marker")]
fn fixture_resolved(world: &mut TilthWorld) {
    let fixture = world.fixture.expect("language fixture");
    let path = world.fixture_path.clone();
    match fixture.kind {
        MatchKind::Symbol => {
            assert_symbol_result(world, fixture.query, &path, fixture.line, fixture.core);
        }
        MatchKind::Literal => {
            let source = fixture
                .source
                .lines()
                .nth(fixture.line as usize - 1)
                .expect("literal fixture line");
            assert_literal_result(world, &path, fixture.line, source);
        }
    }
}

#[then("the search resolves the updated fixture marker")]
fn updated_fixture_resolved(world: &mut TilthWorld) {
    let fixture = world.fixture.expect("language fixture");
    let path = world.fixture_path.clone();
    let source = fixture
        .source
        .replacen(fixture.query, fixture.replacement, 1);
    let core = fixture.core.replacen(fixture.query, fixture.replacement, 1);
    match fixture.kind {
        MatchKind::Symbol => {
            assert_symbol_result(world, fixture.replacement, &path, fixture.line, &core);
        }
        MatchKind::Literal => {
            let matched_source = source
                .lines()
                .nth(fixture.line as usize - 1)
                .expect("literal fixture line");
            assert_literal_result(world, &path, fixture.line, matched_source);
        }
    }
}

#[then("the search has no matches")]
fn no_matches(world: &mut TilthWorld) {
    let payload = world.search_payload();
    let results = payload["results"].as_array().expect("search results");
    assert_eq!(results.len(), 1, "{world:?}");
    assert_eq!(results[0]["status"], "no_match", "{world:?}");
    assert_eq!(results[0]["resolved_as"], "miss", "{world:?}");
    assert_eq!(results[0]["total_found"], 0, "{world:?}");
}

#[then("the original fixture marker has no matches")]
fn original_fixture_missing(world: &mut TilthWorld) {
    search(
        world,
        world.fixture.expect("language fixture").query.to_owned(),
    );
    no_matches(world);
}

#[then(expr = "the search is ambiguous between {string} and {string}")]
fn ambiguous_search(world: &mut TilthWorld, first: String, second: String) {
    let payload = world.search_payload();
    let result = &payload["results"][0];
    assert_eq!(result["status"], "ambiguous", "{world:?}");
    assert_eq!(result["resolved_as"], "ambiguous", "{world:?}");
    let paths = result["candidates"]
        .as_array()
        .expect("ambiguous candidates")
        .iter()
        .map(|candidate| candidate["path"].as_str().expect("candidate path"))
        .collect::<Vec<_>>();
    assert_eq!(paths, [first.as_str(), second.as_str()], "{world:?}");
}

#[then(expr = "the search is ambiguous between {string} lines {int} and {int}")]
fn ambiguous_same_file(world: &mut TilthWorld, path: String, first: u64, second: u64) {
    let payload = world.search_payload();
    let result = &payload["results"][0];
    assert_eq!(result["status"], "ambiguous", "{world:?}");
    let mut candidates = result["candidates"]
        .as_array()
        .expect("ambiguous candidates")
        .iter()
        .map(|candidate| {
            (
                candidate["path"]
                    .as_str()
                    .expect("candidate path")
                    .to_owned(),
                candidate["line"].as_u64().expect("candidate line"),
            )
        })
        .collect::<Vec<_>>();
    candidates.sort();
    assert_eq!(
        candidates,
        [(path.clone(), first), (path, second)],
        "{world:?}"
    );
}

#[then(expr = "the content search finds {int} match in {string} at line {int}")]
fn content_match(
    world: &mut TilthWorld,
    count: u64,
    path: String,
    line: u64,
    step: &cucumber::gherkin::Step,
) {
    let payload = world.search_payload();
    let result = &payload["results"][0];
    assert_eq!(result["resolved_as"], "literal", "{world:?}");
    assert_eq!(result["status"], "ok", "{world:?}");
    assert_eq!(result["total_found"], count, "{world:?}");
    assert_literal_preview(world, result, &path, line, &docstring(step));
}

#[when(expr = "I read {string}")]
fn read(world: &mut TilthWorld, selector: String) {
    world.call("tilth_read", json!({"paths": [selector]}));
    let path = selector
        .split_once('#')
        .map_or(selector.as_str(), |(path, _)| path);
    let prefix = format!("[{}#", world.session().workspace().join(path).display());
    let tag = world
        .successful_text()
        .lines()
        .find_map(|line| line.strip_prefix(&prefix)?.strip_suffix(']'))
        .unwrap_or_else(|| panic!("missing response-minted read tag\n{world:?}"))
        .to_owned();
    assert_eq!(tag.len(), 4, "{world:?}");
    assert!(
        tag.bytes().all(|byte| byte.is_ascii_hexdigit()),
        "{world:?}"
    );
    world.read_tag = tag;
    path.clone_into(&mut world.read_path);
}

#[when("I read the fixture")]
fn read_fixture(world: &mut TilthWorld) {
    read(world, world.fixture_path.clone());
}

#[then("the read returns the complete fixture source")]
fn fixture_read_source(world: &mut TilthWorld) {
    let fixture = world.fixture.expect("language fixture");
    assert_eq!(numbered_source(world.successful_text()), fixture.source);
}

#[then("the read returns exactly")]
fn read_exact(world: &mut TilthWorld, step: &cucumber::gherkin::Step) {
    assert_eq!(numbered_source(world.successful_text()), docstring(step));
}

#[then(expr = "the read returns lines {int} through {int} exactly")]
fn read_range_exact(world: &mut TilthWorld, first: u64, last: u64, step: &cucumber::gherkin::Step) {
    let lines = numbered_lines(world.successful_text());
    assert_eq!(
        lines.iter().map(|(number, _)| *number).collect::<Vec<_>>(),
        (first..=last).collect::<Vec<_>>(),
        "{world:?}"
    );
    assert_eq!(
        lines
            .into_iter()
            .map(|(_, source)| source)
            .collect::<Vec<_>>()
            .join("\n"),
        docstring(step),
        "{world:?}"
    );
}

#[then("the search callers are exactly")]
fn search_callers(world: &mut TilthWorld, step: &cucumber::gherkin::Step) {
    let hint = world.search_payload()["hints"]
        .as_array()
        .expect("search hints")
        .iter()
        .find(|hint| hint["kind"] == "fetch_callers")
        .expect("fetch_callers hint")
        .clone();
    world.call("tilth_search", json!({"queries": [{"follow": hint}]}));
    let payload = world.search_payload();
    let mut actual = payload["results"][0]["items"]
        .as_array()
        .expect("caller items")
        .iter()
        .map(|item| {
            format!(
                "{}:{} {}",
                item["path"].as_str().expect("caller path"),
                item["line"],
                item["name"].as_str().expect("caller name")
            )
        })
        .collect::<Vec<_>>();
    actual.sort();
    assert_eq!(actual.join("\n"), docstring(step), "{world:?}");
}
#[then(expr = "the dependency report has exactly 1 local dependency {string}")]
fn exact_local_dependency(world: &mut TilthWorld, path: String) {
    let text = world.successful_text();
    assert!(
        text.lines()
            .next()
            .is_some_and(|line| line.contains("— 1 local,")),
        "{world:?}"
    );
    let local_paths = dependency_section_paths(text, "## Uses (local)");
    assert_eq!(local_paths, [path], "{world:?}");
}

#[then(
    expr = "the dependency report has exactly 1 caller dependent {string} at line {int} owned by {string} calling {string}"
)]
fn exact_caller_dependent(
    world: &mut TilthWorld,
    path: String,
    line: u64,
    owner: String,
    symbol: String,
) {
    let text = world.successful_text();
    assert!(
        text.lines()
            .next()
            .is_some_and(|line| line.contains("1 dependent")),
        "{world:?}"
    );
    let used_by = text.split_once("## Used by\n").expect("used-by section").1;
    let rows: Vec<Vec<&str>> = used_by
        .lines()
        .take_while(|line| !line.is_empty())
        .map(|row| row.split_whitespace().collect())
        .collect();
    assert_eq!(
        rows,
        [vec![
            format!("{path}:{line}").as_str(),
            owner.as_str(),
            "→",
            symbol.as_str()
        ]],
        "{world:?}"
    );
}

fn dependency_section_paths(text: &str, heading: &str) -> Vec<String> {
    text.split_once(&format!("{heading}\n"))
        .map(|(_, section)| {
            section
                .lines()
                .take_while(|line| !line.is_empty())
                .map(|line| {
                    line.split_whitespace()
                        .next()
                        .expect("dependency path")
                        .to_owned()
                })
                .collect()
        })
        .unwrap_or_default()
}

#[when(expr = "another editor replaces {string} with {string} in {string}")]
fn external_edit(world: &mut TilthWorld, old: String, new: String, path: String) {
    let path = world.session().workspace().join(path);
    let original = fs::read_to_string(&path).expect("read external edit target");
    assert_eq!(
        original.matches(&old).count(),
        1,
        "external edit must match once"
    );
    fs::write(path, original.replacen(&old, &new, 1)).expect("apply external edit");
}

#[when(expr = "I replace {string} with {string} using the read tag")]
fn replace(world: &mut TilthWorld, old: String, new: String) {
    let path = world.session().workspace().join(&world.read_path);
    world.before_write = fs::read(path).expect("capture bytes before tagged edit");
    world.call(
        "tilth_write",
        json!({"edits": [{
            "path": world.read_path, "tag": world.read_tag,
            "ops": [{"op": "replace_text", "old": old, "new": new}]
        }]}),
    );
}

#[when("I replace the fixture marker using the read tag")]
fn replace_fixture(world: &mut TilthWorld) {
    let fixture = world.fixture.expect("language fixture");
    replace(
        world,
        fixture.query.to_owned(),
        fixture.replacement.to_owned(),
    );
}

#[then("the edit is applied")]
fn applied(world: &mut TilthWorld) {
    let text = world.successful_text();
    assert_eq!(text.lines().nth(1), Some("applied"), "{world:?}");
}

#[then("the conflicting edit is rejected")]
fn rejected(world: &mut TilthWorld) {
    assert_eq!(world.response["isError"], true, "{world:?}");
    let text = world.response["content"][0]["text"]
        .as_str()
        .expect("rejection text");
    assert!(text.contains("text to replace was not found"), "{world:?}");
    assert!(
        text.contains("The file also changed since the read"),
        "{world:?}"
    );
}

#[then("the edited file is unchanged")]
fn unchanged(world: &mut TilthWorld) {
    let path = world.session().workspace().join(&world.read_path);
    assert_eq!(
        fs::read(path).expect("read rejected target"),
        world.before_write,
        "{world:?}"
    );
}

#[then("the fixture file contains the complete edited source")]
fn fixture_file_edited(world: &mut TilthWorld) {
    let fixture = world.fixture.expect("language fixture");
    let expected = fixture
        .source
        .replacen(fixture.query, fixture.replacement, 1);
    let path = world.session().workspace().join(&world.fixture_path);
    assert_eq!(fs::read(path).expect("read fixture"), expected.as_bytes());
    assert!(
        !expected.contains(fixture.query),
        "old marker remains in oracle"
    );
}

#[then(expr = "the file {string} contains exactly")]
fn file_equals(world: &mut TilthWorld, path: String, step: &cucumber::gherkin::Step) {
    let path = world.session().workspace().join(path);
    let expected = format!("{}\n", docstring(step));
    assert_eq!(
        fs::read(path).expect("read resulting file"),
        expected.as_bytes(),
        "{world:?}"
    );
}

fn assert_symbol_result(world: &TilthWorld, name: &str, path: &str, line: u64, core: &str) {
    let payload = world.search_payload();
    let results = payload["results"].as_array().expect("search results");
    assert_eq!(results.len(), 1, "{world:?}");
    let result = &results[0];
    assert_eq!(result["resolved_as"], "symbol", "{world:?}");
    assert!(
        matches!(result["status"].as_str(), Some("ok" | "partial")),
        "{world:?}"
    );
    assert_eq!(result["target"]["name"], name, "{world:?}");
    assert_eq!(result["target"]["path"], path, "{world:?}");
    assert_eq!(result["target"]["line"], line, "{world:?}");
    assert_eq!(result["core"], core, "{world:?}");
}

fn assert_literal_result(world: &TilthWorld, path: &str, line: u64, source: &str) {
    let payload = world.search_payload();
    let result = &payload["results"][0];
    assert_eq!(result["resolved_as"], "literal", "{world:?}");
    assert_eq!(result["status"], "ok", "{world:?}");
    assert_eq!(result["total_found"], 1, "{world:?}");
    assert_literal_preview(world, result, path, line, source);
}

fn assert_literal_preview(world: &TilthWorld, result: &Value, path: &str, line: u64, source: &str) {
    let preview = result["preview"].as_str().expect("literal preview");
    let location = preview
        .lines()
        .find_map(|line| {
            line.strip_prefix("### ")?
                .split_once(" [")
                .map(|pair| pair.0)
        })
        .expect("literal match location");
    assert_eq!(location, format!("{path}:{line}"), "{world:?}");

    let source_prefix = format!("-> [{line}]   ");
    let matched_source = preview
        .lines()
        .find_map(|line| line.strip_prefix(&source_prefix))
        .expect("literal matched source");
    assert_eq!(matched_source, source, "{world:?}");
}

fn numbered_lines(text: &str) -> Vec<(u64, &str)> {
    text.lines()
        .filter_map(|line| {
            let (number, source) = line.split_once(':')?;
            number.parse::<u64>().ok().map(|number| (number, source))
        })
        .collect()
}

fn numbered_source(text: &str) -> String {
    numbered_lines(text)
        .into_iter()
        .map(|(_, source)| source)
        .collect::<Vec<_>>()
        .join("\n")
}

fn docstring(step: &cucumber::gherkin::Step) -> String {
    let text = step
        .docstring
        .as_deref()
        .and_then(|text| text.strip_prefix('\n'))
        .and_then(|text| text.strip_suffix('\n'))
        .expect("expected a docstring with delimiters on separate lines");
    if text.lines().all(|line| line.starts_with('|')) {
        text.lines()
            .map(|line| line.strip_prefix('|').expect("checked marker"))
            .collect::<Vec<_>>()
            .join("\n")
    } else {
        text.to_owned()
    }
}

#[derive(clap::Args)]
struct CargoFilter {
    /// Literal scenario-name substring, as passed by cargo test FILTER.
    #[arg(value_name = "FILTER")]
    filter: Option<String>,
}

fn main() {
    let mut cli = cucumber::cli::Opts::<_, _, _, CargoFilter>::parsed();
    let filter = cli.custom.filter.take();
    futures::executor::block_on(
        TilthWorld::cucumber()
            .with_cli(cli)
            .max_concurrent_scenarios(1)
            .fail_on_skipped_with(|_, _, _| true)
            .filter_run_and_exit("tests/bdd/features", move |_, _, scenario| {
                filter
                    .as_ref()
                    .is_none_or(|filter| scenario.name.contains(filter))
            }),
    );
}
