"""Tests for Telegram inline keyboard approval buttons."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Telegram mock so TelegramAdapter can be imported
# ---------------------------------------------------------------------------
def _ensure_telegram_mock():
    """Wire up the minimal mocks required to import TelegramAdapter."""
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    # Provide real exception classes so ``except (NetworkError, ...)`` in
    # connect() doesn't blow up under xdist when this mock leaks.
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.platforms.telegram import TelegramAdapter
from gateway.config import Platform, PlatformConfig


def _make_adapter(extra=None):
    """Create a TelegramAdapter with mocked internals."""
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


class _AuthRunner:
    """Minimal runner shim for callback auth tests."""

    def __init__(self, authorized: bool):
        self.authorized = authorized
        self.last_source = None

    async def _handle_message(self, event):
        return None

    def _is_user_authorized(self, source):
        self.last_source = source
        return self.authorized


# ===========================================================================
# Operational issue notifications
# ===========================================================================

class TestTelegramOperationalNotifications:
    """Test Telegram operational email notifications stay best-effort."""

    @pytest.mark.asyncio
    async def test_new_allowed_chat_member_sends_email(self):
        adapter = _make_adapter()
        adapter._send_operational_issue_email = MagicMock(return_value=True)

        user = SimpleNamespace(id=111, username="mark", first_name="Mark")
        chat = SimpleNamespace(id=-100, title="Chris, Bella and Mark", type="group")
        message = SimpleNamespace(
            chat=chat,
            chat_id=-100,
            message_thread_id=None,
            new_chat_members=[user],
        )
        update = SimpleNamespace(message=message)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "111"}, clear=False):
            await adapter._handle_new_chat_members(update, MagicMock())

        adapter._send_operational_issue_email.assert_called_once()
        subject, body = adapter._send_operational_issue_email.call_args[0]
        assert subject == "Telegram allowed user added"
        assert "User ID: 111" in body
        assert "No secrets" in body

    @pytest.mark.asyncio
    async def test_new_blocked_chat_member_does_not_email(self):
        adapter = _make_adapter()
        adapter._send_operational_issue_email = MagicMock(return_value=True)

        user = SimpleNamespace(id=222, username="mallory", first_name="Mallory")
        chat = SimpleNamespace(id=-100, title="Group", type="group")
        message = SimpleNamespace(chat=chat, chat_id=-100, new_chat_members=[user])
        update = SimpleNamespace(message=message)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "111"}, clear=False):
            await adapter._handle_new_chat_members(update, MagicMock())

        adapter._send_operational_issue_email.assert_not_called()

    def test_bella_button_failure_sends_issue_email(self):
        adapter = _make_adapter()
        adapter._send_operational_issue_email = MagicMock(return_value=True)

        failed = SimpleNamespace(returncode=1, stdout="", stderr="boom")
        with patch("subprocess.run", return_value=failed):
            confirmation, had_issue = adapter._process_bella_button_callback("bld_ok:missing")

        assert had_issue is True
        assert "Could not process Bella button callback" in confirmation
        adapter._send_operational_issue_email.assert_called_once()
        subject, body = adapter._send_operational_issue_email.call_args[0]
        assert subject == "Telegram approval/build-fix button issue"
        assert "bld_ok:missing" in body
        assert "No API keys" in body

    def test_issue_email_prefers_google_oauth_script(self, tmp_path):
        adapter = _make_adapter()
        script = tmp_path / "google_api.py"
        script.write_text("# test google api script\n")
        completed = SimpleNamespace(returncode=0, stdout='{"status":"sent"}', stderr="")

        with patch.dict(
            os.environ,
            {
                "HERMES_GOOGLE_WORKSPACE_SCRIPT": str(script),
                "TELEGRAM_ISSUE_EMAIL_TO": "ops@example.com",
            },
            clear=False,
        ), patch("subprocess.run", return_value=completed) as run:
            assert adapter._send_operational_issue_email("Subject", "Body") is True

        cmd = run.call_args[0][0]
        assert cmd[1] == str(script)
        assert cmd[2:4] == ["gmail", "send"]
        assert "--to" in cmd
        assert cmd[cmd.index("--to") + 1] == "ops@example.com"
        assert "--subject" in cmd
        assert cmd[cmd.index("--subject") + 1] == "Subject"
        assert "--body" in cmd
        assert cmd[cmd.index("--body") + 1] == "Body"

    def test_issue_email_falls_back_to_smtp_when_oauth_fails(self):
        adapter = _make_adapter()
        adapter._send_operational_issue_email_via_google_oauth = MagicMock(return_value=False)
        adapter._send_operational_issue_email_via_smtp = MagicMock(return_value=True)

        assert adapter._send_operational_issue_email("Subject", "Body") is True
        adapter._send_operational_issue_email_via_google_oauth.assert_called_once_with("Subject", "Body")
        adapter._send_operational_issue_email_via_smtp.assert_called_once_with("Subject", "Body")


# ===========================================================================
# send_exec_approval — inline keyboard buttons
# ===========================================================================

class TestTelegramExecApproval:
    """Test the send_exec_approval method sends InlineKeyboard buttons."""

    @pytest.mark.asyncio
    async def test_sends_inline_keyboard(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        result = await adapter.send_exec_approval(
            chat_id="12345",
            command="rm -rf /important",
            session_key="agent:main:telegram:group:12345:99",
            description="dangerous deletion",
        )

        assert result.success is True
        assert result.message_id == "42"

        adapter._bot.send_message.assert_called_once()
        kwargs = adapter._bot.send_message.call_args[1]
        assert kwargs["chat_id"] == 12345
        assert "rm -rf /important" in kwargs["text"]
        assert "dangerous deletion" in kwargs["text"]
        assert kwargs["reply_markup"] is not None  # InlineKeyboardMarkup

    @pytest.mark.asyncio
    async def test_stores_approval_state(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        await adapter.send_exec_approval(
            chat_id="12345",
            command="echo test",
            session_key="my-session-key",
        )

        # The approval_id should map to the session_key
        assert len(adapter._approval_state) == 1
        approval_id = list(adapter._approval_state.keys())[0]
        assert adapter._approval_state[approval_id] == "my-session-key"

    @pytest.mark.asyncio
    async def test_sends_in_thread(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        await adapter.send_exec_approval(
            chat_id="12345",
            command="ls",
            session_key="s",
            metadata={"thread_id": "999"},
        )

        kwargs = adapter._bot.send_message.call_args[1]
        assert kwargs.get("message_thread_id") == 999

    @pytest.mark.asyncio
    async def test_retries_without_thread_when_thread_not_found(self):
        adapter = _make_adapter()
        call_log = []

        class FakeBadRequest(Exception):
            pass

        async def mock_send_message(**kwargs):
            call_log.append(dict(kwargs))
            if kwargs.get("message_thread_id") is not None:
                raise FakeBadRequest("Message thread not found")
            return SimpleNamespace(message_id=42)

        adapter._bot.send_message = AsyncMock(side_effect=mock_send_message)

        result = await adapter.send_exec_approval(
            chat_id="12345",
            command="ls",
            session_key="s",
            metadata={"thread_id": "999"},
        )

        assert result.success is True
        assert len(call_log) == 2
        assert call_log[0]["message_thread_id"] == 999
        assert "message_thread_id" not in call_log[1] or call_log[1]["message_thread_id"] is None

    @pytest.mark.asyncio
    async def test_not_connected(self):
        adapter = _make_adapter()
        adapter._bot = None
        result = await adapter.send_exec_approval(
            chat_id="12345", command="ls", session_key="s"
        )
        assert result.success is False

    @pytest.mark.asyncio
    async def test_disable_link_previews_sets_preview_kwargs(self):
        adapter = _make_adapter(extra={"disable_link_previews": True})
        mock_msg = MagicMock()
        mock_msg.message_id = 42
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        await adapter.send_exec_approval(
            chat_id="12345", command="ls", session_key="s"
        )

        kwargs = adapter._bot.send_message.call_args[1]
        assert (
            kwargs.get("disable_web_page_preview") is True
            or kwargs.get("link_preview_options") is not None
        )

    @pytest.mark.asyncio
    async def test_send_update_prompt_escapes_dynamic_prompt(self):
        adapter = _make_adapter()
        sent = {}

        async def mock_send_message(**kwargs):
            sent.update(kwargs)
            return SimpleNamespace(message_id=55)

        adapter._bot.send_message = AsyncMock(side_effect=mock_send_message)

        result = await adapter.send_update_prompt(
            chat_id="12345",
            prompt="Fix [issue]_1 and verify *markdown*",
            default="alpha_beta",
            metadata={"thread_id": "999"},
        )

        assert result.success is True
        assert "MARKDOWN_V2" in repr(sent["parse_mode"])
        assert "Fix \\[issue\\]\\_1" in sent["text"]
        assert "alpha\\_beta" in sent["text"]

    @pytest.mark.asyncio
    async def test_truncates_long_command(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 1
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        long_cmd = "x" * 5000
        await adapter.send_exec_approval(
            chat_id="12345", command=long_cmd, session_key="s"
        )

        kwargs = adapter._bot.send_message.call_args[1]
        assert "..." in kwargs["text"]
        assert len(kwargs["text"]) < 5000
# _handle_callback_query — approval button clicks
# ===========================================================================

class TestTelegramApprovalCallback:
    """Test the approval callback handling in _handle_callback_query."""

    @pytest.mark.asyncio
    async def test_resolves_approval_on_click(self):
        adapter = _make_adapter()
        # Set up approval state
        adapter._approval_state[1] = "agent:main:telegram:group:12345:99"

        # Mock callback query
        query = AsyncMock()
        query.data = "ea:once:1"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.first_name = "Norbert"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()
        query.from_user.id = "12345"

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
                await adapter._handle_callback_query(update, context)

        mock_resolve.assert_called_once_with("agent:main:telegram:group:12345:99", "once")
        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once()

        # State should be cleaned up
        assert 1 not in adapter._approval_state

    @pytest.mark.asyncio
    async def test_resume_typing_after_inline_approval(self):
        """Clicking an inline approval button must un-pause the chat's typing.

        Regression for #27853: the text /approve path resumed typing, but the
        ea: callback path did not, so the typing indicator stayed gone for the
        rest of a long-running turn after a button click.
        """
        adapter = _make_adapter()
        adapter._approval_state[5] = "agent:main:telegram:group:12345:99"
        adapter.pause_typing_for_chat("12345")
        assert "12345" in adapter._typing_paused

        query = AsyncMock()
        query.data = "ea:once:5"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.first_name = "Norbert"
        query.from_user.id = "12345"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch("tools.approval.resolve_gateway_approval", return_value=1):
                await adapter._handle_callback_query(update, context)

        assert "12345" not in adapter._typing_paused

    @pytest.mark.asyncio
    async def test_typing_stays_paused_when_resolve_returns_zero(self):
        """If resolve_gateway_approval reports 0 resolves, the agent thread
        was never unblocked, so typing should NOT be force-resumed."""
        adapter = _make_adapter()
        adapter._approval_state[6] = "agent:main:telegram:group:12345:99"
        adapter.pause_typing_for_chat("12345")

        query = AsyncMock()
        query.data = "ea:once:6"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.first_name = "Norbert"
        query.from_user.id = "12345"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch("tools.approval.resolve_gateway_approval", return_value=0):
                await adapter._handle_callback_query(update, context)

        assert "12345" in adapter._typing_paused

    @pytest.mark.asyncio
    async def test_approval_callback_escapes_dynamic_user_name(self):
        adapter = _make_adapter()
        adapter._approval_state[3] = "agent:main:telegram:group:12345:99"

        query = AsyncMock()
        query.data = "ea:once:3"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.first_name = "Alice_Bob"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()
        query.from_user.id = "12345"

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch("tools.approval.resolve_gateway_approval", return_value=1):
                await adapter._handle_callback_query(update, context)

        edit_kwargs = query.edit_message_text.call_args[1]
        assert "MARKDOWN_V2" in repr(edit_kwargs["parse_mode"])
        assert "Alice\\_Bob" in edit_kwargs["text"]
        assert "Approved once" in edit_kwargs["text"]

    @pytest.mark.asyncio
    async def test_deny_button(self):
        adapter = _make_adapter()
        adapter._approval_state[2] = "some-session"

        query = AsyncMock()
        query.data = "ea:deny:2"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.first_name = "Alice"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()
        query.from_user.id = "12345"

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
                await adapter._handle_callback_query(update, context)

        mock_resolve.assert_called_once_with("some-session", "deny")
        edit_kwargs = query.edit_message_text.call_args[1]
        assert "Denied" in edit_kwargs["text"]

    @pytest.mark.asyncio
    async def test_approval_callback_rejects_user_blocked_by_global_allowlist(self):
        adapter = _make_adapter()
        adapter._approval_state[7] = "agent:main:telegram:group:12345:99"
        runner = _AuthRunner(authorized=False)
        adapter._message_handler = runner._handle_message

        query = AsyncMock()
        query.data = "ea:once:7"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.chat.type = "private"
        query.from_user = MagicMock()
        query.from_user.id = 222
        query.from_user.first_name = "Mallory"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
            await adapter._handle_callback_query(update, context)

        mock_resolve.assert_not_called()
        query.answer.assert_called_once()
        assert "not authorized" in query.answer.call_args[1]["text"].lower()
        query.edit_message_text.assert_not_called()
        assert adapter._approval_state[7] == "agent:main:telegram:group:12345:99"
        assert runner.last_source is not None
        assert runner.last_source.platform == Platform.TELEGRAM
        assert runner.last_source.user_id == "222"
        assert runner.last_source.chat_id == "12345"

    @pytest.mark.asyncio
    async def test_already_resolved(self):
        adapter = _make_adapter()
        # No state for approval_id 99 — already resolved

        query = AsyncMock()
        query.data = "ea:once:99"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.first_name = "Bob"
        query.answer = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()
        query.from_user.id = "12345"

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
                await adapter._handle_callback_query(update, context)

        # Should NOT resolve — already handled
        mock_resolve.assert_not_called()
        # Should still ack with "already resolved" message
        query.answer.assert_called_once()
        assert "already been resolved" in query.answer.call_args[1]["text"]

    @pytest.mark.asyncio
    async def test_model_picker_callback_not_affected(self):
        """Ensure model picker callbacks still route correctly."""
        adapter = _make_adapter()

        query = AsyncMock()
        query.data = "mp:some_provider"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        # Model picker callback should be handled (not crash)
        # We just verify it doesn't try to resolve an approval
        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
            with patch.object(adapter, "_handle_model_picker_callback", new_callable=AsyncMock):
                await adapter._handle_callback_query(update, context)

        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_prompt_callback_not_affected(self, tmp_path):
        """Ensure update prompt callbacks still work."""
        adapter = _make_adapter()

        query = AsyncMock()
        query.data = "update_prompt:y"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.id = 123
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
            with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
                # Allow the caller — the new fail-closed allowlist gate
                # (#24457) rejects empty TELEGRAM_ALLOWED_USERS, but this
                # test isn't exercising that gate; it's verifying the
                # update_prompt callback still writes the response.
                with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}):
                    await adapter._handle_callback_query(update, context)

        # Should NOT have triggered approval resolution
        mock_resolve.assert_not_called()
        assert (tmp_path / ".update_response").read_text() == "y"

    @pytest.mark.asyncio
    async def test_update_prompt_callback_rejects_unauthorized_user(self, tmp_path):
        """Update prompt buttons should honor TELEGRAM_ALLOWED_USERS."""
        adapter = _make_adapter()

        query = AsyncMock()
        query.data = "update_prompt:y"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.id = 222
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
            with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "111"}):
                await adapter._handle_callback_query(update, context)

        query.answer.assert_called_once()
        assert "not authorized" in query.answer.call_args[1]["text"].lower()
        query.edit_message_text.assert_not_called()
        assert not (tmp_path / ".update_response").exists()

    @pytest.mark.asyncio
    async def test_update_prompt_callback_rejects_user_blocked_by_global_allowlist(self, tmp_path):
        adapter = _make_adapter()
        runner = _AuthRunner(authorized=False)
        adapter._message_handler = runner._handle_message

        query = AsyncMock()
        query.data = "update_prompt:y"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.chat.type = "private"
        query.from_user = MagicMock()
        query.from_user.id = 222
        query.from_user.first_name = "Mallory"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
            with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": ""}):
                await adapter._handle_callback_query(update, context)

        query.answer.assert_called_once()
        assert "not authorized" in query.answer.call_args[1]["text"].lower()
        query.edit_message_text.assert_not_called()
        assert not (tmp_path / ".update_response").exists()
        assert runner.last_source is not None
        assert runner.last_source.platform == Platform.TELEGRAM
        assert runner.last_source.user_id == "222"

    @pytest.mark.asyncio
    async def test_update_prompt_callback_allows_authorized_user(self, tmp_path):
        """Allowed Telegram users can still answer update prompt buttons."""
        adapter = _make_adapter()

        query = AsyncMock()
        query.data = "update_prompt:n"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.id = 111
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
            with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "111"}):
                await adapter._handle_callback_query(update, context)

        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once()
        assert (tmp_path / ".update_response").read_text() == "n"
