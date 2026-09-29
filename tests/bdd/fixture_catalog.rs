#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MatchKind {
    Symbol,
    Literal,
}

#[derive(Clone, Copy, Debug)]
pub struct Fixture {
    pub source: &'static str,
    pub core: &'static str,
    pub query: &'static str,
    pub replacement: &'static str,
    pub line: u64,
    pub kind: MatchKind,
}

pub fn fixture(language: &str) -> Fixture {
    match language {
        "Rust" => symbol(
            "pub fn language_marker() -> u8 {\n    1\n}\n\npub fn untouched() -> u8 { 9 }\n",
            "pub fn language_marker() -> u8 {\n    1\n}",
            "language_marker",
            "language_updated",
            1,
        ),
        "TypeScript" => symbol(
            "export function languageMarker(): number {\n  return 1;\n}\n\nexport const untouched = 9;\n",
            "export function languageMarker(): number {\n  return 1;\n}",
            "languageMarker",
            "languageUpdated",
            1,
        ),
        "TSX" => symbol(
            "export function LanguageMarker() {\n  return <div>one</div>;\n}\n\nexport const untouched = 9;\n",
            "export function LanguageMarker() {\n  return <div>one</div>;\n}",
            "LanguageMarker",
            "LanguageUpdated",
            1,
        ),
        "JavaScript" => symbol(
            "export function languageMarker() {\n  return 1;\n}\n\nexport const untouched = 9;\n",
            "export function languageMarker() {\n  return 1;\n}",
            "languageMarker",
            "languageUpdated",
            1,
        ),
        "Python" => symbol(
            "def language_marker():\n    return 1\n\ndef untouched():\n    return 9\n",
            "def language_marker():\n    return 1",
            "language_marker",
            "language_updated",
            1,
        ),
        "Go" => symbol(
            "package sample\n\nfunc LanguageMarker() int {\n\treturn 1\n}\n\nfunc Untouched() int { return 9 }\n",
            "func LanguageMarker() int {\n\treturn 1\n}",
            "LanguageMarker",
            "LanguageUpdated",
            3,
        ),
        "Java" => symbol(
            "public class LanguageMarker {\n    public int value() { return 1; }\n}\n\nclass Untouched {}\n",
            "public class LanguageMarker {\n    public int value() { return 1; }\n}",
            "LanguageMarker",
            "LanguageUpdated",
            1,
        ),
        "Scala" => symbol(
            "class LanguageMarker {\n  def value: Int = 1\n}\n\nclass Untouched\n",
            "class LanguageMarker {\n  def value: Int = 1\n}",
            "LanguageMarker",
            "LanguageUpdated",
            1,
        ),
        "C" => symbol(
            "int language_marker(void) {\n    return 1;\n}\n\nint untouched(void) { return 9; }\n",
            "int language_marker(void) {\n    return 1;\n}",
            "language_marker",
            "language_updated",
            1,
        ),
        "C++" => symbol(
            "int language_marker() {\n    return 1;\n}\n\nint untouched() { return 9; }\n",
            "int language_marker() {\n    return 1;\n}",
            "language_marker",
            "language_updated",
            1,
        ),
        "Ruby" => symbol(
            "def language_marker\n  1\nend\n\ndef untouched\n  9\nend\n",
            "def language_marker\n  1\nend",
            "language_marker",
            "language_updated",
            1,
        ),
        "PHP" => symbol(
            "<?php\nfunction language_marker(): int {\n    return 1;\n}\n\nfunction untouched(): int { return 9; }\n",
            "function language_marker(): int {\n    return 1;\n}",
            "language_marker",
            "language_updated",
            2,
        ),
        "Swift" => symbol(
            "func languageMarker() -> Int {\n    return 1\n}\n\nfunc untouched() -> Int { return 9 }\n",
            "func languageMarker() -> Int {\n    return 1\n}",
            "languageMarker",
            "languageUpdated",
            1,
        ),
        "Kotlin" => symbol(
            "fun languageMarker(): Int {\n    return 1\n}\n\nfun untouched(): Int = 9\n",
            "fun languageMarker(): Int {\n    return 1\n}",
            "languageMarker",
            "languageUpdated",
            1,
        ),
        "C#" => symbol(
            "public class LanguageMarker {\n    public int Value() { return 1; }\n}\n\npublic class Untouched {}\n",
            "public class LanguageMarker {\n    public int Value() { return 1; }\n}",
            "LanguageMarker",
            "LanguageUpdated",
            1,
        ),
        "Elixir" => symbol(
            "defmodule LanguageMarker do\n  def value, do: 1\nend\n\ndefmodule Untouched do\nend\n",
            "defmodule LanguageMarker do\n  def value, do: 1\nend",
            "LanguageMarker",
            "LanguageUpdated",
            1,
        ),
        "Bash" => symbol(
            "language_marker() {\n  echo one\n}\n\nuntouched() { echo nine; }\n",
            "language_marker() {\n  echo one\n}",
            "language_marker",
            "language_updated",
            1,
        ),
        "Docker" => literal(
            "FROM alpine:3.20\nRUN echo language-marker-old\n",
            "language-marker-old",
            "language-marker-new",
            2,
        ),
        "Make" => literal(
            "language-marker-old:\n\t@echo one\n\nuntouched:\n\t@echo nine\n",
            "language-marker-old",
            "language-marker-new",
            1,
        ),
        other => panic!("unknown fixture language: {other}"),
    }
}

const fn symbol(
    source: &'static str,
    core: &'static str,
    query: &'static str,
    replacement: &'static str,
    line: u64,
) -> Fixture {
    Fixture {
        source,
        core,
        query,
        replacement,
        line,
        kind: MatchKind::Symbol,
    }
}

const fn literal(
    source: &'static str,
    query: &'static str,
    replacement: &'static str,
    line: u64,
) -> Fixture {
    Fixture {
        source,
        core: "",
        query,
        replacement,
        line,
        kind: MatchKind::Literal,
    }
}
