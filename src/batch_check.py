from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.batch_chunks import (
    EMBEDDING_DIMENSIONS,
    RawMessage,
    _chunk_windows,
    load_chunk_batch_settings,
)
from src.batch_summaries import _messages_by_taipei_hour, load_summary_batch_settings
from src.migration import check_schema


TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class ArtifactCheck:
    expected_count: int
    actual_count: int
    missing_keys: list[str]
    unexpected_keys: list[str]
    embedding_dimensions_ok: bool
    bad_embedding_keys: list[str]

    @property
    def ok(self) -> bool:
        return (
            not self.missing_keys
            and not self.unexpected_keys
            and self.embedding_dimensions_ok
            and self.expected_count == self.actual_count
        )


@dataclass(frozen=True)
class BatchCheckResult:
    day: str
    ok: bool
    raw_eligible_count: int
    chunk_strategy_version: str | None
    summary_strategy_version: str | None
    expected_artifact_counts: dict[str, int]
    actual_artifact_counts: dict[str, int]
    missing_keys: dict[str, list[str]]
    unexpected_keys: dict[str, list[str]]
    embedding_dimension_status: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(
            {
                "day": self.day,
                "ok": self.ok,
                "raw_eligible_count": self.raw_eligible_count,
                "strategy_versions": {
                    "chunks": self.chunk_strategy_version,
                    "summaries": self.summary_strategy_version,
                },
                "expected_artifact_counts": self.expected_artifact_counts,
                "actual_artifact_counts": self.actual_artifact_counts,
                "missing_keys": self.missing_keys,
                "unexpected_keys": self.unexpected_keys,
                "embedding_dimension_status": self.embedding_dimension_status,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )


def check_batch_day(
    *,
    database_url: str,
    day: date,
    only: str = "all",
    schema_checker: Callable[[str], Any] = check_schema,
    connect: Callable[[str], Any] | None = None,
) -> BatchCheckResult:
    if only not in ("all", "chunks", "summaries"):
        raise RuntimeError("only must be one of: all, chunks, summaries")

    chunk_settings = load_chunk_batch_settings() if only in ("all", "chunks") else None
    summary_settings = load_summary_batch_settings() if only in ("all", "summaries") else None

    schema = schema_checker(database_url)
    if not schema.ok:
        raise RuntimeError("Database schema check failed; run migrate up before check-batch")

    if connect is None:
        import psycopg

        connect = psycopg.connect

    start_at, end_at = _utc_day_bounds(day)
    with connect(database_url) as conn:
        messages = _select_raw_messages(conn, start_at, end_at)
        chunks = _empty_artifact_check()
        summaries = _empty_artifact_check()
        if only in ("all", "chunks"):
            chunks = _check_chunks(
                conn,
                day,
                messages,
                chunk_settings.chunk_strategy_version,
                start_at,
                end_at,
                chunk_settings,
            )
        if only in ("all", "summaries"):
            summaries = _check_summaries(
                conn,
                messages,
                summary_settings.summary_strategy_version,
                start_at,
                end_at,
            )

    expected_counts = {
        "chunks": chunks.expected_count if only in ("all", "chunks") else 0,
        "summaries": summaries.expected_count if only in ("all", "summaries") else 0,
    }
    actual_counts = {
        "chunks": chunks.actual_count if only in ("all", "chunks") else 0,
        "summaries": summaries.actual_count if only in ("all", "summaries") else 0,
    }
    missing_keys = {
        "chunks": chunks.missing_keys if only in ("all", "chunks") else [],
        "summaries": summaries.missing_keys if only in ("all", "summaries") else [],
    }
    unexpected_keys = {
        "chunks": chunks.unexpected_keys if only in ("all", "chunks") else [],
        "summaries": summaries.unexpected_keys if only in ("all", "summaries") else [],
    }
    embedding_status = {
        "expected_dimensions": EMBEDDING_DIMENSIONS,
        "chunks": {
            "ok": chunks.embedding_dimensions_ok if only in ("all", "chunks") else True,
            "bad_keys": chunks.bad_embedding_keys if only in ("all", "chunks") else [],
        },
        "summaries": {
            "ok": summaries.embedding_dimensions_ok if only in ("all", "summaries") else True,
            "bad_keys": summaries.bad_embedding_keys if only in ("all", "summaries") else [],
        },
    }
    return BatchCheckResult(
        day=day.isoformat(),
        ok=(chunks.ok if only in ("all", "chunks") else True)
        and (summaries.ok if only in ("all", "summaries") else True),
        raw_eligible_count=len(messages),
        chunk_strategy_version=chunk_settings.chunk_strategy_version if chunk_settings else None,
        summary_strategy_version=summary_settings.summary_strategy_version if summary_settings else None,
        expected_artifact_counts=expected_counts,
        actual_artifact_counts=actual_counts,
        missing_keys=missing_keys,
        unexpected_keys=unexpected_keys,
        embedding_dimension_status=embedding_status,
    )


def _check_chunks(
    conn: Any,
    day: date,
    messages: list[RawMessage],
    strategy_version: str,
    start_at: datetime,
    end_at: datetime,
    settings: Any,
) -> ArtifactCheck:
    expected = [
        f"chunk:{strategy_version}:{day.isoformat()}:{window[0].message_id}:{window[-1].message_id}"
        for window in _chunk_windows(messages, settings)
    ]
    rows = conn.execute(
        """
        SELECT chunk_key, vector_dims(embedding)
        FROM conversation_chunks
        WHERE chunk_strategy_version = %(strategy_version)s
          AND start_at >= %(start_at)s
          AND start_at < %(end_at)s
        ORDER BY chunk_key
        """,
        {"strategy_version": strategy_version, "start_at": start_at, "end_at": end_at},
    ).fetchall()
    return _compare_artifacts(expected, rows)


def _check_summaries(
    conn: Any,
    messages: list[RawMessage],
    strategy_version: str,
    start_at: datetime,
    end_at: datetime,
) -> ArtifactCheck:
    expected = []
    for hour_start, group in _messages_by_taipei_hour(messages):
        expected.append(
            f"summary:{strategy_version}:{hour_start.isoformat()}:{group[0].message_id}:{group[-1].message_id}"
        )
    rows = conn.execute(
        """
        SELECT summary_key, vector_dims(embedding)
        FROM hourly_summaries
        WHERE summary_strategy_version = %(strategy_version)s
          AND hour_start >= %(start_at)s
          AND hour_start < %(end_at)s
        ORDER BY summary_key
        """,
        {"strategy_version": strategy_version, "start_at": start_at, "end_at": end_at},
    ).fetchall()
    return _compare_artifacts(expected, rows)


def _compare_artifacts(expected_keys: list[str], rows: list[Any]) -> ArtifactCheck:
    actual_dimensions = {str(row[0]): int(row[1]) for row in rows}
    expected_set = set(expected_keys)
    actual_set = set(actual_dimensions)
    bad_embedding_keys = sorted(
        key for key, dimensions in actual_dimensions.items() if dimensions != EMBEDDING_DIMENSIONS
    )
    return ArtifactCheck(
        expected_count=len(expected_keys),
        actual_count=len(actual_dimensions),
        missing_keys=sorted(expected_set - actual_set),
        unexpected_keys=sorted(actual_set - expected_set),
        embedding_dimensions_ok=not bad_embedding_keys,
        bad_embedding_keys=bad_embedding_keys,
    )


def _empty_artifact_check() -> ArtifactCheck:
    return ArtifactCheck(
        expected_count=0,
        actual_count=0,
        missing_keys=[],
        unexpected_keys=[],
        embedding_dimensions_ok=True,
        bad_embedding_keys=[],
    )


def _select_raw_messages(conn: Any, start_at: datetime, end_at: datetime) -> list[RawMessage]:
    rows = conn.execute(
        """
        SELECT message_id, created_at, author_id, normalized_content
        FROM raw_messages
        WHERE is_rag_eligible = true
          AND created_at >= %(start_at)s
          AND created_at < %(end_at)s
        ORDER BY created_at, message_id
        """,
        {"start_at": start_at, "end_at": end_at},
    ).fetchall()
    return [
        RawMessage(
            message_id=int(row[0]),
            created_at=_aware_utc(row[1]),
            author_id=int(row[2]),
            normalized_content=str(row[3]),
        )
        for row in rows
    ]


def _utc_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=TAIPEI).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=TAIPEI).astimezone(timezone.utc)
    return start, end


def _aware_utc(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise RuntimeError(f"Unsupported created_at value from database: {value!r}")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
