mod session;

use cucumber::{given, then, when, World};
use serde_json::{json, Value};
use std::fs;

#[derive(Debug, Default, World)]
struct TilthWorld {
    session: Option<session::Session>,
    response: Value,
    read_path: String,
    read_tag: String,
    before_write: Vec<u8>,
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
}

#[given("an example workspace")]
fn example_workspace(world: &mut TilthWorld) {
    world.session = Some(session::Session::start());
}

#[expect(
    clippy::needless_pass_by_value,
    reason = "Cucumber captures require FromStr"
)]
#[when(expr = "I search for {string}")]
fn search(world: &mut TilthWorld, query: String) {
    world.call("tilth_search", json!({"queries": [{"query": query}]}));
}

#[expect(
    clippy::needless_pass_by_value,
    reason = "Cucumber captures require FromStr"
)]
#[then(expr = "the search resolves {string} in {string} at line {int}")]
fn resolved(
    world: &mut TilthWorld,
    name: String,
    path: String,
    line: u64,
    step: &cucumber::gherkin::Step,
) {
    let payload: Value =
        serde_json::from_str(world.successful_text()).expect("search JSON envelope");
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
    assert_eq!(result["core"], docstring(step), "{world:?}");
}

#[then("the search has no matches")]
fn no_matches(world: &mut TilthWorld) {
    let payload: Value =
        serde_json::from_str(world.successful_text()).expect("search JSON envelope");
    let results = payload["results"].as_array().expect("search results");
    assert_eq!(results.len(), 1, "{world:?}");
    assert_eq!(results[0]["status"], "no_match", "{world:?}");
    assert_eq!(results[0]["resolved_as"], "miss", "{world:?}");
    assert_eq!(results[0]["total_found"], 0, "{world:?}");
}

#[when(expr = "I read {string}")]
fn read(world: &mut TilthWorld, path: String) {
    world.call("tilth_read", json!({"paths": [path]}));
    let prefix = format!("[{}#", world.session().workspace().join(&path).display());
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
    world.read_path = path;
}

#[expect(
    clippy::needless_pass_by_value,
    reason = "Cucumber captures require FromStr"
)]
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

#[expect(
    clippy::needless_pass_by_value,
    reason = "Cucumber captures require FromStr"
)]
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

fn docstring(step: &cucumber::gherkin::Step) -> &str {
    // Gherkin keeps the newlines next to the opening and closing delimiters.
    step.docstring
        .as_deref()
        .and_then(|text| text.strip_prefix('\n'))
        .and_then(|text| text.strip_suffix('\n'))
        .expect("expected a docstring with delimiters on separate lines")
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
