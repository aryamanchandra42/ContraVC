"""Unit tests for the name match trust heuristic in IntelligenceBrief."""

from __future__ import annotations

import pytest

from contra.intelligence.brief import _is_match_untrusted


@pytest.mark.parametrize("input_name,matched_name,method,expected", [
    # Exact match — always trusted
    ("Will Bricker", "Will Bricker", "exact", False),
    # Alias with surname mismatch — the canonical Will Bricker → Will Au problem
    ("Will Bricker", "Will Au", "alias", True),
    # Alias with surname match — same person, alias resolved correctly
    ("Will Bricker", "William Bricker", "alias", False),
    # Fuzzy with partial last-name match
    ("HarbourVest Partners", "HarbourVest Partners Ltd", "fuzzy", False),
    # Fuzzy with completely different surname
    ("Alex Johnson", "Alex Jones", "fuzzy", True),
    # Fuzzy with no match at all
    ("Sequoia Capital", "Tiger Global", "fuzzy", True),
    # Fuzzy — same company, abbreviated
    ("Sequoia Capital", "Sequoia Capital Partners", "fuzzy", False),
    # method=none → always trusted
    ("Foo Bar", "Baz Qux", "none", False),
    # Empty names → trusted
    ("", "", "alias", False),
    ("Will Bricker", "", "alias", False),
    # Single-token name — can't detect surname mismatch safely
    ("Will", "Au", "alias", False),
])
def test_is_match_untrusted(input_name, matched_name, method, expected):
    result = _is_match_untrusted(input_name, matched_name, method)
    assert result == expected, (
        f"_is_match_untrusted({input_name!r}, {matched_name!r}, {method!r}) "
        f"returned {result}, expected {expected}"
    )
