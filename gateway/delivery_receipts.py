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
import stat
import threading
import time
from datetime import datetime, timezone
from typing import Any

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

RECEIPT_FILENAME = "platform-accepted-replies.jsonl"
_SCHEMA_VERSION = "hermes.platform_accepted_reply.v1"
_PROFILE_LOCKS = tuple(threading.Lock() for _ in range(64))
_RECEIPT_FILE_INDEX: dict[str, tuple[int, int, str, set[str]]] = {}
_RECEIPT_FILE_INDEX_LOCK = threading.Lock()
_RECEIPT_FILE_INDEX_CAP = 32
_LOCK_WAIT_SECONDS = 0.25
_LOCK_RETRY_SECONDS = 0.01


def _private_regular_file_info(fd):
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_nlink != 1
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
    ):
        return None
    return info


def _read_at(fd, offset: int, count: int) -> bytes:
    if hasattr(os, "pread"):
        return os.pread(fd, count, offset)
    current = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, count)
    finally:
        os.lseek(fd, current, os.SEEK_SET)


def _prefix_identity(fd, end: int) -> str:
    width = min(4096, end)
    first = _read_at(fd, 0, width)
    last = _read_at(fd, max(0, end - width), width)
    material = str(end).encode("ascii") + b"\0" + first + b"\0" + last
    return hashlib.sha256(material).hexdigest()


def _store_receipt_index(
    cache_key: str, inode: int, size: int, identity: str, pairs: set[str]
) -> None:
    with _RECEIPT_FILE_INDEX_LOCK:
        _RECEIPT_FILE_INDEX.pop(cache_key, None)
        _RECEIPT_FILE_INDEX[cache_key] = (inode, size, identity, pairs)
        while len(_RECEIPT_FILE_INDEX) > _RECEIPT_FILE_INDEX_CAP:
            _RECEIPT_FILE_INDEX.pop(next(iter(_RECEIPT_FILE_INDEX)))


def _existing_reply_pair_hashes(fd, cache_key: str) -> set[str] | None:
    """Incrementally index redacted hashes from one validated open file."""
    try:
        info = _private_regular_file_info(fd)
        if info is None:
            return None
        with _RECEIPT_FILE_INDEX_LOCK:
            cached = _RECEIPT_FILE_INDEX.get(cache_key)
        if (
            cached
            and cached[0] == info.st_ino
            and 0 <= cached[1] <= info.st_size
            and cached[2] == _prefix_identity(fd, cached[1])
        ):
            start = cached[1]
            pairs = cached[3]
            discard_partial_line = False
        else:
            # Cold starts must rebuild the exact durable index. A bounded tail
            # would allow an older pair to be replayed once it fell outside
            # the window. This streams the file once, then later calls read
            # only the appended tail through the cache above.
            start = 0
            pairs = set()
            discard_partial_line = False
        os.lseek(fd, start, os.SEEK_SET)
        with os.fdopen(os.dup(fd), "r", encoding="utf-8", errors="strict") as handle:
            if discard_partial_line:
                handle.readline()
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                value = record.get("reply_pair_hash") if isinstance(record, dict) else None
                if isinstance(value, str):
                    pairs.add(value)
        current = os.fstat(fd)
        _store_receipt_index(
            cache_key,
            current.st_ino,
            current.st_size,
            _prefix_identity(fd, current.st_size),
            pairs,
        )
        return pairs
    except (OSError, UnicodeError):
        return None


def _remember_appended_pair(
    fd, cache_key: str, pair_hash: str, existing_pairs: set[str]
) -> None:
    info = os.fstat(fd)
    existing_pairs.add(pair_hash)
    _store_receipt_index(
        cache_key,
        info.st_ino,
        info.st_size,
        _prefix_identity(fd, info.st_size),
        existing_pairs,
    )


def _open_private_lock(path):
    """Open a profile-local lock without following links or widening permissions."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    if _private_regular_file_info(fd) is None:
        os.close(fd)
        raise OSError("receipt lock is not a private regular file")
    return os.fdopen(fd, "a+", encoding="utf-8")


def _acquire_bounded_file_lock(handle, try_acquire) -> bool:
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while True:
        if try_acquire(handle):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_LOCK_RETRY_SECONDS, remaining))


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
        reply_pair_hash = _reply_pair_hash(
            platform=platform_text,
            chat_id=chat_text,
            inbound_message_id=inbound_text,
            outbound_message_id=outbound_text,
        )
        hermes_home = get_hermes_home()
        profile_key = os.path.normcase(os.path.realpath(os.fspath(hermes_home)))
        profile_lock = _PROFILE_LOCKS[hash(profile_key) % len(_PROFILE_LOCKS)]

        # Keep the check and append in one critical section.  Multiple async
        # gateway paths or restarted processes can finish the same reply. Only
        # the first durable append owns the pair; replayed timestamps must not
        # make old delivery evidence look fresh.
        with profile_lock:
            log_dir = hermes_home / "logs"
            log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = log_dir / RECEIPT_FILENAME
            lock_handle = _open_private_lock(log_dir / f".{RECEIPT_FILENAME}.lock")
            try:
                from gateway.status import _release_file_lock, _try_acquire_file_lock

                if not _acquire_bounded_file_lock(lock_handle, _try_acquire_file_lock):
                    return False
                flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
                flags |= getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(path, flags, 0o600)
                existing_pairs = _existing_reply_pair_hashes(fd, str(path))
                if existing_pairs is None:
                    os.close(fd)
                    return False
                if reply_pair_hash in existing_pairs:
                    os.close(fd)
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
                        "reply_pair_hash": reply_pair_hash,
                    }
                    payload = (
                        json.dumps(record, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    ).encode("utf-8")
                    # One O_APPEND write keeps each compact JSONL receipt contiguous.
                    previous_size = os.fstat(fd).st_size
                    written = os.write(fd, payload)
                    if written == len(payload):
                        _remember_appended_pair(
                            fd,
                            str(path),
                            reply_pair_hash,
                            existing_pairs,
                        )
                        return True
                    os.ftruncate(fd, previous_size)
                    return False
                finally:
                    os.close(fd)
            finally:
                try:
                    _release_file_lock(lock_handle)
                except (NameError, OSError):
                    pass
                lock_handle.close()
    except Exception as exc:  # observability must never break a successful send
        logger.warning("Platform-accepted reply receipt write failed: %s", exc)
        return False
