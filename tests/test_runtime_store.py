from __future__ import annotations

from datetime import datetime, timezone
import unittest

from src.config import Settings
from src.member_identity import RetrievalIntent, TimeWindow
from src.runtime import RetrievedChunk, RetrievedSummary
from src.runtime_store import PostgresRuntimeStore, _session_lock_key


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        if not self.rows:
            return None
        return self.rows[0]


class FakeConnection:
    def __init__(
        self,
        summary_rows=None,
        aligned_rows=None,
        chunk_rows=None,
        honor_metadata_filters: bool = False,
    ):
        self.executions = []
        self.summary_rows = summary_rows or []
        self.aligned_rows = aligned_rows or []
        self.chunk_rows = chunk_rows or []
        self.honor_metadata_filters = honor_metadata_filters

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params or {}))
        if self.honor_metadata_filters and (
            "author_ids" in (params or {}) or "window_start" in (params or {})
        ):
            return FakeResult([])
        if "FROM ranked_chunks" in sql:
            return FakeResult(self.aligned_rows)
        if "FROM conversation_chunks" in sql and "chunk_text" in sql:
            return FakeResult(self.chunk_rows)
        if "FROM hourly_summaries" in sql and "summary_text" in sql:
            return FakeResult(self.summary_rows)
        return FakeResult([])


class RuntimeStoreTest(unittest.TestCase):
    def test_retrieval_queries_filter_strategy_versions(self):
        fake_conn = FakeConnection(summary_rows=[("summary-1", "summary text")])
        store = PostgresRuntimeStore("postgresql://local/test", connect=lambda _url: fake_conn)
        settings = Settings(
            bot_token="token",
            guild_id=1,
            channel_id=2,
            chunk_strategy_version="timegap-v1",
            summary_strategy_version="hourly-v1",
            summary_top_k=3,
            aligned_chunks_max=6,
            global_chunk_fallback_top_k=2,
        )

        store.retrieve_context([0.1] * 1536, settings, RetrievalIntent())

        executed_sql = "\n".join(sql for sql, _params in fake_conn.executions)
        params = [params for _sql, params in fake_conn.executions]
        self.assertIn("summary_strategy_version = %(strategy_version)s", executed_sql)
        self.assertIn("chunk_strategy_version = %(strategy_version)s", executed_sql)
        self.assertIn("SELECT summary.summary_key", executed_sql)
        self.assertEqual(params[0]["strategy_version"], "hourly-v1")
        self.assertEqual(params[1]["strategy_version"], "timegap-v1")
        self.assertEqual(params[1]["per_summary_limit"], 2)

    def test_retrieve_context_preserves_aligned_chunk_summary_relation(self):
        fake_conn = FakeConnection(
            summary_rows=[("summary-1", "summary text")],
            aligned_rows=[("summary-1", "chunk-1", "chunk text")],
        )
        store = PostgresRuntimeStore("postgresql://local/test", connect=lambda _url: fake_conn)
        settings = Settings(
            bot_token="token",
            guild_id=1,
            channel_id=2,
            chunk_strategy_version="timegap-v1",
            summary_strategy_version="hourly-v1",
        )

        context = store.retrieve_context([0.1] * 1536, settings, RetrievalIntent())

        self.assertEqual(context.chunks[0].chunk_key, "chunk-1")
        self.assertEqual(context.chunks[0].source, "aligned")
        self.assertEqual(context.chunks[0].summary_key, "summary-1")

    def test_retrieve_context_applies_author_and_time_filters(self):
        fake_conn = FakeConnection(summary_rows=[("summary-1", "summary text")])
        store = PostgresRuntimeStore("postgresql://local/test", connect=lambda _url: fake_conn)
        settings = Settings(
            bot_token="token",
            guild_id=1,
            channel_id=2,
            chunk_strategy_version="timegap-v1",
            summary_strategy_version="hourly-v1",
        )

        context = store.retrieve_context(
            [0.1] * 1536,
            settings,
            RetrievalIntent(
                target_author_ids=(456,),
                target_time_window=TimeWindow(
                    datetime(2026, 6, 12, tzinfo=timezone.utc),
                    datetime(2026, 6, 13, tzinfo=timezone.utc),
                ),
            ),
        )

        executed_sql = "\n".join(sql for sql, _params in fake_conn.executions)
        params = [params for _sql, params in fake_conn.executions]
        self.assertIn("raw.author_id = ANY(%(author_ids)s)", executed_sql)
        self.assertIn("hourly_summaries.hour_start < %(window_end)s", executed_sql)
        self.assertIn("chunk.start_at < %(window_end)s", executed_sql)
        self.assertEqual(params[0]["author_ids"], [456])
        self.assertIn("window_start", params[0])
        self.assertEqual(context.retrieval_filter_status, ("author_time_filter",))

    def test_retrieve_context_falls_back_when_filtered_attempt_is_empty(self):
        fake_conn = FakeConnection(
            summary_rows=[],
            chunk_rows=[("chunk-1", "general chunk")],
            honor_metadata_filters=True,
        )
        store = PostgresRuntimeStore("postgresql://local/test", connect=lambda _url: fake_conn)
        settings = Settings(
            bot_token="token",
            guild_id=1,
            channel_id=2,
            chunk_strategy_version="timegap-v1",
            summary_strategy_version="hourly-v1",
        )

        context = store.retrieve_context(
            [0.1] * 1536,
            settings,
            RetrievalIntent(target_author_ids=(456,)),
        )

        self.assertEqual(context.chunk_keys, ["chunk-1"])
        self.assertEqual(
            context.retrieval_filter_status,
            ("author_filter_empty",),
        )

    def test_retrieve_context_relaxes_combined_filter_to_author_only(self):
        class CombinedFallbackConnection(FakeConnection):
            def execute(self, sql, params=None):
                self.executions.append((sql, params or {}))
                if "author_ids" in (params or {}) and "window_start" in (params or {}):
                    return FakeResult([])
                if "FROM conversation_chunks" in sql and "chunk_text" in sql:
                    return FakeResult([("chunk-1", "author-only chunk")])
                return FakeResult([])

        fake_conn = CombinedFallbackConnection()
        store = PostgresRuntimeStore("postgresql://local/test", connect=lambda _url: fake_conn)
        settings = Settings(
            bot_token="token",
            guild_id=1,
            channel_id=2,
            chunk_strategy_version="timegap-v1",
            summary_strategy_version="hourly-v1",
        )

        context = store.retrieve_context(
            [0.1] * 1536,
            settings,
            RetrievalIntent(
                target_author_ids=(456,),
                target_time_window=TimeWindow(
                    datetime(2026, 6, 12, tzinfo=timezone.utc),
                    datetime(2026, 6, 13, tzinfo=timezone.utc),
                ),
            ),
        )

        self.assertEqual(context.chunk_keys, ["chunk-1"])
        self.assertEqual(
            context.retrieval_filter_status,
            ("author_time_filter_empty", "author_filter"),
        )

    def test_load_context_by_keys_keeps_reused_chunks_global_without_provenance(self):
        fake_conn = FakeConnection(
            summary_rows=[("summary-1", "summary text")],
            chunk_rows=[("chunk-1", "chunk text")],
        )
        store = PostgresRuntimeStore("postgresql://local/test", connect=lambda _url: fake_conn)

        context = store.load_context_by_keys(["summary-1"], ["chunk-1"])

        self.assertEqual(context.status, "reused")
        self.assertEqual(context.chunks[0].source, "global")
        self.assertIsNone(context.chunks[0].summary_key)

    def test_session_lookup_create_uses_bigint_advisory_lock_for_discord_channel_id(self):
        fake_conn = FakeConnection()
        store = PostgresRuntimeStore("postgresql://local/test", connect=lambda _url: fake_conn)
        settings = Settings(
            bot_token="token",
            guild_id=1,
            channel_id=1135190284033065033,
            session_timeout_minutes=15,
        )

        from datetime import datetime, timezone

        channel_id = 1135190284033065033
        store.get_or_create_active_session(
            channel_id, datetime(2026, 6, 12, tzinfo=timezone.utc), settings
        )

        lock_sql, lock_params = fake_conn.executions[0]
        self.assertIn("pg_advisory_xact_lock(%(lock_key)s)", lock_sql)
        self.assertEqual(lock_params["lock_key"], _session_lock_key(channel_id))
        self.assertIsInstance(lock_params["lock_key"], int)
        self.assertGreaterEqual(lock_params["lock_key"], -(2**63))
        self.assertLess(lock_params["lock_key"], 2**63)

    def test_session_turn_update_keeps_json_payload_and_chronological_order(self):
        fake_conn = FakeConnection()
        store = PostgresRuntimeStore("postgresql://local/test", connect=lambda _url: fake_conn)
        settings = Settings(
            bot_token="token",
            guild_id=1,
            channel_id=1135190284033065033,
            session_stored_turns=20,
        )

        from datetime import datetime, timezone

        from src.runtime import RetrievalContext, RuntimeMessage, SessionState

        channel_id = 1135190284033065033
        store.append_successful_turns(
            SessionState("session-1", [], [], [], None),
            RuntimeMessage(
                message_id=123,
                created_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
                author_id=456,
                raw_content="<@999> hi",
                guild_id=1,
                channel_id=channel_id,
                is_dm=False,
                is_thread=False,
                is_bot_author=False,
                is_bot_mentioned=True,
                bot_user_id=999,
            ),
            "hi",
            "hello",
            RetrievalContext(
                [RetrievedSummary("summary-1", "summary text")],
                [RetrievedChunk("chunk-1", "chunk text", "aligned")],
                "retrieved",
            ),
            "hi",
            datetime(2026, 6, 12, 1, tzinfo=timezone.utc),
            settings,
        )

        lock_sql, lock_params = fake_conn.executions[0]
        sql, params = fake_conn.executions[1]
        self.assertIn("pg_advisory_xact_lock(%(lock_key)s)", lock_sql)
        self.assertEqual(lock_params["lock_key"], _session_lock_key(channel_id))
        self.assertIn("jsonb_agg(turn ORDER BY ord)", sql)
        self.assertIsInstance(params["new_turns"], str)
        self.assertIn('"role": "user"', params["new_turns"])
        self.assertEqual(params["chunk_keys"], ["chunk-1"])
        self.assertEqual(params["summary_keys"], ["summary-1"])
        self.assertEqual(params["last_rag_query"], "hi")


if __name__ == "__main__":
    unittest.main()
