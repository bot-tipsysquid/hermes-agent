"""Tests for Telegram model picker thread fallback."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _ensure_telegram_mock():
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
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def test_picker_provider_list_keeps_current_provider_before_moa(monkeypatch):
    """The virtual MoA row must not displace the active provider on Telegram.

    The Telegram picker renders providers in the order returned by
    list_picker_providers(). If MoA is blindly prepended, the first tap after
    `/model` is no longer the current provider, making the common "change model
    within my current provider" flow needlessly awkward.
    """
    from hermes_cli import inventory, model_switch

    monkeypatch.setattr(
        model_switch,
        "list_authenticated_providers",
        lambda **_kwargs: [
            {
                "slug": "openai-codex",
                "name": "OpenAI Codex",
                "is_current": True,
                "models": ["gpt-5.5"],
                "total_models": 1,
            },
            {
                "slug": "custom:api.venice.ai",
                "name": "api.venice.ai",
                "is_current": False,
                "is_user_defined": True,
                "api_url": "https://api.venice.ai/api/v1",
                "models": ["qwen3-coder"],
                "total_models": 1,
            },
        ],
    )
    monkeypatch.setattr(
        inventory,
        "_moa_provider_row",
        lambda current_provider="": {
            "slug": "moa",
            "name": "Mixture of Agents",
            "is_current": current_provider == "moa",
            "models": ["default"],
            "total_models": 1,
        },
    )

    providers = model_switch.list_picker_providers(
        current_provider="openai-codex",
        include_moa=True,
    )

    assert [p["slug"] for p in providers[:2]] == ["openai-codex", "moa"]


class TestTelegramModelPicker:
    @pytest.mark.asyncio
    async def test_send_model_picker_escapes_dynamic_provider_label(self):
        adapter = _make_adapter()
        sent = {}

        async def mock_send_message(**kwargs):
            sent.update(kwargs)
            return SimpleNamespace(message_id=101)

        adapter._bot.send_message = AsyncMock(side_effect=mock_send_message)

        result = await adapter.send_model_picker(
            chat_id="12345",
            providers=[
                {"slug": "provider_one", "name": "Provider One", "total_models": 1, "is_current": True}
            ],
            current_model="model_1",
            current_provider="provider_one",
            session_key="s",
            on_model_selected=AsyncMock(),
            metadata={"thread_id": "99999"},
        )

        assert result.success is True
        assert "MARKDOWN_V2" in repr(sent["parse_mode"])
        assert "provider\\_one" in sent["text"]
        assert "`model_1`" in sent["text"]

    @pytest.mark.asyncio
    async def test_back_button_escapes_dynamic_provider_label(self):
        adapter = _make_adapter()
        adapter._model_picker_state["12345"] = {
            "providers": [{"slug": "provider_one", "name": "Provider One", "total_models": 1, "is_current": True}],
            "current_model": "model_1",
            "current_provider": "provider_one",
            "session_key": "s",
            "on_model_selected": AsyncMock(),
            "msg_id": 42,
        }

        query = AsyncMock()
        query.data = "mb"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        await adapter._handle_model_picker_callback(query, "mb", "12345")

        edit_kwargs = query.edit_message_text.call_args[1]
        assert "MARKDOWN_V2" in repr(edit_kwargs["parse_mode"])
        assert "provider\\_one" in edit_kwargs["text"]
        assert "`model_1`" in edit_kwargs["text"]

    @pytest.mark.asyncio
    async def test_provider_drilldown_prioritizes_and_marks_current_model(self, monkeypatch):
        import plugins.platforms.telegram.adapter as tg

        class _RecordingButton:
            def __init__(self, text, callback_data=None, **kw):
                self.text = text
                self.callback_data = callback_data

        class _RecordingMarkup:
            def __init__(self, rows):
                self.inline_keyboard = rows

        monkeypatch.setattr(tg, "InlineKeyboardButton", _RecordingButton)
        monkeypatch.setattr(tg, "InlineKeyboardMarkup", _RecordingMarkup)

        adapter = _make_adapter()
        adapter._model_picker_state["12345"] = {
            "providers": [
                {
                    "slug": "openai-codex",
                    "name": "OpenAI Codex",
                    "models": ["gpt-5.6-sol", "gpt-5.5", "gpt-5.4"],
                    "total_models": 3,
                    "is_current": True,
                }
            ],
            "current_model": "gpt-5.5",
            "current_provider": "openai-codex",
            "session_key": "s",
            "on_model_selected": AsyncMock(),
            "msg_id": 42,
        }

        query = AsyncMock()
        query.data = "mp:openai-codex"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        await adapter._handle_model_picker_callback(query, "mp:openai-codex", "12345")

        assert adapter._model_picker_state["12345"]["model_list"] == [
            "gpt-5.5",
            "gpt-5.6-sol",
            "gpt-5.4",
        ]
        markup = query.edit_message_text.call_args[1]["reply_markup"]
        labels = [button.text for row in markup.inline_keyboard for button in row]
        assert labels[:3] == ["✓ gpt-5.5", "gpt-5.6-sol", "gpt-5.4"]

    @pytest.mark.asyncio
    async def test_provider_drilldown_includes_direct_command_hint_for_paginated_models(self, monkeypatch):
        import plugins.platforms.telegram.adapter as tg

        class _RecordingButton:
            def __init__(self, text, callback_data=None, **kw):
                self.text = text
                self.callback_data = callback_data

        class _RecordingMarkup:
            def __init__(self, rows):
                self.inline_keyboard = rows

        monkeypatch.setattr(tg, "InlineKeyboardButton", _RecordingButton)
        monkeypatch.setattr(tg, "InlineKeyboardMarkup", _RecordingMarkup)

        adapter = _make_adapter()
        adapter.format_message = lambda content: content
        adapter._model_picker_state["12345"] = {
            "providers": [
                {
                    "slug": "custom:api.venice.ai",
                    "name": "api.venice.ai",
                    "models": [f"venice-model-{i}" for i in range(9)],
                    "total_models": 9,
                    "is_current": False,
                }
            ],
            "current_model": "gpt-5.5",
            "current_provider": "openai-codex",
            "session_key": "s",
            "on_model_selected": AsyncMock(),
            "msg_id": 42,
        }

        query = AsyncMock()
        query.data = "mp:custom:api.venice.ai"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        await adapter._handle_model_picker_callback(
            query, "mp:custom:api.venice.ai", "12345"
        )

        text = query.edit_message_text.call_args[1]["text"]
        assert "type `/model <name> --provider custom:api.venice.ai`" in text

    @pytest.mark.asyncio
    async def test_model_selected_edits_message_on_success(self):
        """Regression: the mm: (model selected → switch) success path must
        edit the picker message to show the confirmation and remove the
        buttons.  An earlier revision of this PR over-indented the
        edit_message_text block so it lived inside the except branch and
        only fired when the callback raised."""
        adapter = _make_adapter()
        callback = AsyncMock(return_value="Switched to `gpt-5`")
        adapter._model_picker_state["12345"] = {
            "providers": [
                {"slug": "openai", "name": "OpenAI", "total_models": 1, "is_current": True}
            ],
            "current_model": "model_1",
            "current_provider": "openai",
            "session_key": "s",
            "on_model_selected": callback,
            "selected_provider": "openai",
            "model_list": ["gpt-5"],
            "msg_id": 42,
        }

        query = AsyncMock()
        query.data = "mm:0"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        await adapter._handle_model_picker_callback(query, "mm:0", "12345")

        callback.assert_awaited_once()
        query.edit_message_text.assert_awaited()
        edit_kwargs = query.edit_message_text.call_args[1]
        assert "MARKDOWN_V2" in repr(edit_kwargs["parse_mode"])
        assert "`gpt-5`" in edit_kwargs["text"]
        assert "12345" not in adapter._model_picker_state
