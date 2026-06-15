from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any
from uuid import uuid4

from src.config import Settings
from src.runtime import (
    RetrievalContext,
    RetrievedChunk,
    RetrievedSummary,
    RuntimeMessage,
    SessionState,
)


SESSION_LOCK_NAMESPACE = 914001

RAW_MESSAGE_UPSERT_SQL = """
INSERT INTO raw_messages (
  message_id,
  created_at,
  author_id,
  raw_content,
  normalized_content,
  is_rag_eligible,
  source
) VALUES (
  %(message_id)s,
  %(created_at)s,
  %(author_id)s,
  %(raw_content)s,
  %(normalized_content)s,
  %(is_rag_eligible)s,
  'discord_gateway'
)
ON CONFLICT (message_id) DO UPDATE SET
  created_at = EXCLUDED.created_at,
  author_id = EXCLUDED.author_id,
  raw_content = EXCLUDED.raw_content,
  normalized_content = EXCLUDED.normalized_content,
  is_rag_eligible = EXCLUDED.is_rag_eligible,
  source = EXCLUDED.source,
  last_seen_at = now()
"""


class PostgresRuntimeStore:
    def __init__(self, database_url: str, connect: Any | None = None) -> None:
        self.database_url = database_url
        if connect is None:
            import psycopg

            connect = psycopg.connect
        self.connect = connect

    def upsert_raw_message(
        self,
        message: RuntimeMessage,
        normalized_content: str,
        is_rag_eligible: bool,
    ) -> None:
        with self.connect(self.database_url) as conn:
            conn.execute(
                RAW_MESSAGE_UPSERT_SQL,
                {
                    "message_id": message.message_id,
                    "created_at": message.created_at,
                    "author_id": message.author_id,
                    "raw_content": message.raw_content,
                    "normalized_content": normalized_content,
                    "is_rag_eligible": is_rag_eligible,
                },
            )

    def get_or_create_active_session(
        self, channel_id: int, now: datetime, settings: Settings
    ) -> SessionState:
        expires_at = now + timedelta(minutes=settings.session_timeout_minutes)
        with self.connect(self.database_url) as conn:
            _lock_session(conn, channel_id)
            row = conn.execute(
                """
                SELECT session_id, turns, retrieved_chunk_keys,
                       retrieved_summary_keys, last_rag_query
                FROM sessions
                WHERE channel_id = %(channel_id)s
                  AND thread_id IS NULL
                  AND expires_at > %(now)s
                ORDER BY expires_at DESC
                LIMIT 1
                """,
                {"channel_id": channel_id, "now": now},
            ).fetchone()
            if row:
                return _session_from_row(row, is_new=False)

            session_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO sessions (
                  session_id, channel_id, thread_id, started_at,
                  last_interaction_at, expires_at
                ) VALUES (
                  %(session_id)s, %(channel_id)s, NULL, %(now)s, %(now)s, %(expires_at)s
                )
                """,
                {
                    "session_id": session_id,
                    "channel_id": channel_id,
                    "now": now,
                    "expires_at": expires_at,
                },
            )
            return SessionState(session_id, [], [], [], None, is_new=True)

    def delete_empty_session(self, session_id: str) -> None:
        with self.connect(self.database_url) as conn:
            conn.execute(
                "DELETE FROM sessions WHERE session_id = %(session_id)s AND turns = '[]'::jsonb",
                {"session_id": session_id},
            )

    def retrieve_context(
        self, query_embedding: list[float], settings: Settings
    ) -> RetrievalContext:
        with self.connect(self.database_url) as conn:
            summaries = _select_summaries(conn, query_embedding, settings)
            aligned = _select_aligned_chunks(conn, query_embedding, summaries, settings)
            global_chunks = _select_global_chunks(
                conn, query_embedding, [chunk.chunk_key for chunk in aligned], settings
            )
        return RetrievalContext(summaries, aligned + global_chunks, "retrieved")

    def load_context_by_keys(
        self, summary_keys: list[str], chunk_keys: list[str]
    ) -> RetrievalContext:
        with self.connect(self.database_url) as conn:
            summaries = conn.execute(
                """
                SELECT summary_key, summary_text
                FROM hourly_summaries
                WHERE summary_key = ANY(%(summary_keys)s)
                """,
                {"summary_keys": summary_keys},
            ).fetchall()
            chunks = conn.execute(
                """
                SELECT chunk_key, chunk_text
                FROM conversation_chunks
                WHERE chunk_key = ANY(%(chunk_keys)s)
                """,
                {"chunk_keys": chunk_keys},
            ).fetchall()
        loaded_summaries = [RetrievedSummary(str(row[0]), str(row[1])) for row in summaries]
        loaded_chunks = [RetrievedChunk(str(row[0]), str(row[1]), "aligned") for row in chunks]
        status = "reused" if loaded_summaries or loaded_chunks else "retrieval_context_missing"
        return RetrievalContext(loaded_summaries, loaded_chunks, status)

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
        expires_at = now + timedelta(minutes=settings.session_timeout_minutes)
        new_turns = [
            {
                "role": "user",
                "message_id": str(message.message_id),
                "content": user_query,
                "created_at": message.created_at.astimezone(timezone.utc).isoformat(),
            },
            {
                "role": "assistant",
                "message_id": None,
                "content": assistant_reply,
                "created_at": now.isoformat(),
            },
        ]
        with self.connect(self.database_url) as conn:
            _lock_session(conn, message.channel_id)
            conn.execute(
                """
                UPDATE sessions
                SET turns = (
                      WITH ordered AS (
                        SELECT turn, ord
                        FROM jsonb_array_elements(turns || %(new_turns)s::jsonb) WITH ORDINALITY AS t(turn, ord)
                        ORDER BY ord DESC
                        LIMIT %(stored_turns)s
                      )
                      SELECT COALESCE(jsonb_agg(turn ORDER BY ord), '[]'::jsonb)
                      FROM ordered
                    ),
                    retrieved_chunk_keys = %(chunk_keys)s,
                    retrieved_summary_keys = %(summary_keys)s,
                    last_rag_query = %(last_rag_query)s,
                    last_interaction_at = %(now)s,
                    expires_at = %(expires_at)s
                WHERE session_id = %(session_id)s
                """,
                {
                    "new_turns": json.dumps(new_turns),
                    "stored_turns": settings.session_stored_turns,
                    "chunk_keys": retrieval.chunk_keys,
                    "summary_keys": retrieval.summary_keys,
                    "last_rag_query": last_rag_query,
                    "now": now,
                    "expires_at": expires_at,
                    "session_id": session.session_id,
                },
            )


def _session_from_row(row: Any, *, is_new: bool) -> SessionState:
    return SessionState(
        session_id=str(row[0]),
        turns=list(row[1] or []),
        retrieved_chunk_keys=list(row[2] or []),
        retrieved_summary_keys=list(row[3] or []),
        last_rag_query=row[4],
        is_new=is_new,
    )


def _lock_session(conn: Any, channel_id: int) -> None:
    conn.execute(
        "SELECT pg_advisory_xact_lock(%(lock_key)s)",
        {"lock_key": _session_lock_key(channel_id)},
    )


def _session_lock_key(channel_id: int) -> int:
    raw_key = f"{SESSION_LOCK_NAMESPACE}:{channel_id}".encode("ascii")
    unsigned_key = int.from_bytes(hashlib.sha256(raw_key).digest()[:8], "big")
    return unsigned_key - (1 << 63)


def _select_summaries(
    conn: Any, query_embedding: list[float], settings: Settings
) -> list[RetrievedSummary]:
    rows = conn.execute(
        """
        SELECT summary_key, summary_text
        FROM hourly_summaries
        WHERE summary_strategy_version = %(strategy_version)s
        ORDER BY embedding <=> %(query_embedding)s::vector
        LIMIT %(limit)s
        """,
        {
            "strategy_version": settings.summary_strategy_version,
            "query_embedding": query_embedding,
            "limit": settings.summary_top_k,
        },
    ).fetchall()
    return [RetrievedSummary(str(row[0]), str(row[1])) for row in rows]


def _select_aligned_chunks(
    conn: Any,
    query_embedding: list[float],
    summaries: list[RetrievedSummary],
    settings: Settings,
) -> list[RetrievedChunk]:
    if not summaries or settings.aligned_chunks_max == 0:
        return []
    rows = conn.execute(
        """
        WITH selected_summaries AS (
          SELECT summary_key, hour_start, hour_end
          FROM hourly_summaries
          WHERE summary_key = ANY(%(summary_keys)s)
        ), ranked_chunks AS (
          SELECT chunk.chunk_key,
                 chunk.chunk_text,
                 row_number() OVER (
                   PARTITION BY summary.summary_key
                   ORDER BY chunk.embedding <=> %(query_embedding)s::vector
                 ) AS summary_rank,
                 chunk.embedding <=> %(query_embedding)s::vector AS distance
          FROM selected_summaries summary
          JOIN conversation_chunks chunk
            ON chunk.start_at < summary.hour_end
           AND chunk.end_at >= summary.hour_start
          WHERE chunk.chunk_strategy_version = %(strategy_version)s
        )
        SELECT chunk_key, chunk_text
        FROM ranked_chunks
        WHERE summary_rank <= %(per_summary_limit)s
        GROUP BY chunk_key, chunk_text
        ORDER BY min(distance)
        LIMIT %(limit)s
        """,
        {
            "summary_keys": [summary.summary_key for summary in summaries],
            "strategy_version": settings.chunk_strategy_version,
            "query_embedding": query_embedding,
            "per_summary_limit": settings.aligned_chunks_per_summary,
            "limit": settings.aligned_chunks_max,
        },
    ).fetchall()
    return [RetrievedChunk(str(row[0]), str(row[1]), "aligned") for row in rows]


def _select_global_chunks(
    conn: Any,
    query_embedding: list[float],
    excluded_chunk_keys: list[str],
    settings: Settings,
) -> list[RetrievedChunk]:
    if settings.global_chunk_fallback_top_k == 0:
        return []
    rows = conn.execute(
        """
        SELECT chunk_key, chunk_text
        FROM conversation_chunks
        WHERE chunk_strategy_version = %(strategy_version)s
          AND NOT (chunk_key = ANY(%(excluded_chunk_keys)s))
        ORDER BY embedding <=> %(query_embedding)s::vector
        LIMIT %(limit)s
        """,
        {
            "strategy_version": settings.chunk_strategy_version,
            "excluded_chunk_keys": excluded_chunk_keys,
            "query_embedding": query_embedding,
            "limit": settings.global_chunk_fallback_top_k,
        },
    ).fetchall()
    return [RetrievedChunk(str(row[0]), str(row[1]), "global") for row in rows]
