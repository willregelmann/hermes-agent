"""Issue #86: a bare local-file path must not vanish from the reply when the attach can't happen.

``extract_local_files`` deletes the path from the text before any upload is attempted. When the
adapter cannot attach (Google Chat without user OAuth) or the upload fails, the path used to reach
the user nowhere. The two contracts below:

* probe says the adapter can't attach for this source -> the text keeps the path, nothing is uploaded;
* probe says yes but the upload fails -> a follow-up message carries the full path.
"""

import asyncio

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, build_session_key


class _AttachAdapter(BasePlatformAdapter):
    def __init__(self, *, can_attach: bool, upload_ok: bool):
        super().__init__(PlatformConfig(enabled=True, token="fake-token"), Platform.DISCORD)
        self._can_attach = can_attach
        self._upload_ok = upload_ok
        self.sent: list[str] = []
        self.documents: list[str] = []

    def can_attach_local_files(self, source) -> bool:
        return self._can_attach

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(content)
        return SendResult(success=True, message_id=f"m{len(self.sent)}")

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None,
                            metadata=None, **kwargs) -> SendResult:
        self.documents.append(file_path)
        if self._upload_ok:
            return SendResult(success=True, message_id="doc")
        return SendResult(success=False, error="native attachment requires user OAuth")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


async def _hold_typing(_chat_id, interval=2.0, metadata=None, stop_event=None):
    await (stop_event.wait() if stop_event is not None else asyncio.Event().wait())


@pytest.fixture
def report(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir()
    f = tmp_path / "PENDING-for-Will-bundle.md"
    f.write_text("bundle\n")
    return str(f)


async def _run(adapter: _AttachAdapter, reply: str) -> None:
    adapter._keep_typing = _hold_typing

    async def handler(_event):
        return reply

    adapter.set_message_handler(handler)
    event = MessageEvent(text="hi", source=SessionSource(platform=Platform.DISCORD, chat_id="c1", chat_type="dm"),
                         message_id="in1")
    await adapter._process_message_background(event, build_session_key(event.source))


class TestPathSurvivesWhenAttachCannotHappen:
    @pytest.mark.asyncio
    async def test_probe_false_keeps_path_in_text_and_uploads_nothing(self, report):
        adapter = _AttachAdapter(can_attach=False, upload_ok=True)
        await _run(adapter, f"The bundle is at {report} for review.")
        assert adapter.documents == []
        assert any(report in s for s in adapter.sent), adapter.sent

    @pytest.mark.asyncio
    async def test_probe_true_failed_upload_sends_followup_with_path(self, report):
        adapter = _AttachAdapter(can_attach=True, upload_ok=False)
        await _run(adapter, f"The bundle is at {report} for review.")
        assert adapter.documents == [report]
        assert any(report in s for s in adapter.sent), adapter.sent

    @pytest.mark.asyncio
    async def test_probe_true_successful_upload_still_strips_path(self, report):
        """Control: the attach path is unchanged when the upload works (the probe gates, it doesn't disable)."""
        adapter = _AttachAdapter(can_attach=True, upload_ok=True)
        await _run(adapter, f"The bundle is at {report} for review.")
        assert adapter.documents == [report]
        assert not any(report in s for s in adapter.sent), adapter.sent
