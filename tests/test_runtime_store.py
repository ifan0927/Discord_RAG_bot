from __future__ import annotations

import unittest

from src.config import Settings
from src.runtime_store import PostgresRuntimeStore


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
    def __init__(self, summary_rows=None):
        self.executions = []
        self.summary_rows = summary_rows or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params or {}))
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

        store.retrieve_context([0.1] * 1536, settings)

        executed_sql = "\n".join(sql for sql, _params in fake_conn.executions)
        params = [params for _sql, params in fake_conn.executions]
        self.assertIn("summary_strategy_version = %(strategy_version)s", executed_sql)
        self.assertIn("chunk_strategy_version = %(strategy_version)s", executed_sql)
        self.assertEqual(params[0]["strategy_version"], "hourly-v1")
        self.assertEqual(params[1]["strategy_version"], "timegap-v1")
        self.assertEqual(params[1]["per_summary_limit"], 2)

    def test_session_lookup_create_uses_channel_advisory_lock(self):
        fake_conn = FakeConnection()
        store = PostgresRuntimeStore("postgresql://local/test", connect=lambda _url: fake_conn)
        settings = Settings(
            bot_token="token",
            guild_id=1,
            channel_id=2,
            session_timeout_minutes=15,
        )

        from datetime import datetime, timezone

        store.get_or_create_active_session(2, datetime(2026, 6, 12, tzinfo=timezone.utc), settings)

        lock_sql, lock_params = fake_conn.executions[0]
        self.assertIn("pg_advisory_xact_lock", lock_sql)
        self.assertEqual(lock_params["channel_id"], 2)

    def test_session_turn_update_keeps_json_payload_and_chronological_order(self):
        fake_conn = FakeConnection()
        store = PostgresRuntimeStore("postgresql://local/test", connect=lambda _url: fake_conn)
        settings = Settings(
            bot_token="token",
            guild_id=1,
            channel_id=2,
            session_stored_turns=20,
        )

        from datetime import datetime, timezone

        from src.runtime import RetrievalContext, RuntimeMessage, SessionState

        store.append_successful_turns(
            SessionState("session-1", [], [], [], None),
            RuntimeMessage(
                message_id=123,
                created_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
                author_id=456,
                raw_content="<@999> hi",
                guild_id=1,
                channel_id=2,
                is_dm=False,
                is_thread=False,
                is_bot_author=False,
                is_bot_mentioned=True,
                bot_user_id=999,
            ),
            "hi",
            "hello",
            RetrievalContext([], [], "retrieval_empty"),
            None,
            datetime(2026, 6, 12, 1, tzinfo=timezone.utc),
            settings,
        )

        lock_sql, lock_params = fake_conn.executions[0]
        sql, params = fake_conn.executions[1]
        self.assertIn("pg_advisory_xact_lock", lock_sql)
        self.assertEqual(lock_params["channel_id"], 2)
        self.assertIn("jsonb_agg(turn ORDER BY ord)", sql)
        self.assertIsInstance(params["new_turns"], str)
        self.assertIn('"role": "user"', params["new_turns"])


if __name__ == "__main__":
    unittest.main()
