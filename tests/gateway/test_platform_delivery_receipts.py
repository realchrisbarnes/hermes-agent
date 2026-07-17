"""Contracts for privacy-safe platform-accepted reply receipts."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gateway import delivery_receipts
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    SendResult,
)
from gateway.session import SessionSource, build_session_key
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


class _ReplyAdapter(BasePlatformAdapter):
    def __init__(self, result: SendResult | None = None):
        super().__init__(
            PlatformConfig(enabled=True, token="fixture-token"),
            Platform.TELEGRAM,
        )
        self.result = result or SendResult(success=True, message_id="outbound-raw")
        self.sent: list[dict] = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return self.result

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


async def _hold_typing(_chat_id, interval=2.0, metadata=None, stop_event=None):
    if stop_event is not None:
        await stop_event.wait()
    else:
        await asyncio.Event().wait()


def _event() -> MessageEvent:
    return MessageEvent(
        text="inbound body must not be logged",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat-raw",
            chat_type="dm",
        ),
        message_id="inbound-raw",
    )


def _read_receipts(home) -> list[dict]:
    path = home / "logs" / delivery_receipts.RECEIPT_FILENAME
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_receipt_is_redacted_profile_scoped_0600_and_single_append_write(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    real_write = os.write
    writes: list[bytes] = []

    def tracking_write(fd, payload):
        writes.append(payload)
        return real_write(fd, payload)

    monkeypatch.setattr(delivery_receipts.os, "write", tracking_write)

    assert delivery_receipts.record_platform_accepted_reply(
        platform=Platform.TELEGRAM,
        chat_id="chat-raw",
        inbound_message_id="inbound-raw",
        outbound_message_id="outbound-raw",
        send_succeeded=True,
    )

    path = tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(writes) == 1

    raw_receipt = path.read_text()
    for private_value in (
        "chat-raw",
        "inbound-raw",
        "outbound-raw",
        "inbound body must not be logged",
    ):
        assert private_value not in raw_receipt

    [record] = _read_receipts(tmp_path)
    assert record["schema_version"] == "hermes.platform_accepted_reply.v1"
    assert record["receipt_type"] == "platform_accepted_reply"
    assert record["delivery_state"] == "PLATFORM_ACCEPTED"
    assert record["human_received_or_read_proven"] is False
    assert record["platform"] == "telegram"
    for field in (
        "inbound_message_id_hash",
        "outbound_message_id_hash",
        "reply_pair_hash",
    ):
        assert record[field].startswith("sha256:")
        assert len(record[field]) == len("sha256:") + 64


def test_duplicate_reply_pair_is_written_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kwargs = {
        "platform": "telegram",
        "chat_id": "same-chat",
        "inbound_message_id": "same-inbound",
        "outbound_message_id": "same-outbound",
        "send_succeeded": True,
    }

    assert delivery_receipts.record_platform_accepted_reply(**kwargs) is True
    assert delivery_receipts.record_platform_accepted_reply(**kwargs) is False
    assert len(_read_receipts(tmp_path)) == 1


def test_duplicate_reply_pair_stays_deduplicated_after_process_restart(
    tmp_path, monkeypatch
):
    """The durable receipt itself must prevent replay after memory is lost."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kwargs = {
        "platform": "telegram",
        "chat_id": "same-chat",
        "inbound_message_id": "same-inbound",
        "outbound_message_id": "same-outbound",
        "send_succeeded": True,
    }

    assert delivery_receipts.record_platform_accepted_reply(**kwargs) is True
    delivery_receipts._RECEIPT_FILE_INDEX.clear()

    script = """
from gateway.delivery_receipts import record_platform_accepted_reply
print(record_platform_accepted_reply(
    platform='telegram', chat_id='same-chat',
    inbound_message_id='same-inbound', outbound_message_id='same-outbound',
    send_succeeded=True,
))
"""
    env = {**os.environ, "HERMES_HOME": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )

    assert result.stdout.strip() == "False"
    assert len(_read_receipts(tmp_path)) == 1


def test_cold_index_finds_pair_older_than_one_megabyte(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kwargs = {
        "platform": "telegram",
        "chat_id": "old-chat",
        "inbound_message_id": "old-inbound",
        "outbound_message_id": "old-outbound",
        "send_succeeded": True,
    }
    assert delivery_receipts.record_platform_accepted_reply(**kwargs)
    receipt_path = tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME
    with receipt_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"padding": "x" * (1024 * 1024 + 1)}) + "\n")
    delivery_receipts._RECEIPT_FILE_INDEX.clear()

    assert delivery_receipts.record_platform_accepted_reply(**kwargs) is False
    assert len(receipt_path.read_text(encoding="utf-8").splitlines()) == 2


def test_receipt_index_reads_only_new_tail_after_initial_hydration(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first = {
        "platform": "telegram",
        "chat_id": "tail-chat",
        "inbound_message_id": "tail-inbound-1",
        "outbound_message_id": "tail-outbound-1",
        "send_succeeded": True,
    }
    second = {
        **first,
        "inbound_message_id": "tail-inbound-2",
        "outbound_message_id": "tail-outbound-2",
    }
    assert delivery_receipts.record_platform_accepted_reply(**first)
    receipt_path = tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME
    original = receipt_path.read_text(encoding="utf-8").splitlines()[0]
    with receipt_path.open("a", encoding="utf-8") as handle:
        handle.write(original + "\n")
    loads = []
    real_loads = delivery_receipts.json.loads
    monkeypatch.setattr(
        delivery_receipts.json,
        "loads",
        lambda value: (loads.append(value), real_loads(value))[1],
    )

    assert delivery_receipts.record_platform_accepted_reply(**second)
    assert len(loads) == 1
    assert len(_read_receipts(tmp_path)) == 3


def test_receipt_index_invalidates_same_inode_rewrite(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kwargs = {
        "platform": "telegram",
        "chat_id": "rewrite-chat",
        "inbound_message_id": "rewrite-inbound",
        "outbound_message_id": "rewrite-outbound",
        "send_succeeded": True,
    }
    assert delivery_receipts.record_platform_accepted_reply(**kwargs)
    receipt_path = tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME
    original = json.loads(receipt_path.read_text(encoding="utf-8"))
    original["reply_pair_hash"] = "sha256:" + ("f" * 64)
    receipt_path.write_text(
        json.dumps(original, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    assert delivery_receipts.record_platform_accepted_reply(**kwargs)
    assert len(_read_receipts(tmp_path)) == 2


def test_cache_eviction_during_append_preserves_complete_pair_index(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first = {
        "platform": "telegram",
        "chat_id": "eviction-chat",
        "inbound_message_id": "eviction-inbound-1",
        "outbound_message_id": "eviction-outbound-1",
        "send_succeeded": True,
    }
    second = {
        **first,
        "inbound_message_id": "eviction-inbound-2",
        "outbound_message_id": "eviction-outbound-2",
    }
    assert delivery_receipts.record_platform_accepted_reply(**first)
    real_write = os.write

    def write_after_eviction(fd, payload):
        written = real_write(fd, payload)
        for index in range(delivery_receipts._RECEIPT_FILE_INDEX_CAP + 1):
            delivery_receipts._store_receipt_index(
                f"dummy-{index}", index, 0, "identity", set()
            )
        return written

    monkeypatch.setattr(delivery_receipts.os, "write", write_after_eviction)

    assert delivery_receipts.record_platform_accepted_reply(**second)
    assert delivery_receipts.record_platform_accepted_reply(**first) is False
    assert len(_read_receipts(tmp_path)) == 2


def test_short_append_is_rolled_back(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    real_write = os.write

    def short_write(fd, payload):
        partial = payload[: max(1, len(payload) // 2)]
        return real_write(fd, partial)

    monkeypatch.setattr(delivery_receipts.os, "write", short_write)

    assert not delivery_receipts.record_platform_accepted_reply(
        platform="telegram",
        chat_id="short-chat",
        inbound_message_id="short-inbound",
        outbound_message_id="short-outbound",
        send_succeeded=True,
    )
    path = tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME
    assert path.read_bytes() == b""


def test_receipt_lock_contention_is_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway import status as gateway_status

    attempts = iter((False, True))
    monkeypatch.setattr(
        gateway_status, "_try_acquire_file_lock", lambda _handle: next(attempts)
    )
    monkeypatch.setattr(delivery_receipts.time, "sleep", lambda _seconds: None)

    assert delivery_receipts.record_platform_accepted_reply(
        platform="telegram",
        chat_id="contention-chat",
        inbound_message_id="contention-inbound",
        outbound_message_id="contention-outbound",
        send_succeeded=True,
    )
    assert len(_read_receipts(tmp_path)) == 1


def test_receipt_lock_contention_times_out_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway import status as gateway_status

    moments = iter((0.0, 1.0))
    monkeypatch.setattr(gateway_status, "_try_acquire_file_lock", lambda _handle: False)
    monkeypatch.setattr(delivery_receipts.time, "monotonic", lambda: next(moments))

    assert not delivery_receipts.record_platform_accepted_reply(
        platform="telegram",
        chat_id="timeout-chat",
        inbound_message_id="timeout-inbound",
        outbound_message_id="timeout-outbound",
        send_succeeded=True,
    )
    assert not (tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME).exists()


def test_receipt_file_rejects_insecure_mode_symlink_and_hardlink(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / delivery_receipts.RECEIPT_FILENAME
    kwargs = {
        "platform": "telegram",
        "chat_id": "private-chat",
        "inbound_message_id": "private-inbound",
        "outbound_message_id": "private-outbound",
        "send_succeeded": True,
    }

    path.write_text("")
    path.chmod(0o644)
    assert delivery_receipts.record_platform_accepted_reply(**kwargs) is False

    path.unlink()
    target = tmp_path / "target.jsonl"
    target.write_text("")
    target.chmod(0o600)
    path.symlink_to(target)
    assert delivery_receipts.record_platform_accepted_reply(**kwargs) is False

    path.unlink()
    os.link(target, path)
    assert delivery_receipts.record_platform_accepted_reply(**kwargs) is False


def test_receipt_lock_rejects_insecure_mode_symlink_and_hardlink(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    logs = tmp_path / "logs"
    logs.mkdir()
    lock = logs / f".{delivery_receipts.RECEIPT_FILENAME}.lock"
    target = tmp_path / "lock-target"
    target.write_text("")
    target.chmod(0o600)

    def attempt(suffix):
        return delivery_receipts.record_platform_accepted_reply(
            platform="telegram",
            chat_id=f"lock-chat-{suffix}",
            inbound_message_id=f"lock-inbound-{suffix}",
            outbound_message_id=f"lock-outbound-{suffix}",
            send_succeeded=True,
        )

    lock.write_text("")
    lock.chmod(0o644)
    assert attempt("mode") is False

    lock.unlink()
    lock.symlink_to(target)
    assert attempt("symlink") is False

    lock.unlink()
    os.link(target, lock)
    assert attempt("hardlink") is False


def test_duplicate_guard_is_thread_safe_and_profile_scoped(tmp_path, monkeypatch):
    first_home = tmp_path / "first"
    second_home = tmp_path / "second"
    monkeypatch.setenv("HERMES_HOME", str(first_home))
    kwargs = {
        "platform": "telegram",
        "chat_id": "concurrent-chat",
        "inbound_message_id": "concurrent-inbound",
        "outbound_message_id": "concurrent-outbound",
        "send_succeeded": True,
    }

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(
            pool.map(
                lambda _index: delivery_receipts.record_platform_accepted_reply(
                    **kwargs
                ),
                range(16),
            )
        )

    assert outcomes.count(True) == 1
    assert len(_read_receipts(first_home)) == 1

    monkeypatch.setenv("HERMES_HOME", str(second_home))
    assert delivery_receipts.record_platform_accepted_reply(**kwargs) is True
    assert len(_read_receipts(second_home)) == 1


@pytest.mark.parametrize(
    ("send_succeeded", "inbound_id", "outbound_id"),
    [
        (False, "inbound", "outbound"),
        (True, "", "outbound"),
        (True, "inbound", ""),
    ],
)
def test_receipt_requires_success_and_both_message_ids(
    tmp_path, monkeypatch, send_succeeded, inbound_id, outbound_id
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert not delivery_receipts.record_platform_accepted_reply(
        platform="telegram",
        chat_id="chat",
        inbound_message_id=inbound_id,
        outbound_message_id=outbound_id,
        send_succeeded=send_succeeded,
    )
    assert not (tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME).exists()


@pytest.mark.asyncio
async def test_non_streamed_reply_records_platform_acceptance(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _ReplyAdapter()
    adapter._keep_typing = _hold_typing

    async def handler(_event):
        return "reply body must not be logged"

    adapter.set_message_handler(handler)
    event = _event()
    await adapter._process_message_background(event, build_session_key(event.source))

    assert len(adapter.sent) == 1
    [record] = _read_receipts(tmp_path)
    assert record["delivery_state"] == "PLATFORM_ACCEPTED"
    assert "reply body must not be logged" not in json.dumps(record)


@pytest.mark.asyncio
async def test_streamed_first_send_records_platform_acceptance(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _ReplyAdapter()
    consumer = GatewayStreamConsumer(
        adapter,
        "chat-raw",
        StreamConsumerConfig(cursor=""),
        initial_reply_to_id="inbound-raw",
    )

    assert await consumer._send_or_edit("streamed reply", finalize=True)

    [record] = _read_receipts(tmp_path)
    assert record["receipt_type"] == "platform_accepted_reply"
    assert record["platform"] == "telegram"


@pytest.mark.asyncio
async def test_receipt_failure_cannot_fail_a_successful_send(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _ReplyAdapter()
    adapter._keep_typing = _hold_typing

    async def handler(_event):
        return "reply still succeeds"

    def fail_receipt(**_kwargs):
        raise OSError("synthetic receipt failure")

    adapter.set_message_handler(handler)
    monkeypatch.setattr(
        delivery_receipts,
        "record_platform_accepted_reply",
        fail_receipt,
    )
    event = _event()

    await adapter._process_message_background(event, build_session_key(event.source))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["content"] == "reply still succeeds"
