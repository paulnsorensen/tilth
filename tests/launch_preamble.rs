const PREAMBLE: &str = include_str!("../prompts/preamble.md");

const BANNED_TERMS: [&str; 6] = [
    "hallouminate",
    "wiki",
    "delegat",
    "taste-tester",
    "reviewer",
    "fork_turns",
];

#[test]
fn launch_preamble_has_required_headings() {
    for heading in ["## Tools", "## Safety", "## Verification"] {
        assert!(
            PREAMBLE.lines().any(|line| line == heading),
            "preamble is missing heading line {heading:?}"
        );
    }
}

#[test]
fn launch_preamble_has_no_personal_policy_terms() {
    let lowered = PREAMBLE.to_lowercase();
    for term in BANNED_TERMS {
        assert!(
            !lowered.contains(term),
            "preamble contains banned term {term:?}"
        );
    }
}

#[test]
fn launch_preamble_first_line_is_unique_heading() {
    let first = PREAMBLE
        .lines()
        .find(|line| !line.trim().is_empty())
        .expect("preamble is empty");
    assert!(
        first.starts_with("# "),
        "first line is not an H1: {first:?}"
    );
    assert!(
        first.to_lowercase().contains("tilth"),
        "first heading lacks `tilth`: {first:?}"
    );
    let h1_count = PREAMBLE.lines().filter(|l| l.starts_with("# ")).count();
    assert_eq!(h1_count, 1, "preamble must have exactly one H1");
}
