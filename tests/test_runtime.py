from __future__ import annotations

from datetime import datetime, timezone
import unittest

from src.config import Settings
from src.runtime import (
    EMPTY_QUERY_REPLY,
    LLMResult,
    MentionRuntime,
    RetrievalContext,
    RetrievedChunk,
    RetrievedSummary,
    RuntimeMessage,
    SessionState,
)


BOT_ID = 999


def settings() -> Settings:
    return Settings(
        bot_token="token",
        guild_id=1,
        channel_id=2,
        database_url="postgresql://local/test",
        chunk_strategy_version="timegap-v1",
        summary_strategy_version="hourly-v1",
    )


def message(raw_content: str = "hello", *, mentioned: bool = False) -> RuntimeMessage:
    return RuntimeMessage(
        message_id=123,
        created_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
        author_id=456,
        raw_content=raw_content,
        guild_id=1,
        channel_id=2,
        is_dm=False,
        is_thread=False,
        is_bot_author=False,
        is_bot_mentioned=mentioned,
        bot_user_id=BOT_ID,
    )


class FakeStore:
    def __init__(self, session: SessionState | None = None, retrieval: RetrievalContext | None = None):
        self.session = session or SessionState("session-1", [], [], [], None, is_new=True)
        self.retrieval = retrieval or RetrievalContext([], [], "retrieved")
        self.raw_upserts = []
        self.session_calls = 0
        self.retrieve_calls = 0
        self.load_context_calls = 0
        self.append_calls = []
        self.deleted_sessions = []

    def upsert_raw_message(self, message, normalized_content, is_rag_eligible):
        self.raw_upserts.append((message.message_id, normalized_content, is_rag_eligible))

    def get_or_create_active_session(self, channel_id, now, settings):
        self.session_calls += 1
        return self.session

    def delete_empty_session(self, session_id):
        self.deleted_sessions.append(session_id)

    def retrieve_context(self, query_embedding, settings):
        self.retrieve_calls += 1
        return self.retrieval

    def load_context_by_keys(self, summary_keys, chunk_keys):
        self.load_context_calls += 1
        return self.retrieval

    def append_successful_turns(
        self,
        session,
        message,
        user_query,
        assistant_reply,
        retrieval,
        last_rag_query,
        now,
        settings,
    ):
        self.append_calls.append((session.session_id, user_query, assistant_reply, retrieval, last_rag_query))


class FakeEmbedding:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = []

    def embed_query(self, query, *, model, timeout_seconds):
        self.calls.append((query, model, timeout_seconds))
        if self.fail:
            raise RuntimeError("embedding failed")
        return [0.1] * 1536


class FakeAnswer:
    def __init__(self, text: str = "answer", fail: bool = False):
        self.text = text
        self.fail = fail
        self.calls = []

    def answer(self, prompt, *, model, timeout_seconds, max_output_tokens):
        self.calls.append((prompt, model, timeout_seconds, max_output_tokens))
        if self.fail:
            raise RuntimeError("answer failed")
        return LLMResult(self.text, model, token_input=10, token_output=5)


class FakeRouter:
    def __init__(self, should_retrieve: bool = False, fail: bool = False):
        self.should_retrieve_value = should_retrieve
        self.fail = fail
        self.calls = []

    def should_retrieve(self, prompt, *, model, timeout_seconds, max_output_tokens):
        self.calls.append((prompt, model, timeout_seconds, max_output_tokens))
        if self.fail:
            raise RuntimeError("router failed")
        return self.should_retrieve_value


class SentReplies:
    def __init__(self):
        self.replies = []

    async def send(self, reply: str):
        self.replies.append(reply)


class MentionRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_non_mention_upserts_raw_without_session_retrieval_or_llm(self):
        store = FakeStore()
        answer = FakeAnswer()
        runtime = MentionRuntime(
            settings(),
            store,
            embedding_client=FakeEmbedding(),
            answer_client=answer,
            router_client=FakeRouter(),
        )
        sent = SentReplies()

        result = await runtime.handle_message(message("hello group"), sent.send)

        self.assertEqual(result.status, "raw_upserted")
        self.assertEqual(store.raw_upserts, [(123, "hello group", True)])
        self.assertEqual(store.session_calls, 0)
        self.assertEqual(store.retrieve_calls, 0)
        self.assertEqual(answer.calls, [])
        self.assertEqual(sent.replies, [])

    async def test_empty_mention_replies_without_session_retrieval_or_llm(self):
        store = FakeStore()
        answer = FakeAnswer()
        runtime = MentionRuntime(
            settings(),
            store,
            embedding_client=FakeEmbedding(),
            answer_client=answer,
            router_client=FakeRouter(),
        )
        sent = SentReplies()

        result = await runtime.handle_message(
            message(f"<@{BOT_ID}>", mentioned=True), sent.send
        )

        self.assertEqual(result.status, "empty_query")
        self.assertEqual(sent.replies, [EMPTY_QUERY_REPLY])
        self.assertEqual(store.raw_upserts, [(123, "", False)])
        self.assertEqual(store.session_calls, 0)
        self.assertEqual(store.retrieve_calls, 0)
        self.assertEqual(answer.calls, [])

    async def test_new_valid_mention_retrieves_and_updates_session_after_send(self):
        retrieval = RetrievalContext(
            summaries=[RetrievedSummary("summary-1", "group summary")],
            chunks=[RetrievedChunk("chunk-1", "group chunk", "aligned")],
            status="retrieved",
        )
        store = FakeStore(retrieval=retrieval)
        answer = FakeAnswer("context answer")
        runtime = MentionRuntime(
            settings(),
            store,
            embedding_client=FakeEmbedding(),
            answer_client=answer,
            router_client=FakeRouter(),
        )
        sent = SentReplies()

        result = await runtime.handle_message(
            message(f"<@{BOT_ID}> explain this", mentioned=True), sent.send
        )

        self.assertEqual(result.status, "answered")
        self.assertEqual(sent.replies, ["context answer"])
        self.assertEqual(store.retrieve_calls, 1)
        self.assertEqual(len(store.append_calls), 1)
        self.assertEqual(store.append_calls[0][1], "explain this")
        self.assertEqual(store.append_calls[0][4], "explain this")
        self.assertIn("group summary", answer.calls[0][0])
        self.assertIn("group chunk", answer.calls[0][0])

    async def test_retrieval_failure_degrades_to_general_answer(self):
        store = FakeStore()
        answer = FakeAnswer("general answer")
        runtime = MentionRuntime(
            settings(),
            store,
            embedding_client=FakeEmbedding(fail=True),
            answer_client=answer,
            router_client=FakeRouter(),
        )
        sent = SentReplies()

        result = await runtime.handle_message(
            message(f"<@{BOT_ID}> hi", mentioned=True), sent.send
        )

        self.assertEqual(result.status, "answered")
        self.assertEqual(sent.replies, ["general answer"])
        self.assertEqual(store.append_calls[0][4], None)
        self.assertNotIn("群組背景摘要", answer.calls[0][0])

    async def test_active_session_reuses_context_when_router_says_no_retrieve(self):
        session = SessionState(
            "session-1",
            [{"role": "user", "content": "previous"}],
            ["chunk-1"],
            ["summary-1"],
            "previous query",
        )
        retrieval = RetrievalContext(
            [RetrievedSummary("summary-1", "old summary")],
            [RetrievedChunk("chunk-1", "old chunk", "aligned")],
            "reused",
        )
        store = FakeStore(session=session, retrieval=retrieval)
        router = FakeRouter(should_retrieve=False)
        runtime = MentionRuntime(
            settings(),
            store,
            embedding_client=FakeEmbedding(),
            answer_client=FakeAnswer(),
            router_client=router,
        )
        sent = SentReplies()

        await runtime.handle_message(
            message(f"<@{BOT_ID}> follow up", mentioned=True), sent.send
        )

        self.assertEqual(len(router.calls), 1)
        self.assertEqual(store.retrieve_calls, 0)
        self.assertEqual(store.load_context_calls, 1)
        self.assertEqual(store.append_calls[0][4], "previous query")

    async def test_session_is_not_updated_when_send_fails(self):
        store = FakeStore()
        runtime = MentionRuntime(
            settings(),
            store,
            embedding_client=FakeEmbedding(),
            answer_client=FakeAnswer(),
            router_client=FakeRouter(),
        )

        async def fail_send(_reply):
            raise RuntimeError("send failed")

        result = await runtime.handle_message(message(f"<@{BOT_ID}> hi", mentioned=True), fail_send)

        self.assertEqual(result.status, "discord_send_failed")
        self.assertEqual(store.append_calls, [])
        self.assertEqual(store.deleted_sessions, ["session-1"])


if __name__ == "__main__":
    unittest.main()
