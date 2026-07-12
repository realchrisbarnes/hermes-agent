"""Trust contracts for proactive cron platform-acceptance receipts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cron import delivery_receipts
from cron.scheduler import _deliver_result, _delivery_message_id
from gateway.config import Platform
from gateway.platforms.base import SendResult


def _job(*, deliver: str = "origin") -> dict:
    return {
        "id": "raw-job-id",
        "name": "private job name",
        "deliver": deliver,
        "origin": {
            "platform": "telegram",
            "chat_id": "raw-chat-target",
            "thread_id": "raw-thread-target",
        },
    }


def _gateway_config():
    pconfig = MagicMock()
    pconfig.enabled = True
    config = MagicMock()
    config.platforms = {Platform.TELEGRAM: pconfig}
    return config


def _read_receipts(home) -> list[dict]:
    path = home / "logs" / delivery_receipts.RECEIPT_FILENAME
    return [json.loads(line) for line in path.read_text().splitlines()]


def _run_coro_on_test_loop(coro, _loop):
    future = Future()
    try:
        future.set_result(asyncio.run(coro))
    except BaseException as exc:  # pragma: no cover - assertion surfaces it
        future.set_exception(exc)
    return future


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

    assert delivery_receipts.record_platform_accepted_cron_delivery(
        platform="telegram",
        job_id="raw-job-id",
        chat_id="raw-chat-target",
        thread_id="raw-thread-target",
        outbound_message_id="raw-outbound-id",
        send_succeeded=True,
    )

    path = tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(writes) == 1

    raw_receipt = path.read_text()
    for private_value in (
        "raw-job-id",
        "raw-chat-target",
        "raw-thread-target",
        "raw-outbound-id",
        "private job name",
        "body must never reach the receipt",
    ):
        assert private_value not in raw_receipt

    [record] = _read_receipts(tmp_path)
    assert record["schema_version"] == "hermes.platform_accepted_cron_delivery.v1"
    assert record["receipt_type"] == "platform_accepted_proactive_cron_text_delivery"
    assert record["delivery_state"] == "PLATFORM_ACCEPTED"
    assert record["human_received_or_read_proven"] is False
    assert record["accepted_component"] == "text"
    assert record["attachments_proven"] is False
    assert record["platform"] == "telegram"
    for field in (
        "job_id_hash",
        "target_hash",
        "outbound_message_id_hash",
        "delivery_hash",
    ):
        assert record[field].startswith("sha256:")
        assert len(record[field]) == len("sha256:") + 64


def _record_fixture_receipt():
    return delivery_receipts.record_platform_accepted_cron_delivery(
        platform="telegram",
        job_id="job",
        chat_id="target",
        thread_id=None,
        outbound_message_id="outbound",
        send_succeeded=True,
    )


def test_receipt_rejects_symlinked_log_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    (tmp_path / "logs").symlink_to(redirected, target_is_directory=True)

    assert _record_fixture_receipt() is False
    assert not (redirected / delivery_receipts.RECEIPT_FILENAME).exists()


def test_receipt_rejects_fifo_without_blocking(tmp_path, monkeypatch):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO unavailable on this platform")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    log_dir = tmp_path / "logs"
    log_dir.mkdir(mode=0o700)
    target = log_dir / delivery_receipts.RECEIPT_FILENAME
    os.mkfifo(target, mode=0o600)

    assert _record_fixture_receipt() is False
    assert stat.S_ISFIFO(target.lstat().st_mode)


def test_receipt_rejects_multi_link_regular_target(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    log_dir = tmp_path / "logs"
    log_dir.mkdir(mode=0o700)
    original = log_dir / "existing-receipt-data"
    original.write_text("preserve\n")
    original.chmod(0o600)
    target = log_dir / delivery_receipts.RECEIPT_FILENAME
    os.link(original, target)

    assert _record_fixture_receipt() is False
    assert original.read_text() == "preserve\n"
    assert target.stat().st_nlink == 2


@pytest.mark.parametrize(
    ("send_succeeded", "platform", "message_id"),
    [
        (False, "telegram", "outbound"),
        ("true", "telegram", "outbound"),
        (1, "telegram", "outbound"),
        (True, "telegram", ""),
        (True, "local", "outbound"),
    ],
)
def test_receipt_requires_platform_success_and_message_id(
    tmp_path, monkeypatch, send_succeeded, platform, message_id
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert not delivery_receipts.record_platform_accepted_cron_delivery(
        platform=platform,
        job_id="job",
        chat_id="target",
        thread_id=None,
        outbound_message_id=message_id,
        send_succeeded=send_succeeded,
    )
    assert not (tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME).exists()


@pytest.mark.parametrize("success", [1, "true", None])
def test_object_receipt_extraction_requires_literal_boolean_success(success):
    result = SendResult(success=success, message_id="raw-outbound-id")
    assert _delivery_message_id(result) is None


def test_standalone_confirmed_delivery_records_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch(
             "tools.send_message_tool._send_to_platform",
             new=AsyncMock(
                 return_value={"success": True, "message_id": "raw-outbound-id"}
             ),
         ):
        result = _deliver_result(_job(), "private delivery body")

    assert result is None
    [record] = _read_receipts(tmp_path)
    assert record["delivery_state"] == "PLATFORM_ACCEPTED"
    assert "private delivery body" not in json.dumps(record)


@pytest.mark.parametrize(
    "send_result",
    [
        {"success": False, "error": "rejected"},
        {"success": True},
        {"message_id": "raw-outbound-id"},
        {"success": None, "message_id": "raw-outbound-id"},
        {"success": "true", "message_id": "raw-outbound-id"},
    ],
)
def test_failed_or_missing_id_standalone_delivery_records_no_receipt(
    tmp_path, monkeypatch, send_result
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch(
             "tools.send_message_tool._send_to_platform",
             new=AsyncMock(return_value=send_result),
         ):
        delivery_result = _deliver_result(_job(), "private delivery body")

    assert not (tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME).exists()
    if send_result.get("success") is True:
        assert delivery_result is None
    else:
        assert delivery_result is not None
        assert "unconfirmed" in delivery_result or "rejected" in delivery_result


def test_standalone_thread_fallback_is_partial_and_uses_actual_flat_route(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime_adapter = MagicMock()

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch(
             "cron.scheduler.load_config",
             return_value={
                 "cron": {"wrap_response": False, "mirror_delivery": True}
             },
         ), \
         patch(
             "tools.send_message_tool._send_to_platform",
             new=AsyncMock(
                 return_value={
                     "success": True,
                     "message_id": "raw-outbound-id",
                     "delivery_thread_id": None,
                     "text_delivery_thread_id": None,
                     "text_delivery_thread_ids": [None],
                     "media_delivery_thread_id": None,
                     "media_delivery_thread_ids": [],
                     "thread_fallback": True,
                     "text_thread_fallback": True,
                     "media_thread_fallback": False,
                 }
             ),
         ), \
         patch(
             "cron.scheduler._seed_cron_channel_session", return_value=True
         ) as seed_flat, \
         patch("cron.scheduler._maybe_mirror_cron_delivery") as mirror:
        result = _deliver_result(
            _job(),
            "private delivery body",
            adapters={Platform.TELEGRAM: runtime_adapter},
            loop=None,
        )

    assert result is not None
    assert "partial delivery" in result
    assert "flat route" in result
    assert seed_flat.call_args.kwargs["user_id"] is None
    assert seed_flat.call_args.args[3] == "raw-chat-target"
    assert mirror.call_args.kwargs["thread_id"] is None
    assert mirror.call_args.kwargs["enabled"] is False

    [record] = _read_receipts(tmp_path)
    assert record["target_hash"] == delivery_receipts._composite_hash(
        "target", "telegram", "raw-chat-target", ""
    )
    assert record["target_hash"] != delivery_receipts._composite_hash(
        "target", "telegram", "raw-chat-target", "raw-thread-target"
    )


def test_mixed_standalone_media_fallback_mirrors_text_route(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    media_root = tmp_path / "media-cache"
    media_path = media_root / "proof.png"
    media_root.mkdir()
    media_path.write_bytes(b"fixture")
    monkeypatch.setattr(
        "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS",
        (media_root,),
    )
    runtime_adapter = MagicMock()

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch(
             "cron.scheduler.load_config",
             return_value={
                 "cron": {"wrap_response": False, "mirror_delivery": True}
             },
         ), \
         patch(
             "tools.send_message_tool._send_to_platform",
             new=AsyncMock(
                 return_value={
                     "success": True,
                     "message_id": "raw-media-id",
                     "delivery_thread_id": "raw-thread-target",
                     "text_delivery_thread_id": "raw-thread-target",
                     "text_delivery_thread_ids": ["raw-thread-target"],
                     "media_delivery_thread_id": None,
                     "media_delivery_thread_ids": [None],
                     "thread_fallback": True,
                     "text_thread_fallback": False,
                     "media_thread_fallback": True,
                 }
             ),
         ), \
         patch("cron.scheduler._seed_cron_channel_session") as seed_flat, \
         patch("cron.scheduler._maybe_mirror_cron_delivery") as mirror:
        result = _deliver_result(
            _job(),
            f"private text\nMEDIA:{media_path}",
            adapters={Platform.TELEGRAM: runtime_adapter},
            loop=None,
        )

    assert result is not None
    assert "media components" in result
    assert "text route was preserved" in result
    seed_flat.assert_not_called()
    assert mirror.call_args.kwargs["thread_id"] == "raw-thread-target"
    assert mirror.call_args.kwargs["enabled"] is True
    assert not (tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME).exists()


def test_standalone_heterogeneous_text_routes_suppress_route_proof_and_mirror(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime_adapter = MagicMock()

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch(
             "cron.scheduler.load_config",
             return_value={
                 "cron": {"wrap_response": False, "mirror_delivery": True}
             },
         ), \
         patch(
             "tools.send_message_tool._send_to_platform",
             new=AsyncMock(
                 return_value={
                     "success": True,
                     "message_id": "second-message",
                     "delivery_thread_id": None,
                     "text_delivery_thread_id": None,
                     "text_delivery_thread_ids": ["raw-thread-target", None],
                     "text_delivery_route_ambiguous": True,
                     "media_delivery_thread_id": None,
                     "media_delivery_thread_ids": [],
                     "thread_fallback": True,
                     "text_thread_fallback": True,
                     "media_thread_fallback": False,
                 }
             ),
         ), \
         patch("cron.scheduler._record_cron_delivery_receipt") as receipt, \
         patch("cron.scheduler._seed_cron_thread_session") as seed_thread, \
         patch("cron.scheduler._seed_cron_channel_session") as seed_flat, \
         patch("cron.scheduler._maybe_mirror_cron_delivery") as mirror:
        result = _deliver_result(
            _job(),
            "two chunk private body",
            adapters={Platform.TELEGRAM: runtime_adapter},
            loop=None,
        )

    assert result is not None
    assert "heterogeneous effective routes" in result
    receipt.assert_not_called()
    seed_thread.assert_not_called()
    seed_flat.assert_not_called()
    assert mirror.call_args.kwargs["enabled"] is False
    assert not (tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME).exists()


@pytest.mark.parametrize("has_text", [False, True], ids=["media-only", "mixed"])
def test_standalone_media_warning_is_partial_not_clean_success(
    tmp_path, monkeypatch, has_text
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    media_root = tmp_path / "media-cache"
    media_path = media_root / "proof.png"
    media_root.mkdir()
    media_path.write_bytes(b"fixture")
    monkeypatch.setattr(
        "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS",
        (media_root,),
    )
    content = f"MEDIA:{media_path}"
    if has_text:
        content = f"private text\n{content}"

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch(
             "tools.send_message_tool._send_to_platform",
             new=AsyncMock(
                 return_value={
                     "success": True,
                     "message_id": "raw-outbound-id",
                     "warnings": ["Some media files were skipped"],
                 }
             ),
         ):
        result = _deliver_result(_job(), content)

    assert result is not None
    assert "partial delivery" in result
    assert "media delivery warning" in result
    assert not (tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME).exists()


def test_standalone_text_only_warning_is_not_misclassified_as_media_loss(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch(
             "tools.send_message_tool._send_to_platform",
             new=AsyncMock(
                 return_value={
                     "success": True,
                     "message_id": "raw-outbound-id",
                     "warnings": ["non-delivery advisory"],
                 }
             ),
         ):
        result = _deliver_result(_job(), "private text")

    assert result is None
    assert len(_read_receipts(tmp_path)) == 1


def test_media_only_standalone_delivery_is_explicitly_outside_text_receipt_scope(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    media_path = tmp_path / "proof.png"
    media_path.write_bytes(b"fixture")

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch(
             "tools.send_message_tool._send_to_platform",
             new=AsyncMock(
                 return_value={"success": True, "message_id": "raw-media-id"}
             ),
         ):
        assert _deliver_result(_job(), f"MEDIA:{media_path}") is None

    assert not (tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME).exists()


def test_mixed_text_and_media_delivery_is_outside_text_only_receipt_scope(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    media_path = tmp_path / "proof.png"
    media_path.write_bytes(b"fixture")

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch(
             "tools.send_message_tool._send_to_platform",
             new=AsyncMock(
                 return_value={"success": True, "message_id": "raw-media-id"}
             ),
         ):
        assert _deliver_result(
            _job(), f"private text\nMEDIA:{media_path}"
        ) is None

    assert not (tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME).exists()


def test_local_delivery_records_no_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with patch.object(
        delivery_receipts, "record_platform_accepted_cron_delivery"
    ) as record:
        assert _deliver_result(_job(deliver="local"), "local body") is None

    record.assert_not_called()


def test_live_adapter_confirmed_delivery_records_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = AsyncMock()
    adapter.send.return_value = SendResult(
        success=True,
        message_id="raw-live-outbound-id",
    )
    loop = MagicMock()
    loop.is_running.return_value = True

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=_run_coro_on_test_loop):
        result = _deliver_result(
            _job(),
            "private delivery body",
            adapters={Platform.TELEGRAM: adapter},
            loop=loop,
        )

    assert result is None
    assert len(_read_receipts(tmp_path)) == 1


def test_live_adapter_filtered_no_send_is_not_reported_as_delivery_success(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = _gateway_config()
    config.filter_silence_narration = True
    adapter = AsyncMock()
    loop = MagicMock()
    loop.is_running.return_value = True
    standalone_send = AsyncMock()

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=_run_coro_on_test_loop), \
         patch("tools.send_message_tool._send_to_platform", new=standalone_send):
        result = _deliver_result(
            _job(),
            ".",
            adapters={Platform.TELEGRAM: adapter},
            loop=loop,
        )

    assert result is not None
    assert "filtered" in result.lower()
    adapter.send.assert_not_awaited()
    standalone_send.assert_not_awaited()
    assert not (tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME).exists()


@pytest.mark.parametrize("has_text", [False, True], ids=["media-only", "mixed"])
@pytest.mark.parametrize(
    "media_result",
    [
        None,
        object(),
        SendResult(success=1, message_id="raw-media-id"),
        SendResult(success=False, error="media rejected"),
    ],
    ids=["none", "missing-success", "non-boolean-success", "explicit-failure"],
)
def test_live_media_requires_literal_success_and_propagates_failure(
    tmp_path, monkeypatch, caplog, has_text, media_result
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    media_root = tmp_path / "media-cache"
    media_path = media_root / "proof.png"
    media_root.mkdir()
    media_path.write_bytes(b"fixture")
    monkeypatch.setattr(
        "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS",
        (media_root,),
    )

    config = _gateway_config()
    config.filter_silence_narration = False
    adapter = AsyncMock()
    adapter.send.return_value = SendResult(
        success=True,
        message_id="raw-text-id",
    )
    adapter.send_image_file.return_value = media_result
    loop = MagicMock()
    loop.is_running.return_value = True
    standalone_send = AsyncMock()
    content = f"MEDIA:{media_path}"
    if has_text:
        content = f"private text\n{content}"

    with caplog.at_level(logging.INFO, logger="cron.scheduler"), \
         patch("gateway.config.load_gateway_config", return_value=config), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=_run_coro_on_test_loop), \
         patch("tools.send_message_tool._send_to_platform", new=standalone_send):
        result = _deliver_result(
            _job(),
            content,
            adapters={Platform.TELEGRAM: adapter},
            loop=loop,
        )

    assert result is not None
    assert "media" in result.lower()
    standalone_send.assert_not_awaited()
    assert not any(
        "delivered to telegram:raw-chat-target via live adapter" in record.message
        for record in caplog.records
    )
    if has_text:
        assert any("partial delivery" in record.message for record in caplog.records)
    assert not (tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME).exists()


@pytest.mark.parametrize("has_text", [False, True], ids=["media-only", "mixed"])
def test_live_media_literal_success_is_confirmed_without_text_receipt(
    tmp_path, monkeypatch, has_text
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    media_root = tmp_path / "media-cache"
    media_path = media_root / "proof.png"
    media_root.mkdir()
    media_path.write_bytes(b"fixture")
    monkeypatch.setattr(
        "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS",
        (media_root,),
    )

    config = _gateway_config()
    config.filter_silence_narration = False
    adapter = AsyncMock()
    adapter.send.return_value = SendResult(
        success=True,
        message_id="raw-text-id",
    )
    adapter.send_image_file.return_value = SendResult(
        success=True,
        message_id="raw-media-id",
    )
    loop = MagicMock()
    loop.is_running.return_value = True
    content = f"MEDIA:{media_path}"
    if has_text:
        content = f"private text\n{content}"

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=_run_coro_on_test_loop):
        result = _deliver_result(
            _job(),
            content,
            adapters={Platform.TELEGRAM: adapter},
            loop=loop,
        )

    assert result is None
    adapter.send_image_file.assert_awaited_once()
    assert not (tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME).exists()


@pytest.mark.parametrize(
    (
        "chat_id",
        "configured_thread_id",
        "adapter_requested_thread_id",
        "expected_receipt_thread_id",
        "outbound_message_id",
    ),
    [
        ("-1001234567890", "1", None, "", "general-message"),
        ("226252250", "reports", "7072", "7072", "named-topic-message"),
        ("226252250", "7072", None, "7072", "direct-topic-message"),
    ],
    ids=["general-normalized-flat", "named-topic-resolved", "direct-topic-preserved"],
)
def test_live_telegram_receipt_hashes_effective_accepted_route(
    tmp_path,
    monkeypatch,
    chat_id,
    configured_thread_id,
    adapter_requested_thread_id,
    expected_receipt_thread_id,
    outbound_message_id,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = _gateway_config()
    config.filter_silence_narration = False
    adapter = AsyncMock()
    adapter.ensure_dm_topic.return_value = "7072"
    adapter.send.return_value = SendResult(
        success=True,
        message_id=outbound_message_id,
        raw_response={
            "message_ids": [outbound_message_id],
            "requested_thread_id": adapter_requested_thread_id,
            "thread_fallback": False,
        },
    )
    loop = MagicMock()
    loop.is_running.return_value = True
    job = _job()
    job["origin"]["chat_id"] = chat_id
    job["origin"]["thread_id"] = configured_thread_id

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=_run_coro_on_test_loop):
        result = _deliver_result(
            job,
            "private delivery body",
            adapters={Platform.TELEGRAM: adapter},
            loop=loop,
        )

    assert result is None
    [record] = _read_receipts(tmp_path)
    assert record["target_hash"] == delivery_receipts._composite_hash(
        "target", "telegram", chat_id, expected_receipt_thread_id
    )


def test_live_telegram_thread_fallback_seeds_and_mirrors_only_flat_route(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = _gateway_config()
    config.filter_silence_narration = False
    adapter = AsyncMock()
    adapter.send.return_value = SendResult(
        success=True,
        message_id="raw-live-id",
        raw_response={
            "message_ids": ["raw-live-id"],
            "requested_thread_id": "opened-topic",
            "thread_fallback": True,
        },
    )
    loop = MagicMock()
    loop.is_running.return_value = True
    job = _job()
    job["origin"]["thread_id"] = None

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch(
             "cron.scheduler.load_config",
             return_value={
                 "cron": {"wrap_response": False, "mirror_delivery": True}
             },
         ), \
         patch(
             "asyncio.run_coroutine_threadsafe",
             side_effect=_run_coro_on_test_loop,
         ), \
         patch(
             "cron.scheduler._open_continuable_cron_thread",
             return_value="opened-topic",
         ), \
         patch("cron.scheduler._seed_cron_thread_session") as seed_thread, \
         patch(
             "cron.scheduler._seed_cron_channel_session", return_value=True
         ) as seed_flat, \
         patch("cron.scheduler._maybe_mirror_cron_delivery") as mirror:
        result = _deliver_result(
            job,
            "private delivery body",
            adapters={Platform.TELEGRAM: adapter},
            loop=loop,
        )

    assert result is not None
    assert "delivered without thread_id" in result
    seed_thread.assert_not_called()
    assert seed_flat.call_args.args[3] == "raw-chat-target"
    assert mirror.call_args.kwargs["thread_id"] is None
    assert mirror.call_args.kwargs["enabled"] is False
    [record] = _read_receipts(tmp_path)
    assert record["target_hash"] == delivery_receipts._composite_hash(
        "target", "telegram", "raw-chat-target", ""
    )


def test_live_multichunk_fallback_without_route_evidence_suppresses_route_proof(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = _gateway_config()
    config.filter_silence_narration = False
    adapter = AsyncMock()
    adapter.send.return_value = SendResult(
        success=True,
        message_id="first-live-id",
        raw_response={
            "message_ids": ["first-live-id", "second-live-id"],
            "requested_thread_id": "raw-thread-target",
            "thread_fallback": True,
        },
    )
    loop = MagicMock()
    loop.is_running.return_value = True

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch(
             "cron.scheduler.load_config",
             return_value={
                 "cron": {"wrap_response": False, "mirror_delivery": True}
             },
         ), \
         patch(
             "asyncio.run_coroutine_threadsafe",
             side_effect=_run_coro_on_test_loop,
         ), \
         patch("cron.scheduler._record_cron_delivery_receipt") as receipt, \
         patch("cron.scheduler._seed_cron_thread_session") as seed_thread, \
         patch("cron.scheduler._seed_cron_channel_session") as seed_flat, \
         patch("cron.scheduler._maybe_mirror_cron_delivery") as mirror:
        result = _deliver_result(
            _job(),
            "two chunk private body",
            adapters={Platform.TELEGRAM: adapter},
            loop=loop,
        )

    assert result is not None
    assert "route" in result
    assert "ambiguous across 2 message chunk(s)" in result
    receipt.assert_not_called()
    seed_thread.assert_not_called()
    seed_flat.assert_not_called()
    assert mirror.call_args.kwargs["enabled"] is False
    assert not (tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME).exists()


def test_mixed_live_filtered_text_with_confirmed_media_is_partial_failure(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    media_root = tmp_path / "media-cache"
    media_path = media_root / "proof.png"
    media_root.mkdir()
    media_path.write_bytes(b"fixture")
    monkeypatch.setattr(
        "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS",
        (media_root,),
    )

    config = _gateway_config()
    config.filter_silence_narration = True
    adapter = AsyncMock()
    adapter.send_image_file.return_value = SendResult(
        success=True,
        message_id="raw-media-id",
    )
    loop = MagicMock()
    loop.is_running.return_value = True
    standalone_send = AsyncMock()

    with caplog.at_level(logging.INFO, logger="cron.scheduler"), \
         patch("gateway.config.load_gateway_config", return_value=config), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=_run_coro_on_test_loop), \
         patch("tools.send_message_tool._send_to_platform", new=standalone_send):
        result = _deliver_result(
            _job(),
            f".\nMEDIA:{media_path}",
            adapters={Platform.TELEGRAM: adapter},
            loop=loop,
        )

    assert result is not None
    assert "filtered" in result.lower()
    adapter.send.assert_not_awaited()
    adapter.send_image_file.assert_awaited_once()
    standalone_send.assert_not_awaited()
    assert any("partial delivery" in record.message for record in caplog.records)
    assert not any(
        "delivered to telegram:raw-chat-target via live adapter" in record.message
        for record in caplog.records
    )
    assert not (tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME).exists()


def test_live_media_with_no_safe_deliverable_component_fails_closed(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    unsafe_media_path = tmp_path / "missing.png"
    config = _gateway_config()
    config.filter_silence_narration = False
    adapter = AsyncMock()
    loop = MagicMock()
    loop.is_running.return_value = True
    standalone_send = AsyncMock()

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=_run_coro_on_test_loop), \
         patch("tools.send_message_tool._send_to_platform", new=standalone_send):
        result = _deliver_result(
            _job(),
            f"MEDIA:{unsafe_media_path}",
            adapters={Platform.TELEGRAM: adapter},
            loop=loop,
        )

    assert result is not None
    assert "no deliverable text or media" in result.lower()
    adapter.send.assert_not_awaited()
    adapter.send_image_file.assert_not_awaited()
    standalone_send.assert_not_awaited()
    assert not (tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME).exists()


def test_mixed_live_unsafe_media_is_reported_as_partial_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    unsafe_media_path = tmp_path / "missing.png"
    config = _gateway_config()
    config.filter_silence_narration = False
    adapter = AsyncMock()
    adapter.send.return_value = SendResult(
        success=True,
        message_id="raw-text-id",
    )
    loop = MagicMock()
    loop.is_running.return_value = True
    standalone_send = AsyncMock()

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=_run_coro_on_test_loop), \
         patch("tools.send_message_tool._send_to_platform", new=standalone_send):
        result = _deliver_result(
            _job(),
            f"private text\nMEDIA:{unsafe_media_path}",
            adapters={Platform.TELEGRAM: adapter},
            loop=loop,
        )

    assert result is not None
    assert "media attachment" in result.lower()
    assert "safe-path validation" in result.lower()
    adapter.send.assert_awaited_once()
    adapter.send_image_file.assert_not_awaited()
    standalone_send.assert_not_awaited()
    assert not (tmp_path / "logs" / delivery_receipts.RECEIPT_FILENAME).exists()


def test_receipt_failure_does_not_replace_successful_delivery_result(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch(
             "tools.send_message_tool._send_to_platform",
             new=AsyncMock(
                 return_value={"success": True, "message_id": "raw-outbound-id"}
             ),
         ), \
         patch.object(
             delivery_receipts,
             "record_platform_accepted_cron_delivery",
             side_effect=OSError("synthetic receipt failure"),
         ):
        result = _deliver_result(_job(), "private delivery body")

    assert result is None
