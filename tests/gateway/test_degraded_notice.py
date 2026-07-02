"""Degraded-fallback honesty banner (2026-07-02 incident regression).

When the primary chat provider is rate-limited and the local fallback answers,
the reply must carry a short notice, or the persona drop reads as a
malfunction (operator literally asked "why are you speaking differently?").
"""

from gateway.run import (
    _DEGRADED_FALLBACK,
    _degraded_fallback_notice,
    _note_fallback_active,
    _note_primary_healthy,
)

INCIDENT_EXC = Exception(
    "Codex provider quota exhausted (429); retry after 435s. "
    "Credentials are still valid."
)


def test_fallback_active_produces_notice_with_minutes():
    _note_fallback_active(INCIDENT_EXC)
    assert _DEGRADED_FALLBACK["retry_seconds"] == 435
    notice = _degraded_fallback_notice()
    assert "backup" in notice
    assert "min" in notice
    assert notice.endswith("\n\n")


def test_primary_healthy_clears_notice():
    _note_fallback_active(INCIDENT_EXC)
    _note_primary_healthy()
    assert _degraded_fallback_notice() == ""


def test_unparseable_retry_falls_back_to_default_window():
    _note_fallback_active(Exception("some auth failure with no retry hint"))
    assert _DEGRADED_FALLBACK["retry_seconds"] == 900
    assert _degraded_fallback_notice() != ""
    _note_primary_healthy()


def test_notice_never_lies_when_inactive():
    _note_primary_healthy()
    assert _degraded_fallback_notice() == ""
