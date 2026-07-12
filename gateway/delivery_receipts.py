"""Privacy-safe receipts for replies accepted by a messaging platform.

These receipts prove only that an outbound reply was accepted by the platform
adapter for a specific inbound message.  They do not prove human receipt,
reading, or response.  The append path is deliberately best-effort so local
observability can never turn a successful user-visible send into a failure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

RECEIPT_FILENAME = "platform-accepted-replies.jsonl"
_SCHEMA_VERSION = "hermes.platform_accepted_reply.v1"


def _composite_id_hash(
    *,
    platform: str,
    chat_id: str,
    direction: str,
    message_id: str,
) -> str:
    """Hash a domain-separated composite identifier, never a raw ID alone."""
    material = "\0".join(
        (_SCHEMA_VERSION, platform, chat_id, direction, message_id)
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


def _reply_pair_hash(
    *,
    platform: str,
    chat_id: str,
    inbound_message_id: str,
    outbound_message_id: str,
) -> str:
    material = "\0".join(
        (
            _SCHEMA_VERSION,
            platform,
            chat_id,
            "reply_pair",
            inbound_message_id,
            outbound_message_id,
        )
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


def record_platform_accepted_reply(
    *,
    platform: Any,
    chat_id: Any,
    inbound_message_id: Any,
    outbound_message_id: Any,
    send_succeeded: bool,
) -> bool:
    """Append one redacted platform-accepted reply receipt.

    Returns ``True`` only when a complete success receipt was appended.  Any
    missing identifier, failed send, serialization error, or filesystem error
    returns ``False`` without raising into the message delivery path.
    """
    if not send_succeeded:
        return False

    platform_value = getattr(platform, "value", platform)
    platform_text = str(platform_value or "").strip().lower()
    chat_text = str(chat_id or "").strip()
    inbound_text = str(inbound_message_id or "").strip()
    outbound_text = str(outbound_message_id or "").strip()
    if not all((platform_text, chat_text, inbound_text, outbound_text)):
        return False

    try:
        record = {
            "schema_version": _SCHEMA_VERSION,
            "recorded_at": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "receipt_type": "platform_accepted_reply",
            "delivery_state": "PLATFORM_ACCEPTED",
            "human_received_or_read_proven": False,
            "platform": platform_text,
            "inbound_message_id_hash": _composite_id_hash(
                platform=platform_text,
                chat_id=chat_text,
                direction="inbound",
                message_id=inbound_text,
            ),
            "outbound_message_id_hash": _composite_id_hash(
                platform=platform_text,
                chat_id=chat_text,
                direction="outbound",
                message_id=outbound_text,
            ),
            "reply_pair_hash": _reply_pair_hash(
                platform=platform_text,
                chat_id=chat_text,
                inbound_message_id=inbound_text,
                outbound_message_id=outbound_text,
            ),
        }
        payload = (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")

        log_dir = get_hermes_home() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = log_dir / RECEIPT_FILENAME
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            # One O_APPEND write keeps each compact JSONL receipt contiguous
            # when multiple platform sessions complete concurrently.
            written = os.write(fd, payload)
            return written == len(payload)
        finally:
            os.close(fd)
    except Exception as exc:  # observability must never break a successful send
        logger.warning("Platform-accepted reply receipt write failed: %s", exc)
        return False
