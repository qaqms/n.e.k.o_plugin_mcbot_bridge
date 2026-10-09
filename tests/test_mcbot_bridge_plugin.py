# -*- coding: utf-8 -*-
"""Unit tests for the mcbot_bridge plugin's service layer.

Exercises the in-process behaviour without a real HTTP bridge or the
user_plugin_server: config parsing, the single-slot claim (busy / overwrite /
anti-thrash), the SSE frame → ``push_message`` cue mapping, the watchdog that
keeps a lost completion event from wedging the slot, and the rule that raw
transport detail never reaches the model.

The plugin facade (``__init__.py``) is exercised separately through the SDK's
auto-register pipeline; here we drive :class:`McbotService` directly, the same
way the sibling ``test_game_agent_minecraft_plugin.py`` does.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

try:
    # Standalone plugin repository: tests/conftest.py registers the plugin
    # directory as a ``mcbot_bridge`` package, because the repository folder is
    # named for the market convention (``n.e.k.o_plugin_mcbot_bridge``) and is
    # not a valid Python identifier.
    from mcbot_bridge.client import (  # type: ignore[import-not-found]
        McbotAuthError,
        McbotBridgeError,
        McbotClient,
        McbotUnreachable,
        SseFrame,
    )
    from mcbot_bridge.service import (  # type: ignore[import-not-found]
        McbotService,
        normalise_base_url,
        task_looks_like_identifier,
    )
except ModuleNotFoundError:  # pragma: no cover - depends on checkout layout
    # N.E.K.O. source tree: the plugin is a submodule of this repository.
    _ROOT = Path(__file__).resolve().parents[2]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

    from plugin.plugins.mcbot_bridge.client import (  # noqa: E402
        McbotAuthError,
        McbotBridgeError,
        McbotClient,
        McbotUnreachable,
        SseFrame,
    )
    from plugin.plugins.mcbot_bridge.service import (  # noqa: E402
        McbotService,
        normalise_base_url,
        task_looks_like_identifier,
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeClient:
    """Stand-in for ``McbotClient``.

    The real client owns the HTTP session; here we record calls and return
    canned payloads or raise, so the service's own logic is what is under test.
    """

    def __init__(
        self,
        *,
        task_id: int | None = 7,
        ask_answer: str = "在挖石头",
        raise_on: dict[str, Exception] | None = None,
        status_payload: dict | None = None,
    ) -> None:
        self.base_url = "http://127.0.0.1:57121"
        self.token = ""
        self._task_id = task_id
        self._ask_answer = ask_answer
        self._raise_on = raise_on or {}
        self._status_payload = status_payload or {"in_game": True, "companion": "aimi"}
        self.calls: list[tuple] = []
        self._streaming = False
        self.last_error = ""
        self.session_id = ""
        self.cursor_resets = 0

    # -- transport surface the service uses -------------------------------

    async def open(self) -> None:
        self.calls.append(("open",))
        self._streaming = True

    async def close(self) -> None:
        self.calls.append(("close",))
        self._streaming = False

    def set_token(self, token: str) -> None:
        self.token = token
        self.calls.append(("set_token", token))

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    async def post_task(self, text: str, *, wait_s: float = 0.0) -> dict:
        self.calls.append(("post_task", text, wait_s))
        self._maybe_raise("post_task")
        return {"task_id": self._task_id, "done": False, "fragments": []}

    async def cancel_task(self, task_id=None) -> dict:
        self.calls.append(("cancel_task", task_id))
        self._maybe_raise("cancel_task")
        return {"ok": True}

    async def ask(self, text: str) -> dict:
        self.calls.append(("ask", text))
        self._maybe_raise("ask")
        return {"answer": self._ask_answer}

    async def answer(self, question_id: str, text: str) -> dict:
        self.calls.append(("answer", question_id, text))
        self._maybe_raise("answer")
        return {"ok": True}

    async def get_status(self) -> dict:
        self.calls.append(("get_status",))
        self._maybe_raise("get_status")
        return dict(self._status_payload)

    def note_session(self, session_id) -> None:
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id

    def reset_cursor(self) -> None:
        self.cursor_resets += 1

    def _maybe_raise(self, method: str) -> None:
        exc = self._raise_on.get(method)
        if exc is not None:
            raise exc


def _make_service(*, push_calls: list | None = None) -> tuple[McbotService, list]:
    captured = push_calls if push_calls is not None else []

    def fake_push(**kwargs):
        captured.append(kwargs)

    return McbotService(logger=None, push_message_fn=fake_push), captured


async def _settle() -> None:
    """Let queued tasks (watchdogs, resolve coroutines) run."""
    for _ in range(5):
        await asyncio.sleep(0)


def _frame(event: str, **payload) -> SseFrame:
    return SseFrame(event=event, data=json.dumps(payload), id="1")


# ---------------------------------------------------------------------------
# configure() / helpers
# ---------------------------------------------------------------------------


def test_configure_uses_shipped_defaults_when_keys_missing():
    service, _ = _make_service()
    service.configure({})
    status = service.get_status()
    assert status["base_url"] == "http://127.0.0.1:57121"
    assert status["stream_enabled"] is True


def test_configure_clamps_hostile_values():
    service, _ = _make_service()
    service.configure({
        "reconnect_interval_seconds": -5,
        "http_timeout_seconds": 0,
        "ask_timeout_seconds": 1,
        "task_wait_timeout_seconds": -1,
        "progress_min_interval_seconds": 0,
    })
    assert service._reconnect_interval == 0.5
    assert service._http_timeout == 0.5
    assert service._ask_timeout == 5.0
    assert service._task_wait_timeout == 5.0
    assert service._progress_min_interval == 1.0


def test_configure_ignores_wrongly_typed_values():
    service, _ = _make_service()
    service.configure({"stream_events": "yes", "base_url": 42, "game_dir": None})
    assert service._stream_events is True
    assert service._base_url == "http://127.0.0.1:57121"
    assert service._game_dir == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("127.0.0.1:57121", "http://127.0.0.1:57121"),
        ("http://127.0.0.1:57121/", "http://127.0.0.1:57121"),
        ("http://127.0.0.1:57121///", "http://127.0.0.1:57121"),
        ("", "http://127.0.0.1:57121"),
        ("   ", "http://127.0.0.1:57121"),
        ("http://host:1/path", "http://host:1/path"),
    ],
)
def test_normalise_base_url(raw, expected):
    # A trailing slash would make httpx join paths into ``//v1/task``.
    assert normalise_base_url(raw) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("break_block", True),
        ("goToCoordinates(1, 2, 3)", True),
        ("move_to", True),
        ("去挖点木头", False),
        ("chop some wood", False),
        ("挖 three 个", False),
        ("", False),
    ],
)
def test_task_looks_like_identifier(text, expected):
    assert task_looks_like_identifier(text) is expected


# ---------------------------------------------------------------------------
# Single task slot
# ---------------------------------------------------------------------------


async def test_claim_takes_the_slot_and_reports_busy_after():
    service, _ = _make_service()
    first = await service.try_claim_pending("挖点木头")
    assert first is not None
    assert service.has_pending_task()
    assert await service.try_claim_pending("再来点") is None
    assert service.current_task_text() == "挖点木头"


async def test_overwrite_is_refused_inside_the_anti_thrash_floor():
    service, _ = _make_service()
    first = await service.try_claim_pending("挖点木头")
    # Same instant: the floor exists so a model cannot interrupt work it just
    # started by re-dispatching in the same breath.
    assert await service.try_claim_pending("改成挖铁", overwrite=True) is None
    assert service._pending is first


async def test_overwrite_after_the_floor_supersedes_the_old_task():
    service, _ = _make_service()
    first = await service.try_claim_pending("挖点木头")
    assert first is not None
    first.started_at -= 10.0  # age past the floor
    second = await service.try_claim_pending("改成挖铁", overwrite=True)
    assert second is not None
    assert first.event.is_set()
    # Superseded, not failed — being replaced is not a malfunction.
    assert first.result["status"] == "superseded"


async def test_stop_releases_a_waiting_slot():
    service, _ = _make_service()
    claimed = await service.try_claim_pending("挖点木头")
    await service.stop()
    assert claimed is not None and claimed.event.is_set()
    assert not service.has_pending_task()


async def test_watchdog_releases_the_slot_when_no_completion_arrives():
    service, pushes = _make_service()
    service._task_wait_timeout = 0.05
    claimed = await service.try_claim_pending("挖点木头")
    assert claimed is not None
    await asyncio.sleep(0.2)
    # Without the watchdog a lost `done` event would wedge the slot forever
    # and every later dispatch would answer "busy".
    assert service._pending is None
    assert any("coalesce_key" in call and call["coalesce_key"] == "mcbot_completion" for call in pushes)
    await service.stop()


# ---------------------------------------------------------------------------
# dispatch_claimed
# ---------------------------------------------------------------------------


async def test_dispatch_records_mcbot_task_id_and_returns_empty_on_success():
    service, _ = _make_service()
    client = _FakeClient(task_id=42)
    service._client = client
    claimed = await service.try_claim_pending("挖点木头")
    assert claimed is not None
    assert await service.dispatch_claimed(claimed) == {}
    assert claimed.task_id == 42
    # Fire-and-forget: the dialog LLM's turn must not wait for the action.
    assert ("post_task", "挖点木头", 0.0) in client.calls
    await service.stop()


async def test_dispatch_releases_the_slot_when_the_bridge_is_unreachable():
    service, _ = _make_service()
    service._client = _FakeClient(raise_on={"post_task": McbotUnreachable()})
    claimed = await service.try_claim_pending("挖点木头")
    assert claimed is not None
    envelope = await service.dispatch_claimed(claimed)
    assert envelope["is_error"] is True
    assert envelope["error"] == "UNREACHABLE"
    assert not service.has_pending_task()


async def test_dispatch_without_a_client_returns_a_friendly_error():
    service, _ = _make_service()
    claimed = await service.try_claim_pending("挖点木头")
    assert claimed is not None
    envelope = await service.dispatch_claimed(claimed)
    assert envelope["is_error"] is True
    assert envelope["error"] == "BRIDGE_NOT_STARTED"
    assert not service.has_pending_task()


# ---------------------------------------------------------------------------
# SSE → cue mapping
# ---------------------------------------------------------------------------


async def test_progress_becomes_a_read_cue_and_is_throttled():
    service, pushes = _make_service()
    await service.handle_event(_frame("progress", text="step one"))
    await service.handle_event(_frame("progress", text="step two"))
    # One progress frame is emitted per executed step; without the floor a
    # busy task floods the dialog context.
    assert len(pushes) == 1
    assert pushes[0]["ai_behavior"] == "read"
    assert pushes[0]["coalesce_key"] == "mcbot_progress"
    assert pushes[0]["priority"] == 4
    assert "step one" in pushes[0]["parts"][0]["text"]


async def test_done_resolves_the_matching_task_and_responds():
    service, pushes = _make_service()
    service._client = _FakeClient(task_id=9)
    claimed = await service.try_claim_pending("挖点木头")
    assert claimed is not None
    await service.dispatch_claimed(claimed)

    await service.handle_event(_frame("done", task_id=9, text="挖到 3 个圆石", status="completed"))
    assert not service.has_pending_task()
    assert claimed.event.is_set()
    # v1.0 terminal vocabulary, not an invented "ok".
    assert claimed.result["status"] == "completed"

    completion = [c for c in pushes if c["coalesce_key"] == "mcbot_completion"]
    assert len(completion) == 1
    assert completion[0]["ai_behavior"] == "respond"
    assert completion[0]["priority"] == 7
    await service.stop()


async def test_done_for_another_task_leaves_our_slot_alone():
    service, _ = _make_service()
    service._client = _FakeClient(task_id=9)
    claimed = await service.try_claim_pending("挖点木头")
    assert claimed is not None
    await service.dispatch_claimed(claimed)

    # e.g. the user drove the companion via the in-game chat prefix.
    await service.handle_event(_frame("done", task_id=99, text="别的活做完了"))
    assert service.has_pending_task()
    await service.stop()


async def test_question_is_recorded_and_asks_the_user():
    service, pushes = _make_service()
    await service.handle_event(
        _frame("question", question_id="q3", text="要我把它烧掉吗", task_id=1)
    )
    assert service.get_status()["pending_questions"] == 1
    question = [c for c in pushes if c["coalesce_key"] == "mcbot_question"]
    assert question and question[0]["ai_behavior"] == "respond"
    assert question[0]["priority"] == 8


async def test_state_becomes_a_read_cue():
    service, pushes = _make_service()
    await service.handle_event(_frame("state", text="同伴已召唤"))
    assert pushes and pushes[0]["ai_behavior"] == "read"
    assert pushes[0]["coalesce_key"] == "mcbot_state"


async def test_unknown_and_malformed_events_are_ignored():
    service, pushes = _make_service()
    await service.handle_event(_frame("invented_event", text="x"))
    await service.handle_event(SseFrame(event="progress", data="not json", id=""))
    assert pushes == []


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def test_answer_confirms_and_forgets_the_question():
    service, _ = _make_service()
    service._client = _FakeClient()
    await service.handle_event(_frame("question", question_id="q3", text="烧掉吗"))
    result = await service.answer("q3", "烧")
    assert isinstance(result, str)
    assert service.get_status()["pending_questions"] == 0


async def test_answer_on_an_expired_question_is_reported_as_such():
    service, _ = _make_service()
    service._client = _FakeClient(
        raise_on={"answer": McbotBridgeError("gone", status=404, code="BRIDGE_HTTP_ERROR")}
    )
    result = await service.answer("q-old", "烧")
    assert result["is_error"] is True
    assert result["error"] == "QUESTION_EXPIRED"


async def test_ask_returns_the_companions_answer_verbatim():
    service, _ = _make_service()
    service._client = _FakeClient(ask_answer="在挖石头")
    assert "在挖石头" in await service.ask("你在干嘛")


async def test_ask_falls_back_when_the_companion_stays_silent():
    service, _ = _make_service()
    service._client = _FakeClient(ask_answer="")
    result = await service.ask("你在干嘛")
    assert isinstance(result, str) and result


async def test_cancel_without_a_client_is_an_error():
    service, _ = _make_service()
    claimed = await service.try_claim_pending("挖点木头")
    assert claimed is not None
    result = await service.cancel()
    assert result["is_error"] is True


# ---------------------------------------------------------------------------
# Bridge contract v1.0 conformance
# ---------------------------------------------------------------------------


def _client_with_transport(handler):
    """A client wired to an in-process transport, for asserting request shape."""
    import httpx


    client = McbotClient("http://127.0.0.1:57121", "tok")
    client._http = httpx.AsyncClient(
        base_url="http://127.0.0.1:57121",
        headers={"Authorization": "Bearer tok"},
        transport=httpx.MockTransport(handler),
    )
    return client


async def test_cancel_all_targets_id_zero_not_a_bare_path():
    """v1.0 has no /v1/task/cancel route; cancel-all is task_id 0.

    The bare path this plugin used to send never existed and 404s.
    """
    seen: list[str] = []
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"ok": True}, request=request)

    client = _client_with_transport(handler)
    await client.cancel_task()
    await client.cancel_task(12)
    await client.close()

    assert seen == ["/v1/task/0/cancel", "/v1/task/12/cancel"]


async def test_rest_error_code_is_parsed_from_the_envelope():
    """v1.0 error bodies carry a stable ``error_code`` the caller branches on."""
    import httpx


    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            413,
            json={"error": "body exceeds 65536 bytes", "error_code": "BODY_TOO_LARGE"},
            request=request,
        )

    client = _client_with_transport(handler)
    with pytest.raises(McbotBridgeError) as caught:
        await client.post_task("x" * 100)
    await client.close()

    assert caught.value.code == "BODY_TOO_LARGE"
    assert caught.value.status == 413


async def test_reset_cursor_clears_the_last_event_id():
    """Ids restart at 1 per bridge, so a stale cursor must be droppable."""

    client = McbotClient("http://127.0.0.1:57121", "tok")
    client._last_event_id = "30"
    client.reset_cursor()
    assert client.last_event_id == ""


async def test_adopt_session_resets_the_cursor_when_the_session_changes():
    service, _ = _make_service()
    client = _FakeClient(
        status_payload={"contract_version": "1.0", "session_id": "sess-A", "in_game": True}
    )
    service._client = client

    await service._adopt_session()
    assert service._session_id == "sess-A"
    assert service._contract_version == "1.0"
    resets_after_first = client.cursor_resets

    # Same session (e.g. a config reload): cursor reused, no reset.
    await service._adopt_session()
    assert client.cursor_resets == resets_after_first

    # New world -> new bridge -> ids restart at 1.
    client._status_payload = {
        "contract_version": "1.0", "session_id": "sess-B", "in_game": True,
    }
    await service._adopt_session()
    assert service._session_id == "sess-B"
    assert client.cursor_resets == resets_after_first + 1


async def test_session_change_invalidates_the_in_flight_task_as_unknown():
    """A bridge that went away cannot report what happened; never claim success."""
    service, _ = _make_service()
    client = _FakeClient(status_payload={"session_id": "sess-A"})
    service._client = client
    await service._adopt_session()

    claimed = await service.try_claim_pending("挖点木头")
    assert claimed is not None

    client._status_payload = {"session_id": "sess-B"}
    await service._adopt_session()

    assert claimed.event.is_set()
    assert claimed.result["status"] == "unknown"
    assert not service.has_pending_task()


async def test_session_change_discards_open_questions():
    service, _ = _make_service()
    client = _FakeClient(status_payload={"session_id": "sess-A"})
    service._client = client
    await service._adopt_session()
    await service.handle_event(
        _frame("question", question_id="q1", task_id=1, text="烧掉吗", session_id="sess-A")
    )
    assert service.get_status()["pending_questions"] == 1

    client._status_payload = {"session_id": "sess-B"}
    await service._adopt_session()
    assert service.get_status()["pending_questions"] == 0


async def test_a_frame_from_another_session_stops_writing_to_old_state():
    """v1.0: session_id mismatch means stop attributing to the old session."""
    service, _ = _make_service()
    service._session_id = "sess-A"
    claimed = await service.try_claim_pending("挖点木头")
    assert claimed is not None

    await service.handle_event(
        _frame("done", task_id=1, text="别的工作", status="completed", session_id="sess-B")
    )
    # The old task must not be resolved by a different session's event.
    assert claimed.result is None or claimed.result.get("status") != "completed"


@pytest.mark.parametrize("status", ["failed"])
async def test_a_failed_done_is_never_framed_as_success(status):
    service, pushes = _make_service()
    claimed = await service.try_claim_pending("挖点木头")
    assert claimed is not None
    claimed.awaiting_id = False

    await service.handle_event(
        _frame("done", task_id=1, text="找不到路", status=status, session_id=service._session_id)
    )
    assert claimed.result["status"] == status
    cues = [c for c in pushes if c["coalesce_key"] == "mcbot_completion"]
    assert len(cues) == 1
    text = cues[0]["parts"][0]["text"]
    # The failure wording, not the "finished" one.
    assert "did not finish" in text or "没做成" in text


@pytest.mark.parametrize("status", ["cancelled", "superseded"])
async def test_cancelled_and_superseded_dones_stay_silent(status):
    """Deliberate stops are not news — cue'ing them invites a retry storm."""
    service, pushes = _make_service()
    claimed = await service.try_claim_pending("挖点木头")
    assert claimed is not None
    claimed.awaiting_id = False

    await service.handle_event(
        _frame("done", task_id=1, text="已取消", status=status, session_id=service._session_id)
    )
    assert claimed.result["status"] == status
    assert not service.has_pending_task()
    assert [c for c in pushes if c["coalesce_key"] == "mcbot_completion"] == []


async def test_a_done_arriving_before_the_post_returns_is_still_grouped():
    """The bridge takes its cursor before submitting, so a fast done can win."""
    service, pushes = _make_service()
    client = _FakeClient(task_id=77)
    service._client = client

    claimed = await service.try_claim_pending("挖点木头")
    assert claimed is not None
    assert claimed.awaiting_id is True

    # Arrives while the POST is still in flight: parked, not mis-attributed.
    await service.handle_event(
        _frame("done", task_id=77, text="做完了", status="completed")
    )
    assert claimed.event.is_set() is False
    assert len(claimed.buffered) == 1

    await service.dispatch_claimed(claimed)
    assert claimed.awaiting_id is False
    assert claimed.event.is_set()
    assert claimed.result["status"] == "completed"
    assert any(c["coalesce_key"] == "mcbot_completion" for c in pushes)


async def test_an_unattributable_done_does_not_clear_our_slot():
    service, _ = _make_service()
    claimed = await service.try_claim_pending("挖点木头")
    assert claimed is not None
    claimed.task_id = 5
    claimed.awaiting_id = False

    await service.handle_event(
        _frame("done", task_id=99, text="别人的活", status="completed")
    )
    assert service.has_pending_task()
    assert not claimed.event.is_set()


async def test_unknown_error_codes_degrade_without_leaking_the_code():
    service, _ = _make_service()
    service.set_lang("en")
    envelope = service.error_envelope(
        McbotBridgeError("internal detail", status=500, code="SOME_FUTURE_CODE"),
        query="挖点木头",
    )
    text = json.dumps(envelope, ensure_ascii=False)
    # The machine code may ride along in the `error` field; the human-facing
    # message must not, and neither may the exception's own text.
    assert "SOME_FUTURE_CODE" not in envelope["output"]["error"]
    assert "internal detail" not in text
    assert envelope["output"]["error"]


async def test_known_error_codes_map_to_speakable_text():
    service, _ = _make_service()
    service.set_lang("en")
    envelope = service.error_envelope(
        McbotBridgeError("body exceeds 65536 bytes", status=413, code="BODY_TOO_LARGE")
    )
    assert "too long to pass along" in envelope["output"]["error"]


async def test_status_exposes_the_session_and_contract_version():
    service, _ = _make_service()
    service._client = _FakeClient(
        status_payload={"contract_version": "1.0", "session_id": "sess-A"}
    )
    await service._adopt_session()
    status = service.get_status()
    assert status["session_id"] == "sess-A"
    assert status["contract_version"] == "1.0"


# ---------------------------------------------------------------------------
# Privacy: raw transport detail must never reach the model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        McbotUnreachable(),
        McbotAuthError(),
        McbotBridgeError("POST /v1/task timed out", code="BRIDGE_TIMEOUT"),
        McbotBridgeError("boom", status=500, code="BRIDGE_HTTP_ERROR"),
    ],
)
def test_error_envelope_carries_no_transport_detail(exc):
    service, _ = _make_service()
    envelope = service.error_envelope(exc, query="挖点木头")
    text = json.dumps(envelope, ensure_ascii=False)
    # Tool results persist in the conversation transcript, so a host, a port or
    # an endpoint path must never appear in them.
    for leak in ("127.0.0.1", "57121", "/v1/", "POST", "ConnectError", "timed out"):
        assert leak not in text
    assert envelope["is_error"] is True
    assert envelope["output"]["error"]


def test_progress_cue_carries_the_internal_state_gag():
    service, pushes = _make_service()
    service.set_lang("en")
    service._push_cue(
        "CUE_PROGRESS",
        ai_behavior="read",
        priority=4,
        coalesce_key="mcbot_progress",
        text="mined 3 cobble",
    )
    assert len(pushes) == 1
    text = pushes[0]["parts"][0]["text"]
    assert "mined 3 cobble" in text
    # The gag is why the character narrates in-world instead of reporting
    # "SSE" / "mcbot" / "tool" to the user.
    assert "Do NOT narrate internals" in text
