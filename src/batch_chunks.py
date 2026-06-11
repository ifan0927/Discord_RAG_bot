from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.migration import check_schema


TAIPEI = ZoneInfo("Asia/Taipei")
EMBEDDING_DIMENSIONS = 1536


@dataclass(frozen=True)
class ChunkBatchSettings:
    chunk_strategy_version: str
    embedding_model: str
    chunk_window_messages: int
    chunk_stride_messages: int
    min_chunk_messages: int
    chunk_text_max_chars: int
    batch_staging_dir: Path


@dataclass(frozen=True)
class RawMessage:
    message_id: int
    created_at: datetime
    author_id: int
    normalized_content: str


@dataclass(frozen=True)
class StagedChunk:
    chunk_key: str
    chunk_strategy_version: str
    start_message_id: int
    end_message_id: int
    start_at: datetime
    end_at: datetime
    message_count: int
    chunk_text: str
    embedding: list[float]
    embedding_model: str
    batch_id: str


@dataclass(frozen=True)
class StagedChunks:
    chunks: list[StagedChunk]
    truncation_count: int


@dataclass(frozen=True)
class ChunkBatchResult:
    day: str
    status: str
    batch_id: str
    staged_chunks: int
    committed_chunks: int
    staging_dir: str
    manifest_path: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "day": self.day,
                "status": self.status,
                "batch_id": self.batch_id,
                "staged_chunks": self.staged_chunks,
                "committed_chunks": self.committed_chunks,
                "staging_dir": self.staging_dir,
                "manifest_path": self.manifest_path,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class DeterministicFakeEmbeddingProvider:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_fake_embedding(text) for text in texts]


def load_chunk_batch_settings() -> ChunkBatchSettings:
    load_dotenv()
    return ChunkBatchSettings(
        chunk_strategy_version=_required("CHUNK_STRATEGY_VERSION"),
        embedding_model=_required("EMBEDDING_MODEL"),
        chunk_window_messages=_positive_int("CHUNK_WINDOW_MESSAGES"),
        chunk_stride_messages=_positive_int("CHUNK_STRIDE_MESSAGES"),
        min_chunk_messages=_positive_int("MIN_CHUNK_MESSAGES"),
        chunk_text_max_chars=_positive_int("CHUNK_TEXT_MAX_CHARS"),
        batch_staging_dir=Path(_required("BATCH_STAGING_DIR")),
    )


def run_chunks_batch(
    *,
    database_url: str,
    day: date,
    embedding_provider: EmbeddingProvider,
    settings: ChunkBatchSettings | None = None,
    schema_checker: Any = check_schema,
    connect: Any | None = None,
) -> ChunkBatchResult:
    settings = settings or load_chunk_batch_settings()
    _validate_settings(settings)
    started_at = datetime.now(timezone.utc)

    schema = schema_checker(database_url)
    if not schema.ok:
        raise RuntimeError("Database schema check failed; run migrate up before run-batch")

    if connect is None:
        import psycopg

        connect = psycopg.connect

    start_at, end_at = _utc_day_bounds(day)
    with connect(database_url) as conn:
        messages = _select_raw_messages(conn, start_at, end_at)
        batch_id = _next_batch_id(
            conn,
            day,
            settings.chunk_strategy_version,
            start_at,
            end_at,
            settings.batch_staging_dir,
        )

    staging_dir = _staging_dir(settings.batch_staging_dir, day, batch_id)
    staged = _stage_chunks(day, messages, batch_id, settings, embedding_provider)
    validation_errors = _validate_chunks(day, staged.chunks, settings)
    manifest = _write_staging(
        staging_dir,
        day,
        batch_id,
        messages,
        staged,
        validation_errors,
        settings,
        started_at,
        status="failed" if validation_errors else "staged",
    )

    if validation_errors:
        return ChunkBatchResult(
            day=day.isoformat(),
            status="failed",
            batch_id=batch_id,
            staged_chunks=len(staged.chunks),
            committed_chunks=0,
            staging_dir=str(staging_dir),
            manifest_path=str(staging_dir / "manifest.json"),
        )

    with connect(database_url) as conn:
        committed = _commit_chunks(conn, staged.chunks, settings.chunk_strategy_version, start_at, end_at)

    final_status = "success_empty" if len(staged.chunks) == 0 else "success"
    _write_manifest(
        staging_dir / "manifest.json",
        {**manifest, "status": final_status, "step_status": {"chunks": final_status}, "finished_at": _now_iso()},
    )
    return ChunkBatchResult(
        day=day.isoformat(),
        status=final_status,
        batch_id=batch_id,
        staged_chunks=len(staged.chunks),
        committed_chunks=committed,
        staging_dir=str(staging_dir),
        manifest_path=str(staging_dir / "manifest.json"),
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


def _next_batch_id(
    conn: Any,
    day: date,
    strategy_version: str,
    start_at: datetime,
    end_at: datetime,
    staging_root: Path,
) -> str:
    rows = conn.execute(
        """
        SELECT batch_id
        FROM conversation_chunks
        WHERE chunk_strategy_version = %(strategy_version)s
          AND start_at >= %(start_at)s
          AND start_at < %(end_at)s
          AND batch_id LIKE %(prefix_like)s
        """,
        {
            "strategy_version": strategy_version,
            "start_at": start_at,
            "end_at": end_at,
            "prefix_like": f"chunk-{day.isoformat()}-r%",
        },
    ).fetchall()
    prefix = f"chunk-{day.isoformat()}-r"
    suffixes = _staging_batch_suffixes(staging_root, day, prefix)
    for row in rows:
        value = str(row[0])
        if value.startswith(prefix) and value[len(prefix) :].isdigit():
            suffixes.append(int(value[len(prefix) :]))
    return f"{prefix}{max(suffixes, default=0) + 1}"


def _staging_batch_suffixes(staging_root: Path, day: date, prefix: str) -> list[int]:
    day_dir = staging_root / day.isoformat()
    if not day_dir.is_dir():
        return []
    suffixes = []
    for path in day_dir.iterdir():
        if not path.is_dir():
            continue
        name = path.name
        if name.startswith(prefix) and name[len(prefix) :].isdigit():
            suffixes.append(int(name[len(prefix) :]))
    return suffixes


def _stage_chunks(
    day: date,
    messages: list[RawMessage],
    batch_id: str,
    settings: ChunkBatchSettings,
    embedding_provider: EmbeddingProvider,
) -> StagedChunks:
    windows = _chunk_windows(messages, settings)
    text_results = [_chunk_text(window, settings.chunk_text_max_chars) for window in windows]
    texts = [text for text, _truncated in text_results]
    embeddings = embedding_provider.embed(texts) if texts else []
    if len(embeddings) != len(texts):
        embeddings = [[] for _ in texts]
    chunks = []
    for window, text, embedding in zip(windows, texts, embeddings):
        start = window[0]
        end = window[-1]
        chunks.append(
            StagedChunk(
                chunk_key=(
                    f"chunk:{settings.chunk_strategy_version}:{day.isoformat()}:"
                    f"{start.message_id}:{end.message_id}"
                ),
                chunk_strategy_version=settings.chunk_strategy_version,
                start_message_id=start.message_id,
                end_message_id=end.message_id,
                start_at=start.created_at,
                end_at=end.created_at,
                message_count=len(window),
                chunk_text=text,
                embedding=embedding,
                embedding_model=settings.embedding_model,
                batch_id=batch_id,
            )
        )
    return StagedChunks(
        chunks=chunks,
        truncation_count=sum(1 for _text, truncated in text_results if truncated),
    )


def _chunk_windows(
    messages: list[RawMessage], settings: ChunkBatchSettings
) -> list[list[RawMessage]]:
    windows = []
    start = 0
    while len(messages) - start >= settings.min_chunk_messages:
        windows.append(messages[start : start + settings.chunk_window_messages])
        start += settings.chunk_stride_messages
    return windows


def _chunk_text(messages: list[RawMessage], max_chars: int) -> tuple[str, bool]:
    lines = [
        (
            f"{message.created_at.astimezone(TAIPEI).strftime('%Y-%m-%d %H:%M:%S')} "
            f"author:{message.author_id} {message.normalized_content}"
        )
        for message in messages
    ]
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text, False
    keep = max(0, (max_chars - 15) // 2)
    return f"{text[:keep]}\n...[truncated]...\n{text[-keep:]}", True


def _validate_chunks(
    day: date, chunks: list[StagedChunk], settings: ChunkBatchSettings
) -> list[str]:
    errors = []
    keys = [chunk.chunk_key for chunk in chunks]
    if len(keys) != len(set(keys)):
        errors.append("chunk_key values must be unique")
    for chunk in chunks:
        expected_prefix = f"chunk:{settings.chunk_strategy_version}:{day.isoformat()}:"
        if not chunk.chunk_key.startswith(expected_prefix):
            errors.append(f"invalid chunk_key: {chunk.chunk_key}")
        if not chunk.chunk_text:
            errors.append(f"chunk_text is required: {chunk.chunk_key}")
        if chunk.message_count <= 0:
            errors.append(f"message_count must be positive: {chunk.chunk_key}")
        if len(chunk.embedding) != EMBEDDING_DIMENSIONS:
            errors.append(f"embedding must have 1536 dimensions: {chunk.chunk_key}")
        if chunk.embedding_model != settings.embedding_model:
            errors.append(f"embedding_model mismatch: {chunk.chunk_key}")
    return errors


def _write_staging(
    staging_dir: Path,
    day: date,
    batch_id: str,
    messages: list[RawMessage],
    staged: StagedChunks,
    errors: list[str],
    settings: ChunkBatchSettings,
    started_at: datetime,
    status: str,
) -> dict[str, Any]:
    staging_dir.mkdir(parents=True, exist_ok=True)
    chunks_path = staging_dir / "chunks.jsonl"
    errors_path = staging_dir / "errors.jsonl"

    _write_jsonl(chunks_path, [_chunk_row(chunk) for chunk in staged.chunks])
    _write_jsonl(errors_path, [_error_row(error) for error in errors])
    manifest = {
        "actual_artifact_counts": {"chunks": len(staged.chunks)},
        "actual_chunks": len(staged.chunks),
        "chunk_batch_id": batch_id,
        "chunk_strategy_version": settings.chunk_strategy_version,
        "cost_unit_prices_used": "unknown",
        "day": day.isoformat(),
        "date_range": {"start": day.isoformat(), "end": day.isoformat()},
        "error_count": len(errors),
        "error_counts": {"chunks": len(errors)},
        "errors": errors,
        "estimated_cost": "unknown",
        "expected_artifact_counts": {"chunks": len(staged.chunks)},
        "expected_chunks": len(staged.chunks),
        "failed_days": [day.isoformat()] if errors else [],
        "fallback_counts": {"chunks": 0},
        "files": {
            "chunks.jsonl": _sha256(chunks_path),
            "errors.jsonl": _sha256(errors_path),
        },
        "finished_at": _now_iso(),
        "input_raw_counts": {"eligible": len(messages)},
        "only": "chunks",
        "staging_file_paths": {
            "chunks.jsonl": str(chunks_path),
            "errors.jsonl": str(errors_path),
        },
        "staging_file_sha256": {
            "chunks.jsonl": _sha256(chunks_path),
            "errors.jsonl": _sha256(errors_path),
        },
        "started_at": started_at.isoformat(),
        "status": status,
        "step_status": {"chunks": status},
        "summary_batch_id": None,
        "summary_strategy_version": None,
        "token_usage": {
            "chunk_embedding_input": "unknown",
            "embedding_output": 0,
            "summary_llm_input": 0,
            "summary_llm_output": 0,
        },
        "truncation_counts": {"chunks": staged.truncation_count},
        "batch_id": batch_id,
        "embedding_model": settings.embedding_model,
    }
    _write_manifest(staging_dir / "manifest.json", manifest)
    return manifest


def _commit_chunks(
    conn: Any,
    chunks: list[StagedChunk],
    strategy_version: str,
    start_at: datetime,
    end_at: datetime,
) -> int:
    conn.execute(
        """
        DELETE FROM conversation_chunks
        WHERE chunk_strategy_version = %(strategy_version)s
          AND start_at >= %(start_at)s
          AND start_at < %(end_at)s
        """,
        {"strategy_version": strategy_version, "start_at": start_at, "end_at": end_at},
    )
    for chunk in chunks:
        conn.execute(
            """
            INSERT INTO conversation_chunks (
              chunk_key,
              chunk_strategy_version,
              start_message_id,
              end_message_id,
              start_at,
              end_at,
              message_count,
              chunk_text,
              embedding,
              embedding_model,
              batch_id
            ) VALUES (
              %(chunk_key)s,
              %(chunk_strategy_version)s,
              %(start_message_id)s,
              %(end_message_id)s,
              %(start_at)s,
              %(end_at)s,
              %(message_count)s,
              %(chunk_text)s,
              %(embedding)s::vector,
              %(embedding_model)s,
              %(batch_id)s
            )
            """,
            {
                "chunk_key": chunk.chunk_key,
                "chunk_strategy_version": chunk.chunk_strategy_version,
                "start_message_id": chunk.start_message_id,
                "end_message_id": chunk.end_message_id,
                "start_at": chunk.start_at,
                "end_at": chunk.end_at,
                "message_count": chunk.message_count,
                "chunk_text": chunk.chunk_text,
                "embedding": _vector_literal(chunk.embedding),
                "embedding_model": chunk.embedding_model,
                "batch_id": chunk.batch_id,
            },
        )
    return len(chunks)


def _chunk_row(chunk: StagedChunk) -> dict[str, Any]:
    return {
        "chunk_key": chunk.chunk_key,
        "chunk_strategy_version": chunk.chunk_strategy_version,
        "start_message_id": chunk.start_message_id,
        "end_message_id": chunk.end_message_id,
        "start_at": chunk.start_at.isoformat(),
        "end_at": chunk.end_at.isoformat(),
        "message_count": chunk.message_count,
        "chunk_text": chunk.chunk_text,
        "embedding": chunk.embedding,
        "embedding_model": chunk.embedding_model,
        "batch_id": chunk.batch_id,
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _error_row(error: str) -> dict[str, Any]:
    return {
        "artifact_key": None,
        "error_summary": error,
        "error_type": "validation_error",
        "message_ids": [],
        "provider_request_id": None,
        "provider_response_id": None,
        "retry_count": 0,
        "step": "chunk_validation",
    }


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(str(value) for value in values) + "]"


def _fake_embedding(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values = []
    for index in range(EMBEDDING_DIMENSIONS):
        values.append(digest[index % len(digest)] / 255.0)
    return values


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _staging_dir(root: Path, day: date, batch_id: str) -> Path:
    return root / day.isoformat() / batch_id


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


def _validate_settings(settings: ChunkBatchSettings) -> None:
    if settings.min_chunk_messages > settings.chunk_window_messages:
        raise RuntimeError("MIN_CHUNK_MESSAGES must be <= CHUNK_WINDOW_MESSAGES")
    if settings.embedding_model != "text-embedding-3-small":
        raise RuntimeError("EMBEDDING_MODEL must be text-embedding-3-small")


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _positive_int(name: str) -> int:
    value = _required(name)
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be greater than 0")
    return parsed
