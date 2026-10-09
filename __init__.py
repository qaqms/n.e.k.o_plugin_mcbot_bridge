"""mcbot companion bridge plugin.

Bridges a local `mcbot <https://github.com/qaqms/mcbot>`_ companion (a Fabric
mod fake-player that runs its own LLM brain) into N.E.K.O. over the mod's
loopback REST + SSE bridge on ``127.0.0.1:57121``.

Where the actions live, and why:

* :func:`mcbot_task`, :func:`mcbot_ask`, :func:`mcbot_answer`,
  :func:`mcbot_cancel` are ``@llm_tool`` — the *dialog* LLM calls them
  directly, because "go chop some wood" has to be decided by whichever model is
  actually mid-conversation, and the tool needs the model's own typed arguments
  and a synchronous answer.
* :func:`mcbot_status` and :func:`mcbot_reload_config` are ``@plugin_entry`` —
  these are facts and settings, which the *analyzer* asks for and the plugin
  panel can call. They are deliberately not LLM tools: dispatching a status
  query as if it were an action is the canonical way to make an agent loop
  against itself, and a fact query does not deserve a slot in the dialog
  model's tool list.

The two audiences are mutually exclusive by construction: ``@plugin_entry``
never reaches main_server's tool registry (only ``LLM_TOOL_REGISTER``, sent for
``@llm_tool``, does), and the analyzer's plugin view has every ``__llm_tool__``
-prefixed entry stripped out (``app/agent_server/api_runtime.py``). This is the
same split the sibling ``game_agent_minecraft`` plugin uses.

The third channel is the reverse one: the bridge's SSE stream is translated
into ``push_message`` cues, so the character can react to her companion's
progress and relay its questions without anyone polling for them.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    ui,
)

from . import prompts
from .service import McbotService, task_looks_like_identifier

# ---------------------------------------------------------------------------
# JSON Schemas are module-level constants rather than inline literals so the
# plugin's own introspection (the status entry, unit tests) can reference the
# exact contract the model sees.
# ---------------------------------------------------------------------------

MCBOT_TASK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": (
                "A plain-language instruction for the companion, worded the way "
                "the user said it. When the user gave the instruction, relay "
                "their exact wording — do not translate, split, add steps, "
                "quantities or coordinates, or invent a target. Never pass a "
                "bare function or tool name."
            ),
        },
        "overwrite": {
            "type": "boolean",
            "description": (
                "Set true only to interrupt an in-flight task: the user "
                "corrected or cancelled it, or the current task is plainly "
                "stuck. Default false, which returns a 'busy' answer and "
                "leaves the in-flight task running."
            ),
        },
    },
    "required": ["text"],
}

MCBOT_TASK_DESCRIPTION = (
    "Send one instruction to your in-game companion and return immediately "
    "with an acknowledgement. Fire-and-forget: the outcome arrives on its own "
    "as progress and completion cues, so never state or guess a result from "
    "this call alone.\n\n"
    "This tool dispatches actions; it is not an awareness or status source. A "
    "progress line, a completion cue, or a question from the companion is "
    "context to react to — never itself something to dispatch. Before "
    "dispatching, account for what the companion may already be doing; if her "
    "last instruction may still be running, do not send another.\n\n"
    "Use it when the user asks the companion to do something new that has not "
    "already been sent. Do NOT use it for questions about her state or what "
    "she is carrying — those are answered by the mcbot_status entry — and do "
    "not re-send an earlier instruction merely because a cue arrived.\n\n"
    "Parameters:\n"
    "  text (string, required): the instruction, in the user's own wording "
    "when they gave one.\n"
    "  overwrite (bool, default false): true only to interrupt the current "
    "task; see the schema description for when that is warranted."
)

MCBOT_ASK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "A direct question for the companion.",
        },
    },
    "required": ["text"],
}

MCBOT_ASK_DESCRIPTION = (
    "Ask the companion a direct question and wait for her spoken answer. Use "
    "it for a one-off question the user wants asked of her specifically. It "
    "blocks until she answers, so prefer mcbot_task for anything that is an "
    "action rather than a question. If she is mid-task she may not answer."
)

MCBOT_ANSWER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "question_id": {
            "type": "string",
            "description": "The id of the open question, from the question cue.",
        },
        "text": {
            "type": "string",
            "description": "The user's answer, in their own words.",
        },
    },
    "required": ["question_id", "text"],
}

MCBOT_ANSWER_DESCRIPTION = (
    "Answer a question the companion asked through a question cue. Use the "
    "question_id from that cue and pass the user's answer as they gave it — do "
    "not answer on the user's behalf. Only call this when the user actually "
    "answered; if they did not, leave the question open."
)

MCBOT_CANCEL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "integer",
            "description": (
                "Optional id of a specific task to cancel. Omit to cancel "
                "whatever is currently running."
            ),
        },
    },
}

MCBOT_CANCEL_DESCRIPTION = (
    "Tell the companion to stop what she is doing. Use it when the user says "
    "to stop or abort. To redirect her to a different action instead, call "
    "mcbot_task with overwrite=true — that both stops the current task and "
    "starts the new one."
)


@neko_plugin
class McbotBridgePlugin(NekoPluginBase):
    """Plugin facade — lifecycle wiring and the two tool surfaces.

    All game-facing behaviour lives in :class:`McbotService`; this class holds
    only SDK integration so the service stays testable without the plugin SDK.
    """

    def __init__(self, ctx):
        super().__init__(ctx)
        # Follow the global NEKO_LOG_LEVEL (default INFO) rather than pinning a
        # level, so DEBUG actually shows up when someone is diagnosing a bridge
        # that will not connect.
        _log_level = (os.environ.get("NEKO_LOG_LEVEL") or "INFO").strip().upper()
        try:
            self.file_logger = self.enable_file_logging(log_level=_log_level)
        except ValueError:
            self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger

        self._cfg: Dict[str, Any] = {}
        self._lang: str = prompts.DEFAULT_LANG
        self._service = McbotService(
            logger=self.logger,
            push_message_fn=self.push_message,
        )
        # The SDK runs ``@lifecycle(id="startup")`` inside a transient
        # ``asyncio.run(...)`` (plugin/core/host.py), which closes the loop as
        # soon as the handler returns and cancels every ``asyncio.create_task``
        # started beneath it. Starting the bridge there would silently produce
        # a plugin that never streams. The real start is deferred to
        # ``_on_command_loop_start`` (the host's long-lived loop) with a lazy
        # first-call fallback, guarded so concurrent callers cannot double-spawn.
        self._service_lazy_started: bool = False
        self._service_start_lock: asyncio.Lock = asyncio.Lock()

    async def _ensure_service_started(self, *, connect_wait_s: float = 2.0) -> bool:
        """Idempotently start the bridge; returns whether it is streaming.

        A ``False`` return is not an error — it is the normal state when
        Minecraft is closed, since mcbot only serves its bridge while a world
        is loaded. Callers that need a live bridge let the dispatch itself
        produce the user-facing explanation.
        """
        async with self._service_start_lock:
            if not self._service_lazy_started:
                try:
                    await self._service.start()
                    self._service_lazy_started = True
                    self.logger.info("[lazy-start] bridge service started")
                except Exception as exc:
                    # Leave the flag clear so the next call retries.
                    self.logger.warning(
                        "[lazy-start] start failed; will retry — {}: {}",
                        type(exc).__name__, exc,
                    )
                    return False

            deadline = time.monotonic() + connect_wait_s
            while time.monotonic() < deadline:
                if self._service.is_connected():
                    return True
                await asyncio.sleep(0.05)
            return self._service.is_connected()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @lifecycle(id="startup")
    async def startup(self, **_):
        # Configuration only — see the note in __init__ for why the service
        # must not be started from this hook.
        cfg = await self.config.dump(timeout=5.0)
        cfg = cfg if isinstance(cfg, dict) else {}
        section = cfg.get("mcbot")
        self._cfg = section if isinstance(section, dict) else {}
        self._lang = prompts.user_lang()
        self._service.set_lang(self._lang)
        self._service.configure(self._cfg)
        return Ok({"status": "ready", "result": self._service.get_status()})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        if self._service_lazy_started:
            try:
                await self._service.stop()
            except Exception as exc:
                self.logger.warning(
                    "[shutdown] stop raised — {}: {}", type(exc).__name__, exc
                )
            self._service_lazy_started = False
        return Ok({"status": "shutdown"})

    async def _on_command_loop_start(self) -> None:
        """Eager-start the bridge on the host's long-lived command loop.

        The SDK calls this from inside the command dispatch loop, on the same
        loop that later runs every entry and tool handler, so the stream task,
        the locks and the events created here all bind to the loop that will
        use them. Starting lazily from a handler also works (both paths are
        kept), but eager start removes the first-call race entirely.
        """
        try:
            await self._ensure_service_started()
        except Exception as exc:
            self.logger.warning(
                "[eager-start] failed; lazy fallback remains — {}: {}",
                type(exc).__name__, exc,
            )

    # ------------------------------------------------------------------
    # @llm_tool — the dialog LLM's direct surface
    # ------------------------------------------------------------------

    @llm_tool(
        name="mcbot_task",
        description=MCBOT_TASK_DESCRIPTION,
        parameters=MCBOT_TASK_SCHEMA,
        # Held at the registry maximum even though the handler returns in
        # milliseconds: the wrapper must never cap below a raised
        # http_timeout, and keeping the ceiling removes that failure mode.
        timeout=300.0,
    )
    async def mcbot_task(self, *, text: Any = None, overwrite: Any = False, **_):
        # ``text`` is typed ``Any`` rather than ``str`` so a model that
        # violates the schema (omits it, sends null, sends a number) lands in
        # this body and gets a structured hint, instead of the SDK raising a
        # TypeError it would surface as a generic tool failure.
        if not isinstance(text, str) or not text.strip():
            return prompts.t("TASK_SCHEMA_ERROR", lang=self._lang)
        task_text = text.strip()
        if task_looks_like_identifier(task_text):
            return prompts.t("TASK_IDENTIFIER_ERROR", lang=self._lang)

        await self._ensure_service_started()
        claimed = await self._service.try_claim_pending(
            task_text, overwrite=overwrite is True
        )
        if claimed is None:
            current = self._service.current_task_text() or prompts.t(
                "LABEL_IDLE", lang=self._lang
            )
            return prompts.t("TASK_BUSY_HINT", lang=self._lang, current=current[:80])

        envelope = await self._service.dispatch_claimed(claimed)
        if envelope:
            return envelope
        return prompts.t("TASK_DISPATCHED_ACK", lang=self._lang)

    @llm_tool(
        name="mcbot_ask",
        description=MCBOT_ASK_DESCRIPTION,
        parameters=MCBOT_ASK_SCHEMA,
        # mcbot answers or gives up with a 504 at 135s; the client's own budget
        # (ask_timeout_seconds) sits just above that. Keep the wrapper ceiling
        # at the registry max so raising the configured budget cannot silently
        # trip a wrapper timeout instead.
        timeout=300.0,
    )
    async def mcbot_ask(self, *, text: Any = None, **_):
        if not isinstance(text, str) or not text.strip():
            return prompts.t("ASK_SCHEMA_ERROR", lang=self._lang)
        await self._ensure_service_started()
        return await self._service.ask(text.strip())

    @llm_tool(
        name="mcbot_answer",
        description=MCBOT_ANSWER_DESCRIPTION,
        parameters=MCBOT_ANSWER_SCHEMA,
        timeout=30.0,
    )
    async def mcbot_answer(self, *, question_id: Any = None, text: Any = None, **_):
        if not isinstance(question_id, str) or not question_id.strip():
            return prompts.t("ANSWER_SCHEMA_ERROR", lang=self._lang)
        if not isinstance(text, str) or not text.strip():
            return prompts.t("ANSWER_SCHEMA_ERROR", lang=self._lang)
        await self._ensure_service_started()
        return await self._service.answer(question_id.strip(), text.strip())

    @llm_tool(
        name="mcbot_cancel",
        description=MCBOT_CANCEL_DESCRIPTION,
        parameters=MCBOT_CANCEL_SCHEMA,
        timeout=30.0,
    )
    async def mcbot_cancel(self, *, task_id: Any = None, **_):
        # A model may send the id as a string; anything that is not a real int
        # means "cancel whatever is running" rather than a hard failure.
        tid = task_id if isinstance(task_id, int) and not isinstance(task_id, bool) else None
        await self._ensure_service_started()
        return await self._service.cancel(tid)

    # ------------------------------------------------------------------
    # @plugin_entry — analyzer / panel / CLI surface
    # ------------------------------------------------------------------

    @ui.action(id="mcbot_status", label="刷新状态")
    @plugin_entry(
        id="mcbot_status",
        name="查询 mcbot 同伴状态",
        description=(
            "查询 mcbot 同伴是否在线、她当前在做什么、有没有待你回答的问题。"
            "适合在对话里明确问到同伴的状态时调用——「同伴在干嘛」「她在哪」"
            "「她还在挖吗」「连上了吗」。"
            "不要在对话是要让同伴做事（过来、挖矿、建造、跟随等）时调用——"
            "那是 mcbot_task 的职责。"
        ),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
    )
    async def mcbot_status(self, **_):
        try:
            await self._ensure_service_started()
            status = self._service.get_status()
            connected = bool(status.get("connected"))
            if not connected:
                # Only pay for a REST probe when the stream is silent — that
                # is the one case where "stream down" might still mean "bridge
                # up" (streaming disabled, or a stream that has not re-attached
                # yet). The streaming case is authoritative and free.
                live = await self._service.probe_status()
                if live:
                    connected = True
                    status["companion"] = live
            status["connected"] = connected

            label = prompts.t(
                "LABEL_CONNECTED" if connected else "LABEL_DISCONNECTED",
                lang=self._lang,
            )
            task = status.get("pending_task") or prompts.t("LABEL_IDLE", lang=self._lang)
            questions = status.get("pending_questions") or 0
            status["summary"] = prompts.t(
                "STATUS_SUMMARY",
                lang=self._lang,
                connected_label=label,
                task=task[:80],
                questions=questions,
            )
            if not connected and not status.get("token_ok"):
                # Only meaningful while we are failing to reach the bridge: a
                # live stream proves the token works, whatever the file says
                # now (it may have been rotated out from under us).
                status["summary"] += " " + prompts.t("NO_TOKEN_HINT", lang=self._lang)
            return Ok(status)
        except Exception as exc:
            return Err(f"{type(exc).__name__}: {exc}")

    @ui.action(id="mcbot_reload_config", label="重载配置")
    @plugin_entry(
        id="mcbot_reload_config",
        name="重载 mcbot 桥接配置",
        description=(
            "重新读取 plugin.toml [mcbot] 配置并重读 token 文件。base_url、"
            "game_dir、token_file、重连间隔或事件流开关有改动时会重建连接；"
            "纯超时类改动即时生效。"
        ),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
    )
    async def mcbot_reload_config(self, **_):
        try:
            await self._ensure_service_started()
            cfg = await self.config.dump(timeout=5.0)
            cfg = cfg if isinstance(cfg, dict) else {}
            section = cfg.get("mcbot")
            self._cfg = section if isinstance(section, dict) else {}
            restarted = await self._service.reload_config_live(self._cfg)
            return Ok({
                "summary": (
                    "config reloaded with transport restart"
                    if restarted
                    else "config reloaded (live)"
                ),
                "transport_restarted": restarted,
                "result": self._service.get_status(),
            })
        except Exception as exc:
            return Err(f"{type(exc).__name__}: {exc}")
