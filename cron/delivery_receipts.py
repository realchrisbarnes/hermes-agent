"""Privacy-safe platform-acceptance receipts for proactive cron text delivery.

These receipts are intentionally separate from gateway reply receipts.  A
cron receipt proves only that a platform accepted the text component of a
text-only proactive scheduled-job delivery; media-only and mixed text/media
payloads are explicitly outside this schema.  It does not prove human receipt,
reading, or response.  Receipt writing is best-effort and must never change the
underlying delivery result.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

RECEIPT_FILENAME = "platform-accepted-cron-deliveries.jsonl"
_SCHEMA_VERSION = "hermes.platform_accepted_cron_delivery.v1"
_WRITTEN_DELIVERIES: set[tuple[str, str]] = set()
_WRITTEN_DELIVERIES_LOCK = threading.Lock()


def _require_owned_path(st: os.stat_result, *, kind: str) -> None:
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise OSError(f"{kind} is not owned by the current user")


def _validate_log_directory(fd: int) -> None:
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError("cron receipt log path is not a directory")
    _require_owned_path(st, kind="cron receipt log directory")
    if stat.S_IMODE(st.st_mode) & 0o022:
        raise OSError("cron receipt log directory is group/world writable")
    os.fchmod(fd, 0o700)


def _validate_receipt_file(fd: int, expected: os.stat_result | None) -> None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise OSError("cron receipt target is not a regular file")
    if st.st_nlink != 1:
        raise OSError("cron receipt target has multiple hard links")
    _require_owned_path(st, kind="cron receipt target")
    if expected is not None and (
        st.st_dev != expected.st_dev or st.st_ino != expected.st_ino
    ):
        raise OSError("cron receipt target changed during secure open")
    os.fchmod(fd, 0o600)


def _secure_dir_fd_available() -> bool:
    return bool(
        getattr(os, "O_DIRECTORY", 0)
        and getattr(os, "O_NOFOLLOW", 0)
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
    )


@contextmanager
def _open_secure_receipt_file(hermes_home):
    """Open the receipt target without following directory or file links.

    The final file is opened nonblocking so a FIFO swap can never hang the cron
    worker. Platforms without the required dir-fd primitives fail closed; a
    missing observability receipt must not weaken path safety.
    """
    if not _secure_dir_fd_available():
        raise OSError("secure dir-fd receipt append is unavailable")

    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    home_fd = os.open(os.fspath(hermes_home), directory_flags)
    log_fd = None
    receipt_fd = None
    try:
        try:
            os.mkdir("logs", mode=0o700, dir_fd=home_fd)
        except FileExistsError:
            pass
        log_fd = os.open("logs", directory_flags, dir_fd=home_fd)
        _validate_log_directory(log_fd)

        file_flags = (
            os.O_WRONLY
            | os.O_APPEND
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        for _attempt in range(2):
            expected = None
            try:
                expected = os.stat(
                    RECEIPT_FILENAME,
                    dir_fd=log_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(expected.st_mode):
                    raise OSError("cron receipt target is not a regular file")
                if expected.st_nlink != 1:
                    raise OSError("cron receipt target has multiple hard links")
                _require_owned_path(expected, kind="cron receipt target")
            except FileNotFoundError:
                expected = None

            try:
                if expected is None:
                    receipt_fd = os.open(
                        RECEIPT_FILENAME,
                        file_flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=log_fd,
                    )
                else:
                    receipt_fd = os.open(
                        RECEIPT_FILENAME,
                        file_flags,
                        dir_fd=log_fd,
                    )
            except (FileExistsError, FileNotFoundError):
                continue

            try:
                _validate_receipt_file(receipt_fd, expected)
            except Exception:
                os.close(receipt_fd)
                receipt_fd = None
                raise
            break

        if receipt_fd is None:
            raise OSError("cron receipt target changed repeatedly during open")
        yield receipt_fd
    finally:
        if receipt_fd is not None:
            os.close(receipt_fd)
        if log_fd is not None:
            os.close(log_fd)
        os.close(home_fd)


def _composite_hash(label: str, *parts: str) -> str:
    """Hash a schema- and purpose-separated composite identifier."""
    material = "\0".join((_SCHEMA_VERSION, label, *parts)).encode("utf-8")
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


def record_platform_accepted_cron_delivery(
    *,
    platform: Any,
    job_id: Any,
    chat_id: Any,
    thread_id: Any,
    outbound_message_id: Any,
    send_succeeded: bool,
) -> bool:
    """Append one redacted proactive-cron text acceptance receipt.

    Returns ``True`` only when a complete receipt was appended.  Missing
    identifiers, local/failed delivery, serialization errors, and filesystem
    errors return ``False`` without raising into the cron delivery path.
    """
    if send_succeeded is not True:
        return False

    platform_value = getattr(platform, "value", platform)
    platform_text = str(platform_value or "").strip().lower()
    job_text = str(job_id or "").strip()
    chat_text = str(chat_id or "").strip()
    thread_text = str(thread_id or "").strip()
    outbound_text = str(outbound_message_id or "").strip()
    if platform_text == "local" or not all(
        (platform_text, job_text, chat_text, outbound_text)
    ):
        return False

    try:
        job_id_hash = _composite_hash("job", job_text)
        target_hash = _composite_hash(
            "target", platform_text, chat_text, thread_text
        )
        outbound_message_id_hash = _composite_hash(
            "outbound_message",
            platform_text,
            chat_text,
            thread_text,
            outbound_text,
        )
        delivery_hash = _composite_hash(
            "cron_delivery",
            job_text,
            platform_text,
            chat_text,
            thread_text,
            outbound_text,
        )
        hermes_home = get_hermes_home()
        profile_key = os.path.normcase(os.path.realpath(os.fspath(hermes_home)))
        dedup_key = (profile_key, delivery_hash)

        # Keep duplicate detection and append in one critical section.  Manual
        # cron runs and a gateway ticker can complete on different threads.
        with _WRITTEN_DELIVERIES_LOCK:
            if dedup_key in _WRITTEN_DELIVERIES:
                return False

            record = {
                "schema_version": _SCHEMA_VERSION,
                "recorded_at": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
                "receipt_type": "platform_accepted_proactive_cron_text_delivery",
                "delivery_state": "PLATFORM_ACCEPTED",
                "human_received_or_read_proven": False,
                "accepted_component": "text",
                "attachments_proven": False,
                "platform": platform_text,
                "job_id_hash": job_id_hash,
                "target_hash": target_hash,
                "outbound_message_id_hash": outbound_message_id_hash,
                "delivery_hash": delivery_hash,
            }
            payload = (
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")

            with _open_secure_receipt_file(hermes_home) as fd:
                # One O_APPEND write keeps each compact JSONL record contiguous
                # when multiple scheduled deliveries complete concurrently.
                written = os.write(fd, payload)
                if written == len(payload):
                    _WRITTEN_DELIVERIES.add(dedup_key)
                    return True
                return False
    except Exception as exc:  # observability must never break delivery
        logger.warning(
            "Platform-accepted proactive cron receipt write failed: %s", exc
        )
        return False
