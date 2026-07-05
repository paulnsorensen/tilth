"""Tests for benchmark/tasks/base.py required_matches — grader alternation.

Ports the assertions from check_stats.py:check_alternation into real pytest
cases and extends them to cover the empty-alternate contract in the docstring:
a leading/trailing/doubled "|" never becomes an unconditional pass, and an
all-empty required string never matches.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tasks.base import required_matches


def test_plain_substring_hit():
    assert required_matches("ServeHTTP", "servehttp dispatches") is True


def test_plain_substring_miss():
    assert required_matches("absent", "nope") is False


def test_alternation_hit():
    assert required_matches("foo|bar", "only bar here") is True


def test_alternation_miss():
    assert required_matches("foo|bar", "only baz here") is False


def test_alternates_are_whitespace_stripped():
    assert required_matches("foo | bar", "only bar here") is True


def test_leading_pipe_is_not_unconditional_pass():
    assert required_matches("|bar", "only baz here") is False
    assert required_matches("|bar", "only bar here") is True


def test_trailing_pipe_is_not_unconditional_pass():
    assert required_matches("bar|", "only baz here") is False
    assert required_matches("bar|", "only bar here") is True


def test_doubled_pipe_is_not_unconditional_pass():
    assert required_matches("foo||bar", "only baz here") is False
    assert required_matches("foo||bar", "only foo here") is True


def test_all_empty_required_never_matches():
    assert required_matches("", "any text at all") is False
    assert required_matches("||", "any text at all") is False
    assert required_matches("  |  ", "any text at all") is False
