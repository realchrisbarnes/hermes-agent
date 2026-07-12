"""Contracts for privacy-safe platform-accepted reply receipts."""

from __future__ import annotations

import asyncio
import json
import os
import stat

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
