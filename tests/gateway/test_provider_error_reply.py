"""_gateway_provider_error_reply must not mislabel rate-limit/quota as auth failure.

Regression test for the 2026-07-02 incident: a gpt-5.5 quota exhaustion (429)
surfaced to Telegram users as "Provider authentication failed" because the
run_agent resolution except-branch hardcoded the auth wording for every
exception. The branch now routes through _gateway_provider_error_reply; these
tests pin the mapping for the real error strings involved.
"""

from gateway.run import _gateway_provider_error_reply


QUOTA_429 = (
    "Codex provider quota exhausted (429); retry after 6243s. "
    "Credentials are still valid."
)


def test_quota_429_maps_to_rate_limit_reply():
    reply = _gateway_provider_error_reply(QUOTA_429)
    assert "rate-limiting" in reply
    assert "authentication" not in reply.lower()


def test_generic_rate_limited_maps_to_rate_limit_reply():
    reply = _gateway_provider_error_reply("Primary provider rate-limited after retries")
    assert "rate-limiting" in reply


def test_genuine_auth_error_still_maps_to_auth_reply():
    reply = _gateway_provider_error_reply(
        "Incorrect API key provided: sk-abc***. You can find your API key at ..."
    )
    assert "authentication failed" in reply.lower()


def test_unknown_error_maps_to_generic_reply_without_raw_details():
    raw = "SomethingWeirdError: socket exploded at 0x7f"
    reply = _gateway_provider_error_reply(raw)
    assert "socket exploded" not in reply
    assert "gateway logs" in reply
