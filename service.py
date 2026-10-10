"""State and semantics for the mcbot bridge.

This module owns everything that is *about the game* rather than about the
wire: the single in-flight task slot, the reverse-question bookkeeping, and
the translation of bridge events into ``push_message`` cues the character can
act on.

Deliberately absent: any nudge/polling loop. The sibling
``game_agent_minecraft`` plugin needs one (~130 lines of ``_system_prompt_loop``)
because mc-agent never volunteers information, so the only way to keep the
model moving is to poll game state and prod it. mcbot *pushes* — its
``/v1/events`` stream delivers progress, reverse questions and completion as
they happen — so that entire layer is unnecessary here. Losing it is the main
structural win of this plugin over its sibling.

This module imports no plugin SDK types; ``push_message`` arrives as an
injected callable, which keeps the service unit-testable on its own.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import prompts
from .client import (
    McbotBridgeError,
    McbotClient,
    McbotUnreachable,
    SseFrame,
)

DEFAULT_BASE_URL = "http://127.0.0.1:57121"

# Bridge contract this plugin is written against. The bridge advertises it on
# status and on every event; anything else is a legacy-compatible bridge, not a
# v1.0 one, and must not be treated as passing v1.0 acceptance.
CONTRACT_VERSION = "1.0"

# Task terminal statuses. Only "completed" is success — cancelled and
# superseded mean the work stopped, not that it finished, and reporting either
# as success is the exact trap the sibling plugin documents.
TASK_STATUS_COMPLETED = "completed"
TASK_STATUSES_TERMINAL = ("completed", "failed", "cancelled", "superseded")
# Statuses that are non-events for narration: the task was replaced or stopped
# on purpose, and the LLM already knows. Matches the sibling plugin's
# [NO-SWITCH-CUE] decision — cue'ing them just triggers a retry storm.
TASK_STATUSES_SILENT = ("cancelled", "superseded")

# Minimum age before an overwrite is honored. Mirrors the sibling plugin's
# anti-thrash floor: without it, a model that re-dispatches in the same breath
# as its first call would interrupt work it just started.
OVERWRITE_MIN_SURVIVAL_S = 2.0

# Cap on remembered reverse questions. mcbot numbers them monotonically and
# expires them server-side; this is only a memory bound for the local cache.
MAX_PENDING_QUESTIONS = 32

# Cap on retained events, matching mcbot's own 200-event replay ring so a
# reconnect that replays the whole buffer cannot exceed it.
RECENT_EVENT_LIMIT = 200

# Cap on frames parked while a dispatch is awaiting its task id. The window is
# one HTTP round-trip wide, so anything beyond this is the bridge misbehaving;
# the bound exists so a malformed stream cannot grow the list without limit.
_MAX_BUFFERED_EVENTS = 32

# Priority scale is repo-wide, HIGHER = more important:
#   alert 9 > question 8 > completion 7 > progress/state 4
PRIORITY_STATE = 4
PRIORITY_PROGRESS = 4
PRIORITY_COMPLETION = 7
PRIORITY_QUESTION = 8


def task_looks_like_identifier(text: str) -> bool:
    """True when a "task" is really a bare tool/function name.

    Models occasionally answer a request by naming a function instead of
    describing a goal — ``break_block``, ``move_to``, ``goToCoordinates(10,
    64, 20)``. Passed through, mcbot's brain treats the token as a directive and
    the model then sees its own identifier echoed in the progress stream, which
    is exactly the shape that produces dispatch loops. Refusing it with a hint
    costs one turn and removes the failure mode.

    Heuristic, not a parser: a short single token with no whitespace and no
    CJK, or anything with call syntax. Ordinary short requests ("过来") must
    still pass, hence the permissive length and the explicit CJK bail-out.
    """
    stripped = text.strip()
    if not stripped:
        return False
    if any("一" <= ch <= "鿿" for ch in stripped):
        return False
    if "(" in stripped and ")" in stripped:
        return True
    if any(ch.isspace() for ch in stripped):
        return False
    if len(stripped) > 40:
        return False
    return all(ch.isalnum() or ch in "_." for ch in stripped)


def _as_float(value: Any, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return default


def _as_str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def normalise_base_url(raw: Any) -> str:
    """Trim a configured base URL to something ``httpx`` can join paths onto.

    ``httpx`` appends the request path to ``base_url``, so a trailing slash or
    a stray path segment silently produces ``//v1/task`` or ``/foo/v1/task``.
    """
    text = _as_str(raw).strip()
    if not text:
        return DEFAULT_BASE_URL
    text = text.rstrip("/")
    if not text:
        return DEFAULT_BASE_URL
    if "://" not in text:
        text = "http://" + text
    return text


def default_game_dir() -> str:
    """Best-effort platform default Minecraft directory, or ``""``.

    Only a convenience for the common case; the operator can set ``game_dir``
    explicitly when Minecraft lives somewhere else (a launcher profile, a
    portable install, a different drive).
    """
    try:
        if os.name == "nt":
            appdata = os.environ.get("APPDATA")
            return str(Path(appdata) / ".minecraft") if appdata else ""
        home = os.path.expanduser("~")
        if sys.platform == "darwin":
            return str(Path(home) / "Library" / "Application Support" / "minecraft")
        return str(Path(home) / ".minecraft")
    except Exception:
        return ""


class PendingTask:
    """The single in-flight task slot.

    ``task_id`` is mcbot's own numeric id, returned synchronously by
    ``POST /v1/task``. That is a nicer handle than the sibling plugin's
    self-minted uuid: the id needed to correlate the later ``done`` event is
    known the moment the task is accepted, with no echo round-trip.
    """

    __slots__ = (
        "text", "task_id", "started_at", "event", "result", "watchdog",
        "awaiting_id", "buffered",
    )

    def __init__(self, text: str) -> None:
        self.text = text
        self.task_id: Optional[int] = None
        self.started_at = time.monotonic()
        self.event = asyncio.Event()
        self.result: Optional[Dict[str, Any]] = None
        # Set by the service when the slot is taken; cleared when it is
        # released, so a task that resolves early does not leave a sleeper
        # parked on the wait budget.
        self.watchdog: Optional[asyncio.Task[None]] = None
        # True between claiming the slot and learning the task id from the
        # POST response. The bridge takes its event cursor *before* submitting,
        # so a fast ``done`` can land on the stream while the HTTP call is
        # still in flight — those frames are parked here and replayed once the
        # id is known, instead of being matched against a task we cannot yet
        # identify.
        self.awaiting_id = True
        self.buffered: list[Dict[str, Any]] = []


class McbotService:
    """Bridge state machine: one task slot, one event stream, one token."""

    def __init__(
        self,
        *,
        logger: Any = None,
        push_message_fn: Callable[..., Any],
    ) -> None:
        self.logger = logger
        self._push_message = push_message_fn
        self._lang = prompts.DEFAULT_LANG

        # --- configuration (parsed by configure(), clamped on the way in) ---
        self._base_url = DEFAULT_BASE_URL
        self._game_dir = ""
        self._token_file = ""
        self._reconnect_interval = 5.0
        self._http_timeout = 10.0
        self._ask_timeout = 150.0
        self._task_wait_timeout = 300.0
        self._stream_events = True
        self._progress_min_interval = 6.0

        # --- transport ---
        self._client: Optional[McbotClient] = None
        self._bridge_task: Optional[asyncio.Task[None]] = None
        self._stopping = False

        # --- token ---
        self._token = ""
        self._token_path = ""
        self._token_error = ""

        # --- bridge session (v1.0) ---
        # The bridge mints a session_id per run and restarts SSE ids at 1. When
        # it changes, anything we believed about the old session is void: the
        # cursor must reset or the new session's events stay invisible.
        self._session_id = ""
        self._contract_version = ""
        self._contract_warned = False

        # --- game state ---
        self._pending: Optional[PendingTask] = None
        self._pending_lock = asyncio.Lock()
        self._watchdogs: set[asyncio.Task[None]] = set()
        self._pending_questions: Dict[str, Dict[str, Any]] = {}
        self._recent_events: deque[Dict[str, Any]] = deque(maxlen=RECENT_EVENT_LIMIT)
        self._last_progress_at = 0.0
        self._last_event_at = 0.0
        self._last_stream_error = ""
        self._last_error_code = ""

    # ==================================================================
    # Configuration
    # ==================================================================

    def set_lang(self, lang: str) -> None:
        self._lang = lang or prompts.DEFAULT_LANG

    def configure(self, cfg: Optional[Dict[str, Any]]) -> None:
        """Parse ``[mcbot]`` into typed, clamped fields.

        Every read has a default so a missing key degrades to the shipped
        behaviour instead of raising during startup.
        """
        cfg = cfg if isinstance(cfg, dict) else {}
        self._base_url = normalise_base_url(cfg.get("base_url"))
        self._game_dir = _as_str(cfg.get("game_dir")).strip()
        self._token_file = _as_str(cfg.get("token_file")).strip()
        self._reconnect_interval = max(
            0.5, _as_float(cfg.get("reconnect_interval_seconds"), 5.0)
        )
        self._http_timeout = max(
            0.5, _as_float(cfg.get("http_timeout_seconds"), 10.0)
        )
        self._ask_timeout = max(5.0, _as_float(cfg.get("ask_timeout_seconds"), 150.0))
        self._task_wait_timeout = max(
            5.0, _as_float(cfg.get("task_wait_timeout_seconds"), 300.0)
        )
        self._stream_events = _as_bool(cfg.get("stream_events"), True)
        self._progress_min_interval = max(
            1.0, _as_float(cfg.get("progress_min_interval_seconds"), 6.0)
        )

    # Fields whose change requires tearing the transport down and rebuilding
    # it. Pure-value changes (timeouts, intervals) apply in place.
    _TRANSPORT_KEYS = (
        "_base_url",
        "_game_dir",
        "_token_file",
        "_reconnect_interval",
        "_stream_events",
    )

    async def reload_config_live(self, cfg: Optional[Dict[str, Any]]) -> bool:
        """Apply new config; returns ``True`` when the transport was restarted."""
        before = tuple(getattr(self, key) for key in self._TRANSPORT_KEYS)
        self.configure(cfg)
        after = tuple(getattr(self, key) for key in self._TRANSPORT_KEYS)
        if before == after or self._bridge_task is None:
            # A token rotation on disk is picked up by the running loop's next
            # tick; nothing to rebuild.
            return False
        await self.stop()
        await self.start()
        return True

    # ==================================================================
    # Lifecycle — must run on the host's long-lived event loop
    # ==================================================================

    async def start(self) -> None:
        """Spawn the bridge supervisor. Idempotent."""
        existing = self._bridge_task
        if existing is not None and not existing.done():
            return
        self._stopping = False
        self._bridge_task = asyncio.create_task(
            self._bridge_loop(), name="mcbot_bridge.bridge_loop"
        )

    async def stop(self) -> None:
        """Cancel the supervisor, close the client, release any waiter."""
        self._stopping = True
        task = self._bridge_task
        self._bridge_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self._log_warning("bridge loop shutdown error: {}", type(exc).__name__)

        for watchdog in tuple(self._watchdogs):
            watchdog.cancel()
        self._watchdogs.clear()

        client = self._client
        self._client = None
        if client is not None:
            await client.close()

        pending = self._pending
        self._pending = None
        if pending is not None and not pending.event.is_set():
            pending.result = {"status": "cancelled", "query": pending.text}
            pending.event.set()

    async def _bridge_loop(self) -> None:
        """Own the connection policy: refresh the token, then stream.

        One iteration = one connection attempt plus the retry pause. The token
        is re-read every pass because mcbot writes ``bridge.token`` only on the
        first world join — a plugin enabled before Minecraft was ever launched
        must pick the token up later without a restart.
        """
        while not self._stopping:
            try:
                await self._refresh_token()
                if not self._token:
                    # No credentials yet. mcbot writes bridge.token on the
                    # first world join, so this is the normal state before a
                    # world has ever been entered — and also what a wrong
                    # game_dir looks like. Attempting a request anyway sends an
                    # empty bearer token, which httpx rejects as a malformed
                    # header, so the retry loop would otherwise spam a protocol
                    # error every interval instead of saying what is wrong.
                    self._log_info(
                        "no bridge token yet ({}); waiting {:.0f}s",
                        self._token_error or "unknown", self._reconnect_interval,
                    )
                    await asyncio.sleep(self._reconnect_interval)
                    continue
                await self._ensure_client()
                client = self._client
                if client is not None and self._stream_events:
                    # Reconcile the session *before* opening the stream: v1.0
                    # only lets the old cursor be reused when the session is
                    # unchanged, and this is where a changed one is detected.
                    await self._adopt_session()
                    await client.pump_events(self.handle_event)
                else:
                    await asyncio.sleep(self._reconnect_interval)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never let one bad pass kill the supervisor; the loop is the
                # only thing that can bring the stream back.
                self._log_warning(
                    "bridge loop error: {}: {}", type(exc).__name__, exc
                )

            if self._stopping:
                break
            try:
                await asyncio.sleep(self._reconnect_interval)
            except asyncio.CancelledError:
                break

    # ==================================================================
    # Token
    # ==================================================================

    def resolve_token_path(self) -> Optional[Path]:
        """Locate ``bridge.token``, or ``None`` when no directory is usable."""
        explicit = self._token_file
        if explicit:
            return Path(explicit).expanduser()
        game_dir = self._game_dir or default_game_dir()
        if not game_dir:
            return None
        return Path(game_dir).expanduser() / "mcbot" / "bridge.token"

    async def _refresh_token(self) -> None:
        path = self.resolve_token_path()
        if path is None:
            self._token = ""
            self._token_path = ""
            self._token_error = "NO_GAME_DIR"
            return
        self._token_path = str(path)
        try:
            # Disk read off the event loop — a stalled network drive must not
            # freeze every handler in the process.
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except FileNotFoundError:
            self._token = ""
            self._token_error = "TOKEN_MISSING"
            return
        except OSError as exc:
            self._token = ""
            self._token_error = type(exc).__name__
            return
        token = raw.strip()
        if not token:
            self._token = ""
            self._token_error = "TOKEN_EMPTY"
            return
        if token != self._token:
            # Log the fact of rotation, never the value.
            self._log_info("bridge token loaded ({} chars)", len(token))
        self._token = token
        self._token_error = ""

    async def _ensure_client(self) -> None:
        client = self._client
        if client is None:
            client = McbotClient(
                self._base_url,
                self._token,
                http_timeout=self._http_timeout,
                ask_timeout=self._ask_timeout,
                logger=self.logger,
            )
            await client.open()
            self._client = client
            return
        client.set_token(self._token)

    # ==================================================================
    # Bridge session (v1.0)
    # ==================================================================

    async def _adopt_session(self) -> None:
        """Query status and reconcile the bridge session before streaming.

        v1.0 requires the connector to check status after any disconnect: only
        when the session is unchanged may the previous ``Last-Event-ID`` be
        reused. A new session invalidates everything the old one knew — its
        tasks are not resumed or re-dispatched, its questions are void, and the
        cursor resets because ids restart at 1.
        """
        client = self._client
        if client is None:
            return
        try:
            payload = await client.get_status()
        except McbotBridgeError as exc:
            self._last_error_code = exc.code
            return

        version = _as_str(payload.get("contract_version"))
        self._contract_version = version
        if version != CONTRACT_VERSION and not self._contract_warned:
            self._contract_warned = True
            self._log_warning(
                "bridge advertises contract {!r}, this plugin targets {!r};"
                " treating it as legacy-compatible, not v1.0",
                version or "missing", CONTRACT_VERSION,
            )

        session = _as_str(payload.get("session_id"))
        client.note_session(session)
        if session and session != self._session_id:
            await self._invalidate_session_state()
            self._session_id = session
            client.reset_cursor()

    async def _invalidate_session_state(self) -> None:
        """Discard everything that belonged to the previous bridge session.

        The connected task is marked *unknown*, not failed and not succeeded:
        a bridge that went away cannot report what happened to work it was
        holding, and the contract forbids claiming either.
        """
        pending = self._pending
        if pending is not None:
            async with self._pending_lock:
                self._release_lock_free(pending)
            if not pending.event.is_set():
                pending.result = {
                    "status": "unknown",
                    "query": pending.text,
                    "reason": "session_changed",
                }
                pending.event.set()
        if self._pending_questions:
            self._pending_questions.clear()
        self._log_info("bridge session changed; previous session state discarded")

    async def try_claim_pending(
        self, text: str, *, overwrite: Any = False
    ) -> Optional[PendingTask]:
        """Atomically take the single task slot, or return ``None`` if busy.

        Check-and-claim runs under one lock so two concurrent ``mcbot_task``
        calls cannot both see an empty slot and both dispatch.
        """
        overwrite_flag = overwrite is True
        async with self._pending_lock:
            current = self._pending
            if current is not None:
                if not overwrite_flag:
                    self._log_info("claim refused: task slot busy")
                    return None
                age = time.monotonic() - current.started_at
                if age < OVERWRITE_MIN_SURVIVAL_S:
                    self._log_info("overwrite rejected: anti-thrash floor")
                    return None
                # Wake the previous waiter as superseded rather than failed —
                # being replaced is not a malfunction and must not be narrated
                # as one.
                if not current.event.is_set():
                    current.result = {"status": "superseded", "query": current.text}
                    current.event.set()
                self._cancel_watchdog(current)
            claimed = PendingTask(text)
            self._pending = claimed
        self._spawn_watchdog(claimed)
        return claimed

    def _spawn_watchdog(self, pending: PendingTask) -> None:
        """Release the slot if no ``done``/``state`` arrives in time.

        Without this a task whose completion event was lost (bridge restart
        mid-task, crashed companion) would wedge the slot forever and every
        later dispatch would report "busy".
        """
        task = asyncio.create_task(
            self._watch_pending(pending), name="mcbot_bridge.watchdog"
        )
        pending.watchdog = task
        self._watchdogs.add(task)

        def _done(finished: asyncio.Task[None]) -> None:
            self._watchdogs.discard(finished)
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                self._log_warning(
                    "watchdog error: {}: {}", type(exc).__name__, exc
                )

        task.add_done_callback(_done)

    async def _watch_pending(self, pending: PendingTask) -> None:
        try:
            await asyncio.sleep(self._task_wait_timeout)
        except asyncio.CancelledError:
            return
        if pending.event.is_set():
            return
        async with self._pending_lock:
            if self._pending is not pending:
                return
            self._pending = None
        pending.result = {"status": "timeout", "query": pending.text}
        pending.event.set()
        self._log_info("task {} exceeded the wait budget", pending.task_id)
        self._push_cue(
            "CUE_DONE",
            ai_behavior="respond",
            priority=PRIORITY_COMPLETION,
            coalesce_key="mcbot_completion",
            text=prompts.t("ERR_TIMEOUT", lang=self._lang),
        )

    async def dispatch_claimed(self, pending: PendingTask) -> Dict[str, Any]:
        """Send a claimed task. Returns ``{}`` on success, an error envelope otherwise.

        Returns immediately: ``wait_s=0`` tells mcbot to queue the task and
        answer with its id, so the dialog LLM's turn is never held for the
        length of the in-game action. The outcome arrives over SSE.
        """
        client = self._client
        if client is None:
            await self._release(pending)
            return {
                "output": {"error": prompts.t("ERR_UNREACHABLE", lang=self._lang)},
                "is_error": True,
                "error": "BRIDGE_NOT_STARTED",
            }
        try:
            payload = await client.post_task(pending.text, wait_s=0.0)
        except McbotBridgeError as exc:
            await self._release(pending)
            return self.error_envelope(exc, query=pending.text)

        task_id = payload.get("task_id")
        if isinstance(task_id, int) and not isinstance(task_id, bool):
            pending.task_id = task_id
        # The id is known now, so frames that arrived mid-POST can be
        # attributed. Replay before checking the window result below.
        pending.awaiting_id = False
        buffered, pending.buffered = pending.buffered, []
        for parked in buffered:
            await self._apply_done(parked)
        self._log_info("task dispatched (id={})", pending.task_id)

        # `done: true` means the task finished inside the response window; with
        # wait_s=0 that is an instantly-resolved directive and no `done` event
        # will follow. The contract puts the report in `fragments`, not `text`,
        # and carries `status` whenever done is true.
        if payload.get("done") is True and not pending.event.is_set():
            fragments = payload.get("fragments")
            text = _as_str(payload.get("text")).strip()
            if not text and isinstance(fragments, list):
                text = " ".join(str(part) for part in fragments if part)
            await self._apply_done({
                "task_id": pending.task_id,
                "text": text,
                "status": _as_str(payload.get("status")).strip() or TASK_STATUS_COMPLETED,
            })
        return {}

    def _release_lock_free(self, pending: PendingTask) -> None:
        if self._pending is pending:
            self._pending = None
        self._cancel_watchdog(pending)

    def _cancel_watchdog(self, pending: PendingTask) -> None:
        watchdog = pending.watchdog
        pending.watchdog = None
        if watchdog is not None and not watchdog.done():
            watchdog.cancel()

    async def _release(self, pending: PendingTask) -> None:
        async with self._pending_lock:
            self._release_lock_free(pending)

    # ==================================================================
    # Inbound events
    # ==================================================================

    async def handle_event(self, frame: SseFrame) -> None:
        """Route one SSE frame to a cue, and keep the task slot honest."""
        payload = frame.json_data()
        if payload is None:
            return
        # ``ev`` is contractually required on every payload and is the
        # authoritative discriminator; the SSE ``event:`` line is a mirror.
        event = _as_str(payload.get("ev")).strip() or frame.event

        session = _as_str(payload.get("session_id"))
        if session and session != self._session_id:
            # The bridge restarted between the status probe and the stream, or
            # status was unreachable. Adopt the new session and drop the old
            # one's state rather than attributing its events to old tasks.
            await self._invalidate_session_state()
            self._session_id = session
            client = self._client
            if client is not None:
                client.reset_cursor()

        self._last_event_at = time.monotonic()
        self._recent_events.append({"event": event, "data": payload})

        if event == "progress":
            await self._on_progress(payload)
        elif event == "done":
            await self._on_done(payload)
        elif event == "question":
            await self._on_question(payload)
        elif event == "state":
            await self._on_state(payload)
        # Unknown event types are ignored on purpose: the bridge may grow new
        # ones and a forward-compatible reader should not need a release.

    async def _on_progress(self, payload: Dict[str, Any]) -> None:
        text = _as_str(payload.get("text")).strip()
        if not text:
            return
        now = time.monotonic()
        if now - self._last_progress_at < self._progress_min_interval:
            # The bridge emits one progress frame per executed step; without a
            # floor a busy task floods the dialog context. Coalescing alone is
            # not enough — it only collapses frames still queued.
            return
        self._last_progress_at = now
        self._push_cue(
            "CUE_PROGRESS",
            ai_behavior="read",
            priority=PRIORITY_PROGRESS,
            coalesce_key="mcbot_progress",
            text=text[:400],
        )

    async def _on_done(self, payload: Dict[str, Any]) -> None:
        pending = self._pending
        if pending is not None and pending.awaiting_id:
            # Park it: the bridge takes its cursor before submitting, so a fast
            # done can beat the POST response, and until we know our task id we
            # cannot tell it apart from another task's.
            if len(pending.buffered) < _MAX_BUFFERED_EVENTS:
                pending.buffered.append(payload)
            return
        await self._apply_done(payload)

    async def _apply_done(self, payload: Dict[str, Any]) -> None:
        """Release the matching slot and narrate the outcome.

        ``status`` decides the framing. v1.0 can report completed / failed /
        cancelled / superseded, and only the first is success — presenting a
        failure or a cancellation as "done" is the same defect the sibling
        plugin documents for mc-agent.
        """
        task_id = payload.get("task_id")
        text = _as_str(payload.get("text")).strip()
        status = _as_str(payload.get("status")).strip().lower() or TASK_STATUS_COMPLETED

        pending = self._pending
        if pending is not None:
            ours = pending.task_id is None or (
                isinstance(task_id, int)
                and not isinstance(task_id, bool)
                and task_id == pending.task_id
            )
            if ours:
                await self._resolve_pending(pending, text, status=status)
            # else: another (e.g. in-game `@bot`-driven) task finished — our
            # slot stays claimed, but the outcome is still worth narrating.

        if status in TASK_STATUSES_SILENT:
            # Replaced or stopped on purpose; the LLM caused it and knows.
            # Cue'ing it invites a re-dispatch of work already abandoned.
            return
        if not text:
            return
        self._push_cue(
            "CUE_DONE" if status == TASK_STATUS_COMPLETED else "CUE_DONE_FAILED",
            ai_behavior="respond",
            priority=PRIORITY_COMPLETION,
            coalesce_key="mcbot_completion",
            text=text[:600],
            status=status,
        )
    async def _resolve_pending(
        self, pending: PendingTask, text: str, *, status: str = TASK_STATUS_COMPLETED
    ) -> None:
        async with self._pending_lock:
            self._release_lock_free(pending)
        if not pending.event.is_set():
            pending.result = {
                "status": status,
                "query": pending.text,
                "text": text,
                "task_id": pending.task_id,
            }
            pending.event.set()

    async def _on_question(self, payload: Dict[str, Any]) -> None:
        question_id = _as_str(payload.get("question_id")).strip()
        text = _as_str(payload.get("text")).strip()
        if not question_id or not text:
            return
        if len(self._pending_questions) >= MAX_PENDING_QUESTIONS:
            oldest = min(
                self._pending_questions,
                key=lambda qid: self._pending_questions[qid].get("asked_at", 0.0),
            )
            self._pending_questions.pop(oldest, None)
        self._pending_questions[question_id] = {
            "text": text,
            "task_id": payload.get("task_id"),
            "asked_at": time.time(),
        }
        self._push_cue(
            "CUE_QUESTION",
            ai_behavior="respond",
            priority=PRIORITY_QUESTION,
            coalesce_key="mcbot_question",
            text=text[:400],
        )

    async def _on_state(self, payload: Dict[str, Any]) -> None:
        text = _as_str(payload.get("text")).strip()
        if not text:
            return
        self._push_cue(
            "CUE_STATE",
            ai_behavior="read",
            priority=PRIORITY_STATE,
            coalesce_key="mcbot_state",
            text=text[:400],
        )

    # ==================================================================
    # Outbound tool support
    # ==================================================================

    async def ask(self, text: str) -> Dict[str, Any]:
        """Ask the companion a question directly (synchronous, slow)."""
        client = self._client
        if client is None:
            return {
                "output": {"error": prompts.t("ERR_UNREACHABLE", lang=self._lang)},
                "is_error": True,
                "error": "BRIDGE_NOT_STARTED",
            }
        try:
            payload = await client.ask(text)
        except McbotBridgeError as exc:
            return self.error_envelope(exc, query=text)
        answer = _as_str(payload.get("answer")).strip()
        if not answer:
            return prompts.t("ASK_NO_ANSWER", lang=self._lang)
        return prompts.t("ASK_ANSWER", lang=self._lang, answer=answer)

    async def answer(self, question_id: str, text: str) -> Dict[str, Any]:
        """Relay an answer to a reverse question the companion asked."""
        client = self._client
        if client is None:
            return {
                "output": {"error": prompts.t("ERR_UNREACHABLE", lang=self._lang)},
                "is_error": True,
                "error": "BRIDGE_NOT_STARTED",
            }
        try:
            await client.answer(question_id, text)
        except McbotBridgeError as exc:
            if exc.status == 404:
                return {
                    "output": {
                        "error": prompts.t("ANSWER_UNKNOWN_QUESTION", lang=self._lang)
                    },
                    "is_error": True,
                    "error": "QUESTION_EXPIRED",
                }
            return self.error_envelope(exc, query=question_id)
        self._pending_questions.pop(question_id, None)
        return prompts.t("ANSWER_OK", lang=self._lang)

    async def cancel(self, task_id: Optional[int] = None) -> Dict[str, Any]:
        """Cancel the running task."""
        if self._pending is None and task_id is None:
            return prompts.t("CANCEL_NOTHING", lang=self._lang)
        client = self._client
        if client is None:
            return {
                "output": {"error": prompts.t("ERR_UNREACHABLE", lang=self._lang)},
                "is_error": True,
                "error": "BRIDGE_NOT_STARTED",
            }
        try:
            await client.cancel_task(task_id)
        except McbotBridgeError as exc:
            return self.error_envelope(exc, query=str(task_id or ""))
        pending = self._pending
        if pending is not None:
            await self._resolve_pending(pending, "", status="cancelled")
        return prompts.t("CANCEL_OK", lang=self._lang)

    # ==================================================================
    # Status
    # ==================================================================

    def is_connected(self) -> bool:
        """True while the SSE event stream is live."""
        client = self._client
        return bool(client is not None and client.is_streaming)

    def has_pending_task(self) -> bool:
        return self._pending is not None

    def current_task_text(self) -> str:
        pending = self._pending
        return pending.text if pending is not None else ""

    async def probe_status(self) -> Dict[str, Any]:
        """Fetch a live status snapshot; ``{}`` when unreachable."""
        client = self._client
        if client is None:
            return {}
        try:
            payload = await client.get_status()
        except McbotBridgeError as exc:
            self._last_stream_error = exc.code
            return {}
        return payload

    def get_status(self) -> Dict[str, Any]:
        """Local snapshot for the status entry — never performs I/O."""
        pending = self._pending
        client = self._client
        return {
            "connected": self.is_connected(),
            "base_url": self._base_url,
            "session_id": self._session_id,
            "contract_version": self._contract_version,
            "token_path": self._token_path,
            "token_ok": bool(self._token),
            "token_error": self._token_error,
            "stream_enabled": self._stream_events,
            "stream_error": (
                client.last_error if client is not None else ""
            ) or self._last_stream_error,
            "pending_task": pending.text if pending is not None else "",
            "pending_task_id": pending.task_id if pending is not None else None,
            "pending_questions": len(self._pending_questions),
            "last_event_age_seconds": (
                round(time.monotonic() - self._last_event_at, 1)
                if self._last_event_at
                else None
            ),
        }

    # ==================================================================
    # Cue emission
    # ==================================================================

    def _push_cue(
        self,
        key: str,
        *,
        ai_behavior: str,
        priority: int,
        coalesce_key: str,
        **fmt: Any,
    ) -> None:
        """Format and push one cue. Never raises into the caller.

        ``visibility=[]`` means the user does not see the cue parts themselves:
        the dialog LLM absorbs them and, for ``respond`` cues, voices the
        outcome in its own words on the turn it opens. That is deliberate — the
        character *talking about* the companion is the product, and the sibling
        plugin converged on the same shape.
        """
        try:
            text = prompts.t(
                key, lang=self._lang, internal_gag=prompts.gag(self._lang), **fmt
            )
        except Exception as exc:
            self._log_warning("cue {} could not be formatted: {}", key, type(exc).__name__)
            return
        if not text:
            return
        try:
            self._push_message(
                source="mcbot_bridge",
                visibility=[],
                ai_behavior=ai_behavior,
                parts=[{"type": "text", "text": text}],
                priority=priority,
                coalesce_key=coalesce_key,
            )
        except Exception as exc:
            # Delivery is best-effort; a failure here must not corrupt the
            # event pipeline that called us.
            self._log_warning("cue {} push failed: {}", key, type(exc).__name__)
            return
        # Logged on success too: without this the only evidence that a cue left
        # the plugin lives in the host's log, which makes any "the character
        # never mentioned it" report impossible to diagnose from this side.
        # Key/behaviour/size only — the body is conversational content and is
        # never written to the log.
        self._log_debug(
            "cue {} pushed ({} prio={} {} chars)",
            key, ai_behavior, priority, len(text),
        )

    def error_envelope(self, exc: McbotBridgeError, *, query: str = "") -> Dict[str, Any]:
        """Translate a bridge failure into a model-facing error envelope.

        ``output`` carries something the character can say out loud; ``error``
        carries the machine code. The exception's own text is never included —
        the model writes tool results into conversation history permanently, so
        a host name, a port or the bridge's operator-facing prose would be
        persisted in the transcript.
        """
        if isinstance(exc, McbotUnreachable):
            message = prompts.t("ERR_UNREACHABLE", lang=self._lang)
        elif exc.code in prompts.BRIDGE_ERROR_MESSAGES:
            message = prompts.bridge_error(exc.code, lang=self._lang)
        else:
            # Unknown category (the bridge may add codes): degrade to a generic
            # line rather than leaking the code itself.
            message = prompts.bridge_error(
                exc.code, lang=self._lang, status=exc.status
            )
        self._last_error_code = exc.code
        output: Dict[str, Any] = {"error": message}
        if query:
            output["query"] = query[:200]
        return {"output": output, "is_error": True, "error": exc.code}

    # ==================================================================
    # Logging — metadata only. Task text, answers and reverse questions are
    # conversational content and never go through the logger.
    # ==================================================================

    def _log_info(self, msg: str, *args: Any) -> None:
        self._emit("info", msg, *args)

    def _log_debug(self, msg: str, *args: Any) -> None:
        self._emit("debug", msg, *args)

    def _log_warning(self, msg: str, *args: Any) -> None:
        self._emit("warning", msg, *args)

    def _emit(self, level: str, msg: str, *args: Any) -> None:
        if self.logger is None:
            return
        try:
            getattr(self.logger, level)("[Mcbot] " + msg, *args)
        except Exception:
            pass


__all__ = [
    "McbotService",
    "PendingTask",
    "DEFAULT_BASE_URL",
    "default_game_dir",
    "normalise_base_url",
    "task_looks_like_identifier",
]
