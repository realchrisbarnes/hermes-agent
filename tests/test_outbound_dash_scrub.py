"""Tests for outbound em/en dash scrubbing on the user-send path.

Operator hard rule: no em/en dashes anywhere in user-facing output.
These verify the scrub reads naturally, preserves emoji, and is a no-op
on clean text.
"""

from agent.message_sanitization import _scrub_outbound_dashes


def test_clause_em_dash_becomes_comma():
    src = "Immediate execution over 90-day planning — because the user said so."
    out = _scrub_outbound_dashes(src)
    assert "—" not in out
    assert "planning, because" in out


def test_numeric_range_en_dash_becomes_hyphen():
    assert _scrub_outbound_dashes("rows 10–20 affected") == "rows 10-20 affected"


def test_bare_em_dash_becomes_hyphen():
    assert _scrub_outbound_dashes("co—op") == "co-op"


def test_horizontal_bar_scrubbed():
    assert "―" not in _scrub_outbound_dashes("title ― subtitle")


def test_emoji_and_other_unicode_preserved():
    src = "Done ✅ — shipped 🚀 café résumé"
    out = _scrub_outbound_dashes(src)
    assert "—" not in out
    assert "✅" in out and "🚀" in out
    assert "café" in out and "résumé" in out


def test_no_dash_is_noop():
    src = "All good, nothing to change here 💅"
    assert _scrub_outbound_dashes(src) is src


def test_empty_and_none_safe():
    assert _scrub_outbound_dashes("") == ""
    assert _scrub_outbound_dashes(None) is None


def test_markdown_hyphen_rule_untouched():
    # '---' is hyphens, not an em dash; must not be altered.
    src = "above\n---\nbelow"
    assert _scrub_outbound_dashes(src) == src
