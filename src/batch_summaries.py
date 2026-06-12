from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.batch_chunks import EmbeddingProvider, RawMessage
from src.migration import check_schema


TAIPEI = ZoneInfo("Asia/Taipei")
EMBEDDING_DIMENSIONS = 1536


@dataclass(frozen=True)
class SummaryBatchSettings:
    summary_strategy_version: str
    embedding_model: str
    min_summary_llm_messages: int
    summary_input_token_budget: int
    summary_max_output_tokens: int
    summary_fallback_max_chars: int
    summary_llm_max_retries: int
    batch_staging_dir: Path


@dataclass(frozen=True)
class StagedSummary:
    summary_key: str
    summary_strategy_version: str
    hour_start: datetime
    hour_end: datetime
    start_message_id: int
    end_message_id: int
    summary_text: str
    embedding: list[float]
    embedding_model: str
    batch_id: str


@dataclass(frozen=True)
class StagedSummaries:
    summaries: list[StagedSummary]
    fallback_count: int
    truncation_count: int
    summary_llm_error_count: int


@dataclass(frozen=True)
class SummaryBatchResult:
    day: str
    status: str
    batch_id: str
    staged_summaries: int
    committed_summaries: int
    staging_dir: str
    manifest_path: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "day": self.day,
                "status": self.status,
                "batch_id": self.batch_id,
                "staged_summaries": self.staged_summaries,
                "committed_summaries": self.committed_summaries,
                "staging_dir": self.staging_dir,
                "manifest_path": self.manifest_path,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )


class SummaryProvider(Protocol):
    def summarize(self, text: str, *, max_output_tokens: int) -> str:
        ...


def load_summary_batch_settings() -> SummaryBatchSettings:
    load_dotenv()
    return SummaryBatchSettings(
        summary_strategy_version=_required("SUMMARY_STRATEGY_VERSION"),
        embedding_model=_required("EMBEDDING_MODEL"),
        min_summary_llm_messages=_positive_int("MIN_SUMMARY_LLM_MESSAGES"),
        summary_input_token_budget=_positive_int("SUMMARY_INPUT_TOKEN_BUDGET"),
        summary_max_output_tokens=_positive_int("SUMMARY_MAX_OUTPUT_TOKENS"),
        summary_fallback_max_chars=_positive_int("SUMMARY_FALLBACK_MAX_CHARS"),
        summary_llm_max_retries=_positive_int("SUMMARY_LLM_MAX_RETRIES"),
        batch_staging_dir=Path(_required("BATCH_STAGING_DIR")),
    )


def run_summaries_batch(
    *,
    database_url: str,
    day: date,
    embedding_provider: EmbeddingProvider,
    summary_provider: SummaryProvider | None = None,
    settings: SummaryBatchSettings | None = None,
    schema_checker: Any = check_schema,
    connect: Any | None = None,
) -> SummaryBatchResult:
    settings = settings or load_summary_batch_settings()
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
            settings.summary_strategy_version,
            start_at,
            end_at,
            settings.batch_staging_dir,
        )

    staging_dir = _staging_dir(settings.batch_staging_dir, day, batch_id)
    expected_summaries = _expected_summary_count(messages)
    _log_batch_event(
        "batch_started",
        day=day,
        batch_id=batch_id,
        settings=settings,
        status="started",
        input_count=len(messages),
        expected_count=expected_summaries,
        started_at=started_at,
    )
    _log_batch_event(
        "batch_step_started",
        day=day,
        batch_id=batch_id,
        settings=settings,
        status="started",
        input_count=len(messages),
        expected_count=expected_summaries,
        started_at=started_at,
    )

    staged = _stage_summaries(day, messages, batch_id, settings, embedding_provider, summary_provider)
    validation_errors = _validate_summaries(day, staged.summaries, expected_summaries, settings)
    manifest = _write_staging(
        staging_dir,
        day,
        batch_id,
        messages,
        staged,
        expected_summaries,
        validation_errors,
        settings,
        started_at,
        status="failed" if validation_errors else "staged",
    )

    if validation_errors:
        _log_batch_event(
            "artifact_validation_failed",
            day=day,
            batch_id=batch_id,
            settings=settings,
            status="failed",
            input_count=len(messages),
            output_count=len(staged.summaries),
            expected_count=expected_summaries,
            actual_count=len(staged.summaries),
            error_count=len(validation_errors),
            fallback_count=staged.fallback_count,
            truncation_count=staged.truncation_count,
            summary_llm_error_count=staged.summary_llm_error_count,
            started_at=started_at,
        )
        _log_batch_event(
            "batch_step_failed",
            day=day,
            batch_id=batch_id,
            settings=settings,
            status="failed",
            input_count=len(messages),
            output_count=len(staged.summaries),
            expected_count=expected_summaries,
            actual_count=len(staged.summaries),
            error_count=len(validation_errors),
            fallback_count=staged.fallback_count,
            truncation_count=staged.truncation_count,
            summary_llm_error_count=staged.summary_llm_error_count,
            started_at=started_at,
        )
        _log_batch_event(
            "batch_finished",
            day=day,
            batch_id=batch_id,
            settings=settings,
            status="failed",
            input_count=len(messages),
            output_count=len(staged.summaries),
            expected_count=expected_summaries,
            actual_count=len(staged.summaries),
            error_count=len(validation_errors),
            fallback_count=staged.fallback_count,
            truncation_count=staged.truncation_count,
            summary_llm_error_count=staged.summary_llm_error_count,
            started_at=started_at,
        )
        return SummaryBatchResult(
            day=day.isoformat(),
            status="failed",
            batch_id=batch_id,
            staged_summaries=len(staged.summaries),
            committed_summaries=0,
            staging_dir=str(staging_dir),
            manifest_path=str(staging_dir / "manifest.json"),
        )

    with connect(database_url) as conn:
        committed = _commit_summaries(
            conn, staged.summaries, settings.summary_strategy_version, start_at, end_at
        )

    final_status = "success_empty" if len(staged.summaries) == 0 else "success"
    _log_batch_event(
        "artifact_committed",
        day=day,
        batch_id=batch_id,
        settings=settings,
        status=final_status,
        input_count=len(messages),
        output_count=committed,
        expected_count=expected_summaries,
        actual_count=len(staged.summaries),
        fallback_count=staged.fallback_count,
        truncation_count=staged.truncation_count,
        summary_llm_error_count=staged.summary_llm_error_count,
        started_at=started_at,
    )
    _log_batch_event(
        "batch_step_finished",
        day=day,
        batch_id=batch_id,
        settings=settings,
        status=final_status,
        input_count=len(messages),
        output_count=committed,
        expected_count=expected_summaries,
        actual_count=len(staged.summaries),
        fallback_count=staged.fallback_count,
        truncation_count=staged.truncation_count,
        summary_llm_error_count=staged.summary_llm_error_count,
        started_at=started_at,
    )
    _log_batch_event(
        "batch_finished",
        day=day,
        batch_id=batch_id,
        settings=settings,
        status=final_status,
        input_count=len(messages),
        output_count=committed,
        expected_count=expected_summaries,
        actual_count=len(staged.summaries),
        fallback_count=staged.fallback_count,
        truncation_count=staged.truncation_count,
        summary_llm_error_count=staged.summary_llm_error_count,
        started_at=started_at,
    )
    _write_manifest(
        staging_dir / "manifest.json",
        {**manifest, "status": final_status, "step_status": {"summaries": final_status}, "finished_at": _now_iso()},
    )
    return SummaryBatchResult(
        day=day.isoformat(),
        status=final_status,
        batch_id=batch_id,
        staged_summaries=len(staged.summaries),
        committed_summaries=committed,
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
        FROM hourly_summaries
        WHERE summary_strategy_version = %(strategy_version)s
          AND hour_start >= %(start_at)s
          AND hour_start < %(end_at)s
          AND batch_id LIKE %(prefix_like)s
        """,
        {
            "strategy_version": strategy_version,
            "start_at": start_at,
            "end_at": end_at,
            "prefix_like": f"summary-{day.isoformat()}-r%",
        },
    ).fetchall()
    prefix = f"summary-{day.isoformat()}-r"
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


def _stage_summaries(
    day: date,
    messages: list[RawMessage],
    batch_id: str,
    settings: SummaryBatchSettings,
    embedding_provider: EmbeddingProvider,
    summary_provider: SummaryProvider | None,
) -> StagedSummaries:
    groups = _messages_by_taipei_hour(messages)
    text_results = [
        _summary_text(group, settings, summary_provider)
        for _hour_start, group in groups
    ]
    texts = [text for text, _fallback, _truncated, _error_count in text_results]
    embeddings = embedding_provider.embed(texts) if texts else []
    if len(embeddings) != len(texts):
        embeddings = [[] for _ in texts]

    summaries = []
    for (hour_start, group), (text, _fallback, _truncated, _error_count), embedding in zip(
        groups, text_results, embeddings
    ):
        start = group[0]
        end = group[-1]
        hour_end = hour_start + timedelta(hours=1)
        summaries.append(
            StagedSummary(
                summary_key=(
                    f"summary:{settings.summary_strategy_version}:"
                    f"{hour_start.isoformat()}:{start.message_id}:{end.message_id}"
                ),
                summary_strategy_version=settings.summary_strategy_version,
                hour_start=hour_start.astimezone(timezone.utc),
                hour_end=hour_end.astimezone(timezone.utc),
                start_message_id=start.message_id,
                end_message_id=end.message_id,
                summary_text=text,
                embedding=embedding,
                embedding_model=settings.embedding_model,
                batch_id=batch_id,
            )
        )

    return StagedSummaries(
        summaries=summaries,
        fallback_count=sum(1 for _text, fallback, _truncated, _error_count in text_results if fallback),
        truncation_count=sum(1 for _text, _fallback, truncated, _error_count in text_results if truncated),
        summary_llm_error_count=sum(error_count for _text, _fallback, _truncated, error_count in text_results),
    )


def _messages_by_taipei_hour(messages: list[RawMessage]) -> list[tuple[datetime, list[RawMessage]]]:
    groups: dict[datetime, list[RawMessage]] = {}
    for message in messages:
        taipei = message.created_at.astimezone(TAIPEI)
        hour_start = taipei.replace(minute=0, second=0, microsecond=0)
        groups.setdefault(hour_start, []).append(message)
    return [(hour_start, groups[hour_start]) for hour_start in sorted(groups)]


def _summary_text(
    messages: list[RawMessage],
    settings: SummaryBatchSettings,
    summary_provider: SummaryProvider | None,
) -> tuple[str, bool, bool, int]:
    raw_text, raw_truncated = _raw_lines(messages, settings.summary_fallback_max_chars)
    if len(messages) < settings.min_summary_llm_messages:
        return raw_text, True, raw_truncated, 0
    if summary_provider is None:
        return raw_text, True, raw_truncated, 0

    prompt_input, prompt_truncated = _raw_lines(messages, settings.summary_input_token_budget)
    error_count = 0
    for _attempt in range(settings.summary_llm_max_retries + 1):
        try:
            summary = summary_provider.summarize(
                prompt_input,
                max_output_tokens=settings.summary_max_output_tokens,
            )
        except Exception:
            error_count += 1
            continue
        if summary.strip():
            return summary.strip(), False, prompt_truncated, error_count
    return raw_text, True, raw_truncated or prompt_truncated, error_count


def _raw_lines(messages: list[RawMessage], max_chars: int) -> tuple[str, bool]:
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


def _expected_summary_count(messages: list[RawMessage]) -> int:
    return len(_messages_by_taipei_hour(messages))


def _validate_summaries(
    day: date, summaries: list[StagedSummary], expected_count: int, settings: SummaryBatchSettings
) -> list[str]:
    errors = []
    if len(summaries) != expected_count:
        errors.append(f"summary count mismatch: expected {expected_count}, actual {len(summaries)}")
    keys = [summary.summary_key for summary in summaries]
    if len(keys) != len(set(keys)):
        errors.append("summary_key values must be unique")
    for summary in summaries:
        expected_prefix = f"summary:{settings.summary_strategy_version}:"
        if not summary.summary_key.startswith(expected_prefix):
            errors.append(f"invalid summary_key: {summary.summary_key}")
        if summary.hour_start.astimezone(TAIPEI).date() != day:
            errors.append(f"summary hour is outside target day: {summary.summary_key}")
        if not summary.summary_text:
            errors.append(f"summary_text is required: {summary.summary_key}")
        if len(summary.embedding) != EMBEDDING_DIMENSIONS:
            errors.append(f"embedding must have 1536 dimensions: {summary.summary_key}")
        if summary.embedding_model != settings.embedding_model:
            errors.append(f"embedding_model mismatch: {summary.summary_key}")
    return errors


def _write_staging(
    staging_dir: Path,
    day: date,
    batch_id: str,
    messages: list[RawMessage],
    staged: StagedSummaries,
    expected_summaries: int,
    errors: list[str],
    settings: SummaryBatchSettings,
    started_at: datetime,
    status: str,
) -> dict[str, Any]:
    staging_dir.mkdir(parents=True, exist_ok=True)
    summaries_path = staging_dir / "summaries.jsonl"
    errors_path = staging_dir / "errors.jsonl"

    _write_jsonl(summaries_path, [_summary_row(summary) for summary in staged.summaries])
    _write_jsonl(errors_path, [_error_row(error) for error in errors])
    manifest = {
        "actual_artifact_counts": {"summaries": len(staged.summaries)},
        "actual_summaries": len(staged.summaries),
        "batch_id": batch_id,
        "chunk_batch_id": None,
        "cost_unit_prices_used": "unknown",
        "day": day.isoformat(),
        "date_range": {"start": day.isoformat(), "end": day.isoformat()},
        "embedding_model": settings.embedding_model,
        "error_count": len(errors),
        "error_counts": {"summaries": len(errors)},
        "errors": errors,
        "estimated_cost": "unknown",
        "expected_artifact_counts": {"summaries": expected_summaries},
        "expected_summaries": expected_summaries,
        "failed_days": [day.isoformat()] if errors else [],
        "fallback_counts": {"summaries": staged.fallback_count},
        "files": {
            "summaries.jsonl": _sha256(summaries_path),
            "errors.jsonl": _sha256(errors_path),
        },
        "finished_at": _now_iso(),
        "input_raw_counts": {"eligible": len(messages)},
        "only": "summaries",
        "staging_file_paths": {
            "summaries.jsonl": str(summaries_path),
            "errors.jsonl": str(errors_path),
        },
        "staging_file_sha256": {
            "summaries.jsonl": _sha256(summaries_path),
            "errors.jsonl": _sha256(errors_path),
        },
        "started_at": started_at.isoformat(),
        "status": status,
        "step_status": {"summaries": status},
        "summary_batch_id": batch_id,
        "summary_llm_error_count": staged.summary_llm_error_count,
        "summary_strategy_version": settings.summary_strategy_version,
        "token_usage": {
            "chunk_embedding_input": 0,
            "embedding_output": 0,
            "summary_llm_input": "unknown",
            "summary_llm_output": "unknown",
        },
        "truncation_counts": {"summaries": staged.truncation_count},
    }
    _write_manifest(staging_dir / "manifest.json", manifest)
    return manifest


def _commit_summaries(
    conn: Any,
    summaries: list[StagedSummary],
    strategy_version: str,
    start_at: datetime,
    end_at: datetime,
) -> int:
    conn.execute(
        """
        DELETE FROM hourly_summaries
        WHERE summary_strategy_version = %(strategy_version)s
          AND hour_start >= %(start_at)s
          AND hour_start < %(end_at)s
        """,
        {"strategy_version": strategy_version, "start_at": start_at, "end_at": end_at},
    )
    for summary in summaries:
        conn.execute(
            """
            INSERT INTO hourly_summaries (
              summary_key,
              summary_strategy_version,
              hour_start,
              hour_end,
              start_message_id,
              end_message_id,
              summary_text,
              embedding,
              embedding_model,
              batch_id
            ) VALUES (
              %(summary_key)s,
              %(summary_strategy_version)s,
              %(hour_start)s,
              %(hour_end)s,
              %(start_message_id)s,
              %(end_message_id)s,
              %(summary_text)s,
              %(embedding)s::vector,
              %(embedding_model)s,
              %(batch_id)s
            )
            """,
            {
                "summary_key": summary.summary_key,
                "summary_strategy_version": summary.summary_strategy_version,
                "hour_start": summary.hour_start,
                "hour_end": summary.hour_end,
                "start_message_id": summary.start_message_id,
                "end_message_id": summary.end_message_id,
                "summary_text": summary.summary_text,
                "embedding": _vector_literal(summary.embedding),
                "embedding_model": summary.embedding_model,
                "batch_id": summary.batch_id,
            },
        )
    return len(summaries)


def _summary_row(summary: StagedSummary) -> dict[str, Any]:
    return {
        "summary_key": summary.summary_key,
        "summary_strategy_version": summary.summary_strategy_version,
        "hour_start": summary.hour_start.isoformat(),
        "hour_end": summary.hour_end.isoformat(),
        "start_message_id": summary.start_message_id,
        "end_message_id": summary.end_message_id,
        "summary_text": summary.summary_text,
        "embedding": summary.embedding,
        "embedding_model": summary.embedding_model,
        "batch_id": summary.batch_id,
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
        "step": "summary_validation",
    }


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _log_batch_event(
    event_name: str,
    *,
    day: date,
    batch_id: str,
    settings: SummaryBatchSettings,
    status: str,
    input_count: int,
    expected_count: int,
    started_at: datetime,
    output_count: int = 0,
    actual_count: int = 0,
    error_count: int = 0,
    fallback_count: int = 0,
    truncation_count: int = 0,
    summary_llm_error_count: int = 0,
) -> None:
    finished_at = datetime.now(timezone.utc)
    event = {
        "event": event_name,
        "run_id": batch_id,
        "batch_id": batch_id,
        "date": day.isoformat(),
        "only": "summaries",
        "strategy_version": settings.summary_strategy_version,
        "status": status,
        "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
        "input_count": input_count,
        "output_count": output_count,
        "expected_count": expected_count,
        "actual_count": actual_count,
        "error_count": error_count,
        "fallback_count": fallback_count,
        "truncation_count": truncation_count,
        "summary_llm_error_count": summary_llm_error_count,
        "retry_count": 0,
        "token_input": "unknown",
        "token_output": "unknown",
        "estimated_cost": "unknown",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
    }
    print(json.dumps(event, ensure_ascii=False, sort_keys=True), file=sys.stderr)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(str(value) for value in values) + "]"


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


def _validate_settings(settings: SummaryBatchSettings) -> None:
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
