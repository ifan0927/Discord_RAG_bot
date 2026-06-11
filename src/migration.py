from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "docs" / "design" / "rag_schema.sql"

REQUIRED_TABLES = [
    "raw_messages",
    "conversation_chunks",
    "hourly_summaries",
    "sessions",
]

REQUIRED_INDEXES = [
    "raw_messages_created_at_idx",
    "raw_messages_rag_created_at_idx",
    "conversation_chunks_embedding_hnsw_idx",
    "conversation_chunks_strategy_start_at_idx",
    "conversation_chunks_batch_id_idx",
    "hourly_summaries_embedding_hnsw_idx",
    "hourly_summaries_strategy_hour_start_idx",
    "hourly_summaries_batch_id_idx",
    "sessions_channel_thread_expires_at_idx",
]

REQUIRED_CONSTRAINTS = {
    "raw_messages": [
        "PRIMARY KEY (message_id)",
        "CHECK ((source = ANY",
    ],
    "conversation_chunks": [
        "PRIMARY KEY (chunk_key)",
        "FOREIGN KEY (start_message_id) REFERENCES raw_messages(message_id)",
        "FOREIGN KEY (end_message_id) REFERENCES raw_messages(message_id)",
        "CHECK ((message_count > 0))",
        "CHECK ((start_at <= end_at))",
        "CHECK ((start_message_id <= end_message_id))",
    ],
    "hourly_summaries": [
        "PRIMARY KEY (summary_key)",
        "FOREIGN KEY (start_message_id) REFERENCES raw_messages(message_id)",
        "FOREIGN KEY (end_message_id) REFERENCES raw_messages(message_id)",
        "CHECK ((hour_start < hour_end))",
        "CHECK ((start_message_id <= end_message_id))",
    ],
    "sessions": [
        "PRIMARY KEY (session_id)",
        "CHECK ((expires_at > started_at))",
        "CHECK ((last_interaction_at >= started_at))",
        "CHECK ((jsonb_typeof(turns) = 'array'::text))",
    ],
}


@dataclass(frozen=True)
class MigrationCheck:
    ok: bool
    extension: bool
    tables: dict[str, bool]
    indexes: dict[str, bool]
    constraints: dict[str, dict[str, bool]]

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "extension": {"vector": self.extension},
                "tables": self.tables,
                "indexes": self.indexes,
                "constraints": self.constraints,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )


def database_url_from_env() -> str:
    load_dotenv()
    value = os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("Missing required environment variable: DATABASE_URL")
    return value


def apply_schema(database_url: str, schema_path: Path = SCHEMA_PATH) -> None:
    import psycopg

    sql = schema_path.read_text(encoding="utf-8")
    with psycopg.connect(database_url) as conn:
        conn.execute(sql)


def check_schema(database_url: str) -> MigrationCheck:
    import psycopg

    with psycopg.connect(database_url) as conn:
        extension = _extension_exists(conn)
        existing_tables = _existing_tables(conn)
        existing_indexes = _existing_indexes(conn)
        constraint_defs = _constraint_defs(conn)

    tables = {name: name in existing_tables for name in REQUIRED_TABLES}
    indexes = {name: name in existing_indexes for name in REQUIRED_INDEXES}
    constraints = _check_constraints(constraint_defs)
    ok = (
        extension
        and all(tables.values())
        and all(indexes.values())
        and all(all(items.values()) for items in constraints.values())
    )
    return MigrationCheck(ok, extension, tables, indexes, constraints)


def _extension_exists(conn: Any) -> bool:
    row = conn.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')").fetchone()
    return bool(row[0])


def _existing_tables(conn: Any) -> set[str]:
    rows = conn.execute(
        """
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename = ANY(%s)
        """,
        (REQUIRED_TABLES,),
    ).fetchall()
    return {row[0] for row in rows}


def _existing_indexes(conn: Any) -> set[str]:
    rows = conn.execute(
        """
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND indexname = ANY(%s)
        """,
        (REQUIRED_INDEXES,),
    ).fetchall()
    return {row[0] for row in rows}


def _constraint_defs(conn: Any) -> dict[str, list[str]]:
    rows = conn.execute(
        """
        SELECT rel.relname, pg_get_constraintdef(con.oid)
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
        WHERE nsp.nspname = 'public'
          AND rel.relname = ANY(%s)
        """,
        (REQUIRED_TABLES,),
    ).fetchall()

    constraints: dict[str, list[str]] = {name: [] for name in REQUIRED_TABLES}
    for table_name, constraint_def in rows:
        constraints.setdefault(table_name, []).append(constraint_def)
    return constraints


def _check_constraints(constraint_defs: dict[str, list[str]]) -> dict[str, dict[str, bool]]:
    results: dict[str, dict[str, bool]] = {}
    for table_name, required_fragments in REQUIRED_CONSTRAINTS.items():
        defs = constraint_defs.get(table_name, [])
        results[table_name] = {
            fragment: any(fragment in constraint_def for constraint_def in defs)
            for fragment in required_fragments
        }
    return results
