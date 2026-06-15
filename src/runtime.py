from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import time
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

from src.config import Settings
from src.member_identity import (
    MemberIdentity,
    MemberIdentityContext,
    RetrievalIntent,
    build_member_identity_context,
    build_retrieval_intent,
    identity_status,
    render_author_names,
    render_member_identity_block,
)
from src.message_normalization import MessageNormalizationInput, normalize_message


EMPTY_QUERY_REPLY = "你叫我了，但還沒給我問題。"
LLM_FAILED_REPLY = "我剛剛處理時出了一點問題，等一下再叫我一次。"
DISCORD_SINGLE_MESSAGE_LIMIT = 2000


@dataclass(frozen=True)
class RuntimeMessage:
    message_id: int
    created_at: datetime
    author_id: int
    raw_content: str
    guild_id: int | None
    channel_id: int
    is_dm: bool
    is_thread: bool
    is_bot_author: bool
    is_bot_mentioned: bool
    bot_user_id: int
    has_attachments: bool = False
    has_stickers: bool = False
    has_embeds: bool = False
    is_system_message: bool = False
    caller_display_name: str | None = None
    mentioned_members: tuple[MemberIdentity, ...] = ()
    unknown_member_ids: tuple[int, ...] = ()
    member_identity_status: tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionState:
    session_id: str
    turns: list[dict[str, Any]]
    retrieved_chunk_keys: list[str]
    retrieved_summary_keys: list[str]
    last_rag_query: str | None
    is_new: bool = False


@dataclass(frozen=True)
class RetrievedSummary:
    summary_key: str
    summary_text: str


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_key: str
    chunk_text: str
    source: str
    summary_key: str | None = None


@dataclass(frozen=True)
class RetrievalContext:
    summaries: list[RetrievedSummary]
    chunks: list[RetrievedChunk]
    status: str
    retrieval_filter_status: tuple[str, ...] = field(default_factory=tuple)

    @property
    def summary_keys(self) -> list[str]:
        return [summary.summary_key for summary in self.summaries]

    @property
    def chunk_keys(self) -> list[str]:
        return [chunk.chunk_key for chunk in self.chunks]

    @property
    def has_context(self) -> bool:
        return bool(self.summaries or self.chunks)


@dataclass(frozen=True)
class RetrievalResolution:
    context: RetrievalContext
    router_decision: str


@dataclass(frozen=True)
class RouterDecision:
    should_retrieve: bool
    log_value: str


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    token_input: int | None = None
    token_output: int | None = None
    estimated_cost: str | None = None


@dataclass(frozen=True)
class RuntimeResult:
    status: str
    request_id: str | None = None
    reply: str | None = None


class RuntimeStore(Protocol):
    def upsert_raw_message(
        self,
        message: RuntimeMessage,
        normalized_content: str,
        is_rag_eligible: bool,
    ) -> None:
        ...

    def get_or_create_active_session(
        self, channel_id: int, now: datetime, settings: Settings
    ) -> SessionState:
        ...

    def delete_empty_session(self, session_id: str) -> None:
        ...

    def retrieve_context(
        self,
        query_embedding: list[float],
        settings: Settings,
        intent: RetrievalIntent,
    ) -> RetrievalContext:
        ...

    def load_context_by_keys(
        self, summary_keys: list[str], chunk_keys: list[str]
    ) -> RetrievalContext:
        ...

    def append_successful_turns(
        self,
        session: SessionState,
        message: RuntimeMessage,
        user_query: str,
        assistant_reply: str,
        retrieval: RetrievalContext,
        last_rag_query: str | None,
        now: datetime,
        settings: Settings,
    ) -> None:
        ...


class EmbeddingClient(Protocol):
    def embed_query(self, query: str, *, model: str, timeout_seconds: float) -> list[float]:
        ...


class AnswerClient(Protocol):
    def answer(
        self,
        prompt: str,
        *,
        model: str,
        timeout_seconds: float,
        max_output_tokens: int,
    ) -> LLMResult:
        ...


class RouterClient(Protocol):
    def should_retrieve(
        self,
        prompt: str,
        *,
        model: str,
        timeout_seconds: float,
        max_output_tokens: int,
    ) -> bool:
        ...


class UnconfiguredLLMClient:
    def answer(
        self,
        prompt: str,
        *,
        model: str,
        timeout_seconds: float,
        max_output_tokens: int,
    ) -> LLMResult:
        raise RuntimeError("Runtime LLM client is not configured")


class UnconfiguredRouterClient:
    def should_retrieve(
        self,
        prompt: str,
        *,
        model: str,
        timeout_seconds: float,
        max_output_tokens: int,
    ) -> bool:
        raise RuntimeError("Runtime router client is not configured")


class UnconfiguredEmbeddingClient:
    def embed_query(self, query: str, *, model: str, timeout_seconds: float) -> list[float]:
        raise RuntimeError("Runtime embedding client is not configured")


SendReply = Callable[[str], Awaitable[None]]


class MentionRuntime:
    def __init__(
        self,
        settings: Settings,
        store: RuntimeStore,
        embedding_client: EmbeddingClient | None = None,
        answer_client: AnswerClient | None = None,
        fallback_client: AnswerClient | None = None,
        router_client: RouterClient | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.embedding_client = embedding_client or UnconfiguredEmbeddingClient()
        self.answer_client = answer_client or UnconfiguredLLMClient()
        self.fallback_client = fallback_client or self.answer_client
        self.router_client = router_client or UnconfiguredRouterClient()
        self.logger = logger or logging.getLogger("group_memory.runtime")
        self.answer_system_prompt = settings.answer_system_prompt_path.read_text(
            encoding="utf-8"
        )
        self.router_prompt = settings.router_prompt_path.read_text(encoding="utf-8")

    async def handle_message(
        self, message: RuntimeMessage, send_reply: SendReply
    ) -> RuntimeResult:
        if self._should_ignore(message):
            return RuntimeResult(status="ignored")

        normalized = normalize_message(self._normalization_input(message))
        raw_upsert_failed = False
        try:
            await _run_blocking(
                self.settings.raw_upsert_timeout_seconds,
                self.store.upsert_raw_message,
                message,
                normalized.normalized_content,
                normalized.is_rag_eligible,
            )
        except Exception as exc:
            raw_upsert_failed = True
            self._log_event(
                event="raw_upsert_failed",
                status="failed",
                message=message,
                failure_flags=["raw_upsert_failed"],
                error=exc,
            )

        if not message.is_bot_mentioned:
            return RuntimeResult(status="raw_upserted")

        request_id = str(uuid4())
        user_query = normalized.normalized_content
        if not user_query:
            await send_reply(EMPTY_QUERY_REPLY)
            return RuntimeResult(
                status="empty_query", request_id=request_id, reply=EMPTY_QUERY_REPLY
            )

        started = time.monotonic()
        now = _utc_now()
        failure_flags = ["raw_upsert_failed"] if raw_upsert_failed else []
        identity = self._member_identity(message)
        retrieval_intent = build_retrieval_intent(user_query, identity, now)
        session = await self._get_session(message, now, failure_flags)
        retrieval_resolution = await self._resolve_retrieval(
            session, user_query, retrieval_intent, failure_flags
        )
        retrieval = retrieval_resolution.context
        prompt = assemble_answer_prompt(
            self.answer_system_prompt,
            retrieval,
            session.turns if session else [],
            user_query,
            self.settings,
            identity,
        )
        llm_result = await self._answer(prompt, failure_flags)
        reply = _truncate_discord_reply(llm_result.text if llm_result else LLM_FAILED_REPLY)

        send_succeeded = False
        for attempt in range(self.settings.discord_send_retry_count + 1):
            try:
                await send_reply(reply)
                send_succeeded = True
                break
            except Exception as exc:
                if attempt >= self.settings.discord_send_retry_count:
                    failure_flags.append("discord_send_failed")
                    self._log_event(
                        event="discord_send_failed",
                        status="failed",
                        request_id=request_id,
                        message=message,
                        session=session,
                        retrieval=retrieval,
                        router_decision=retrieval_resolution.router_decision,
                        identity=identity,
                        retrieval_intent=retrieval_intent,
                        llm_result=llm_result,
                        failure_flags=failure_flags,
                        error=exc,
                        started=started,
                    )

        if not send_succeeded:
            if session and session.is_new:
                await self._delete_empty_session(session.session_id, failure_flags)
            return RuntimeResult(status="discord_send_failed", request_id=request_id)

        if llm_result and session:
            last_rag_query = user_query if retrieval.status.startswith("retrieved") else session.last_rag_query
            await self._append_turns(
                session,
                message,
                user_query,
                reply,
                retrieval,
                last_rag_query,
                now,
                failure_flags,
            )
        elif session and session.is_new and not llm_result:
            await self._delete_empty_session(session.session_id, failure_flags)

        status = "answered" if llm_result else "llm_failed"
        self._log_event(
            event="runtime_request_finished",
            status=status,
            request_id=request_id,
            message=message,
            session=session,
            retrieval=retrieval,
            router_decision=retrieval_resolution.router_decision,
            identity=identity,
            retrieval_intent=retrieval_intent,
            llm_result=llm_result,
            failure_flags=failure_flags,
            started=started,
        )
        return RuntimeResult(status=status, request_id=request_id, reply=reply)

    def _should_ignore(self, message: RuntimeMessage) -> bool:
        return (
            message.is_bot_author
            or message.is_dm
            or message.is_thread
            or message.guild_id != self.settings.guild_id
            or message.channel_id != self.settings.channel_id
        )

    def _normalization_input(self, message: RuntimeMessage) -> MessageNormalizationInput:
        return MessageNormalizationInput(
            message_id=message.message_id,
            created_at=message.created_at,
            author_id=message.author_id,
            raw_content=message.raw_content,
            is_bot_author=message.is_bot_author,
            is_dm=message.is_dm,
            is_thread=message.is_thread,
            is_wrong_guild=message.guild_id != self.settings.guild_id,
            is_wrong_channel=message.channel_id != self.settings.channel_id,
            is_bot_mentioned=message.is_bot_mentioned,
            has_attachments=message.has_attachments,
            has_stickers=message.has_stickers,
            has_embeds=message.has_embeds,
            bot_user_id=message.bot_user_id,
            is_system_message=message.is_system_message,
        )

    def _member_identity(self, message: RuntimeMessage) -> MemberIdentityContext:
        return build_member_identity_context(
            caller_id=message.author_id,
            caller_display_name=message.caller_display_name,
            mentioned_members=message.mentioned_members,
            unknown_member_ids=message.unknown_member_ids,
            status=message.member_identity_status,
        )

    async def _get_session(
        self, message: RuntimeMessage, now: datetime, failure_flags: list[str]
    ) -> SessionState | None:
        try:
            return await _run_blocking(
                self.settings.session_db_timeout_seconds,
                self.store.get_or_create_active_session,
                message.channel_id, now, self.settings
            )
        except Exception:
            failure_flags.append("session_db_failed")
            return None

    async def _resolve_retrieval(
        self,
        session: SessionState | None,
        user_query: str,
        retrieval_intent: RetrievalIntent,
        failure_flags: list[str],
    ) -> RetrievalResolution:
        should_retrieve = True
        router_decision = "not_needed"
        if session is None:
            router_decision = "not_needed_stateless"
        elif not session.retrieved_chunk_keys or not session.last_rag_query:
            router_decision = "not_needed_missing_provenance"
        if session and session.retrieved_chunk_keys and session.last_rag_query:
            router_decision_result = await self._router_decision(
                session, user_query, failure_flags
            )
            should_retrieve = router_decision_result.should_retrieve
            router_decision = router_decision_result.log_value
        if session and not should_retrieve:
            try:
                retrieval = await _run_blocking(
                    self.settings.retrieval_db_query_timeout_seconds,
                    self.store.load_context_by_keys,
                    session.retrieved_summary_keys, session.retrieved_chunk_keys
                )
                return RetrievalResolution(retrieval, router_decision)
            except Exception:
                failure_flags.append("retrieval_context_missing")
                return RetrievalResolution(
                    RetrievalContext([], [], "retrieval_context_missing"),
                    router_decision,
                )
        try:
            embedding = await _run_blocking(
                self.settings.query_embedding_timeout_seconds,
                self.embedding_client.embed_query,
                user_query,
                model=self.settings.embedding_model,
                timeout_seconds=self.settings.query_embedding_timeout_seconds,
            )
            retrieval = await _run_blocking(
                self.settings.retrieval_db_query_timeout_seconds,
                self.store.retrieve_context,
                embedding,
                self.settings,
                retrieval_intent,
            )
            if not retrieval.has_context and retrieval.status == "retrieved":
                return RetrievalResolution(
                    RetrievalContext(
                        [],
                        [],
                        "retrieval_empty",
                        retrieval.retrieval_filter_status,
                    ),
                    router_decision,
                )
            return RetrievalResolution(retrieval, router_decision)
        except Exception:
            failure_flags.append("retrieval_failed")
            return RetrievalResolution(
                RetrievalContext([], [], "retrieval_failed"),
                router_decision,
            )

    async def _router_decision(
        self, session: SessionState, user_query: str, failure_flags: list[str]
    ) -> RouterDecision:
        prompt = assemble_router_prompt(
            self.router_prompt, session, user_query, self.settings.router_session_turns
        )
        try:
            should_retrieve = await _run_blocking(
                self.settings.router_timeout_seconds,
                self.router_client.should_retrieve,
                prompt,
                model=self.settings.router_model,
                timeout_seconds=self.settings.router_timeout_seconds,
                max_output_tokens=self.settings.router_max_output_tokens,
            )
            return RouterDecision(
                should_retrieve=should_retrieve,
                log_value="retrieve" if should_retrieve else "reuse",
            )
        except Exception:
            failure_flags.append("router_failed")
            return RouterDecision(
                should_retrieve=False,
                log_value="failed_defaulted_reuse",
            )

    async def _answer(self, prompt: str, failure_flags: list[str]) -> LLMResult | None:
        try:
            result = await _run_blocking(
                self.settings.answer_timeout_seconds,
                self.answer_client.answer,
                prompt,
                model=self.settings.answer_model,
                timeout_seconds=self.settings.answer_timeout_seconds,
                max_output_tokens=self.settings.answer_max_output_tokens,
            )
            return _with_estimated_cost(
                result,
                self.settings.runtime_answer_input_price_per_1m_tokens,
                self.settings.runtime_answer_output_price_per_1m_tokens,
            )
        except Exception:
            failure_flags.append("answer_failed")
        try:
            result = await _run_blocking(
                self.settings.fallback_timeout_seconds,
                self.fallback_client.answer,
                prompt,
                model=self.settings.fallback_model,
                timeout_seconds=self.settings.fallback_timeout_seconds,
                max_output_tokens=self.settings.answer_max_output_tokens,
            )
            return _with_estimated_cost(
                result,
                self.settings.runtime_fallback_input_price_per_1m_tokens,
                self.settings.runtime_fallback_output_price_per_1m_tokens,
            )
        except Exception:
            failure_flags.append("fallback_failed")
            return None

    async def _append_turns(
        self,
        session: SessionState,
        message: RuntimeMessage,
        user_query: str,
        reply: str,
        retrieval: RetrievalContext,
        last_rag_query: str | None,
        now: datetime,
        failure_flags: list[str],
    ) -> None:
        for attempt in range(self.settings.session_update_retry_count + 1):
            try:
                await _run_blocking(
                    self.settings.session_db_timeout_seconds,
                    self.store.append_successful_turns,
                    session,
                    message,
                    user_query,
                    reply,
                    retrieval,
                    last_rag_query,
                    now,
                    self.settings,
                )
                return
            except Exception:
                if attempt >= self.settings.session_update_retry_count:
                    failure_flags.append("session_update_failed_after_send")

    async def _delete_empty_session(self, session_id: str, failure_flags: list[str]) -> None:
        try:
            await _run_blocking(
                self.settings.session_db_timeout_seconds,
                self.store.delete_empty_session,
                session_id,
            )
        except Exception:
            failure_flags.append("session_cleanup_failed")

    def _log_event(
        self,
        *,
        event: str,
        status: str,
        message: RuntimeMessage,
        request_id: str | None = None,
        session: SessionState | None = None,
        retrieval: RetrievalContext | None = None,
        router_decision: str | None = None,
        identity: MemberIdentityContext | None = None,
        retrieval_intent: RetrievalIntent | None = None,
        llm_result: LLMResult | None = None,
        failure_flags: list[str] | None = None,
        error: Exception | None = None,
        started: float | None = None,
    ) -> None:
        payload = {
            "event": event,
            "request_id": request_id,
            "timestamp": _utc_now().isoformat(),
            "level": "error" if status.endswith("failed") else "info",
            "component": "runtime",
            "status": status,
            "duration_ms": int((time.monotonic() - started) * 1000) if started else None,
            "guild_id": message.guild_id,
            "channel_id": message.channel_id,
            "message_id": str(message.message_id),
            "session_id": session.session_id if session else None,
            "is_degraded": bool(failure_flags),
            "router_decision": router_decision,
            "retrieval_status": retrieval.status if retrieval else None,
            "target_author_ids": list(retrieval_intent.target_author_ids)
            if retrieval_intent
            else [],
            "target_time_window": retrieval_intent.target_time_window.log_value()
            if retrieval_intent and retrieval_intent.target_time_window
            else None,
            "member_identity_status": list(identity_status(identity)) if identity else [],
            "retrieval_filter_status": list(retrieval.retrieval_filter_status)
            if retrieval
            else [],
            "retrieved_chunk_keys": retrieval.chunk_keys if retrieval else [],
            "retrieved_summary_keys": retrieval.summary_keys if retrieval else [],
            "answer_model": llm_result.model if llm_result else self.settings.answer_model,
            "fallback_model": self.settings.fallback_model,
            "router_model": self.settings.router_model,
            "embedding_model": self.settings.embedding_model,
            "token_input": llm_result.token_input if llm_result else None,
            "token_output": llm_result.token_output if llm_result else None,
            "estimated_cost": llm_result.estimated_cost if llm_result else None,
            "failure_flags": failure_flags or [],
        }
        if error is not None:
            payload["error"] = str(error)[:200]
        self.logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def assemble_answer_prompt(
    system_prompt: str,
    retrieval: RetrievalContext,
    turns: list[dict[str, Any]],
    user_query: str,
    settings: Settings,
    identity: MemberIdentityContext | None = None,
) -> str:
    prompt_turns = turns[-settings.session_prompt_turns :]
    context_budget = int(settings.prompt_input_token_budget * settings.rag_context_budget_ratio)
    context_text = _truncate_chars(_format_retrieval_context(retrieval), context_budget * 4)
    parts = [system_prompt.strip()]
    if identity:
        parts.append(render_member_identity_block(identity))
    if context_text:
        parts.append(
            render_author_names(context_text, identity) if identity else context_text
        )
    if prompt_turns:
        parts.append("最近 session turns:\n" + _format_turns(prompt_turns))
    parts.append("current user query:\n" + user_query)
    return _truncate_chars("\n\n".join(parts), settings.prompt_input_token_budget * 4)


def assemble_router_prompt(
    router_prompt: str, session: SessionState, user_query: str, router_session_turns: int
) -> str:
    turns = _format_turns(session.turns[-router_session_turns :])
    return "\n\n".join(
        [
            router_prompt.strip(),
            f"previous last_rag_query:\n{session.last_rag_query or ''}",
            f"recent session turns:\n{turns}",
            f"current user query:\n{user_query}",
        ]
    )


def _format_retrieval_context(retrieval: RetrievalContext) -> str:
    if not retrieval.has_context:
        return ""
    lines = ["群組背景摘要與對應片段:"]
    aligned_by_summary: dict[str, list[RetrievedChunk]] = {}
    ungrouped_aligned = []
    for chunk in retrieval.chunks:
        if chunk.source != "aligned":
            continue
        if chunk.summary_key:
            aligned_by_summary.setdefault(chunk.summary_key, []).append(chunk)
        else:
            ungrouped_aligned.append(chunk)
    for summary in retrieval.summaries:
        lines.append(f"- {summary.summary_text}")
        for chunk in aligned_by_summary.get(summary.summary_key, []):
            lines.append(f"  - {chunk.chunk_text}")
    if ungrouped_aligned:
        lines.append("對應片段:")
        for chunk in ungrouped_aligned:
            lines.append(f"- {chunk.chunk_text}")
    global_chunks = [chunk for chunk in retrieval.chunks if chunk.source == "global"]
    if global_chunks:
        lines.append("其他可能相關對話片段:")
        for chunk in global_chunks:
            lines.append(f"- {chunk.chunk_text}")
    return "\n".join(lines)


def _format_turns(turns: list[dict[str, Any]]) -> str:
    lines = []
    for turn in turns:
        role = turn.get("role", "")
        content = turn.get("content", "")
        lines.append(f"- {role}: {content}")
    return "\n".join(lines)


def _truncate_chars(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip()


def _truncate_discord_reply(reply: str) -> str:
    if len(reply) <= DISCORD_SINGLE_MESSAGE_LIMIT:
        return reply
    suffix = "\n\n（回覆過長，已截斷。）"
    return reply[: DISCORD_SINGLE_MESSAGE_LIMIT - len(suffix)].rstrip() + suffix


def _with_estimated_cost(
    result: LLMResult,
    input_price_per_1m_tokens: Decimal | None,
    output_price_per_1m_tokens: Decimal | None,
) -> LLMResult:
    if (
        result.token_input is None
        or result.token_output is None
        or input_price_per_1m_tokens is None
        or output_price_per_1m_tokens is None
    ):
        return result
    cost = (
        Decimal(result.token_input) * input_price_per_1m_tokens
        + Decimal(result.token_output) * output_price_per_1m_tokens
    ) / Decimal(1_000_000)
    return LLMResult(
        text=result.text,
        model=result.model,
        token_input=result.token_input,
        token_output=result.token_output,
        estimated_cost=str(cost.quantize(Decimal("0.000001"))),
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _run_blocking(
    operation_timeout_seconds: float,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    return await asyncio.wait_for(
        asyncio.to_thread(func, *args, **kwargs),
        timeout=operation_timeout_seconds,
    )
