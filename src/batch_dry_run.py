from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from typing import Any, Callable
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.migration import check_schema


TAIPEI = ZoneInfo("Asia/Taipei")
TOKEN_ASSUMPTIONS_PER_MESSAGE = (15, 30, 60)


@dataclass(frozen=True)
class BatchDryRunSettings:
    chunk_strategy_version: str
    summary_strategy_version: str
    chunk_window_messages: int
    chunk_stride_messages: int
    min_chunk_messages: int
    min_summary_llm_messages: int
    summary_input_token_budget: int
    summary_max_output_tokens: int
    embedding_price_per_1m_tokens: Decimal | None
    summary_input_price_per_1m_tokens: Decimal | None
    summary_output_price_per_1m_tokens: Decimal | None


@dataclass(frozen=True)
class DayEstimate:
    day: str
    eligible_messages: int
    expected_chunks: int
    expected_summaries: int
    token_estimate: dict[str, dict[str, int]]
    estimated_cost_usd: dict[str, str] | str


@dataclass(frozen=True)
class BatchDryRunResult:
    command: str
    only: str
    dry_run: bool
    start_date: str
    end_date: str
    totals: dict[str, Any]
    days: list[DayEstimate]
    warnings: list[str]

    def to_json(self) -> str:
        return json.dumps(
            {
                "command": self.command,
                "only": self.only,
                "dry_run": self.dry_run,
                "start_date": self.start_date,
                "end_date": self.end_date,
                "totals": self.totals,
                "days": [
                    {
                        "day": day.day,
                        "eligible_messages": day.eligible_messages,
                        "expected_chunks": day.expected_chunks,
                        "expected_summaries": day.expected_summaries,
                        "token_estimate": day.token_estimate,
                        "estimated_cost_usd": day.estimated_cost_usd,
                    }
                    for day in self.days
                ],
                "warnings": self.warnings,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )


def load_dry_run_settings() -> BatchDryRunSettings:
    load_dotenv()
    return BatchDryRunSettings(
        chunk_strategy_version=_required("CHUNK_STRATEGY_VERSION"),
        summary_strategy_version=_required("SUMMARY_STRATEGY_VERSION"),
        chunk_window_messages=_positive_int("CHUNK_WINDOW_MESSAGES"),
        chunk_stride_messages=_positive_int("CHUNK_STRIDE_MESSAGES"),
        min_chunk_messages=_positive_int("MIN_CHUNK_MESSAGES"),
        min_summary_llm_messages=_positive_int("MIN_SUMMARY_LLM_MESSAGES"),
        summary_input_token_budget=_positive_int("SUMMARY_INPUT_TOKEN_BUDGET"),
        summary_max_output_tokens=_positive_int("SUMMARY_MAX_OUTPUT_TOKENS"),
        embedding_price_per_1m_tokens=_optional_decimal("EMBEDDING_PRICE_PER_1M_TOKENS"),
        summary_input_price_per_1m_tokens=_optional_decimal("SUMMARY_INPUT_PRICE_PER_1M_TOKENS"),
        summary_output_price_per_1m_tokens=_optional_decimal("SUMMARY_OUTPUT_PRICE_PER_1M_TOKENS"),
    )


def run_batch_dry_run(
    *,
    database_url: str,
    start_date: date,
    end_date: date,
    only: str,
    command: str,
    settings: BatchDryRunSettings | None = None,
    schema_checker: Callable[[str], Any] = check_schema,
    connect: Callable[[str], Any] | None = None,
) -> BatchDryRunResult:
    if end_date < start_date:
        raise RuntimeError("end date must be on or after start date")
    if only not in ("all", "chunks", "summaries"):
        raise RuntimeError("only must be one of: all, chunks, summaries")

    settings = settings or load_dry_run_settings()
    _validate_chunk_settings(settings)

    schema = schema_checker(database_url)
    if not schema.ok:
        raise RuntimeError("Database schema check failed; run migrate up before dry-run")

    if connect is None:
        import psycopg

        connect = psycopg.connect

    days = _date_range(start_date, end_date)
    start_at, end_at = _utc_bounds(start_date, end_date)
    with connect(database_url) as conn:
        day_counts = _eligible_counts_by_day(conn, start_at, end_at)
        hour_counts = _eligible_counts_by_hour(conn, start_at, end_at)

    day_estimates = [
        _estimate_day(day, day_counts.get(day, 0), hour_counts.get(day, []), only, settings)
        for day in days
    ]
    warnings = _warnings(settings, day_estimates)
    return BatchDryRunResult(
        command=command,
        only=only,
        dry_run=True,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        totals=_totals(day_estimates, warnings),
        days=day_estimates,
        warnings=warnings,
    )


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid date: {value}") from exc


def _estimate_day(
    day: date,
    eligible_messages: int,
    hour_counts: list[int],
    only: str,
    settings: BatchDryRunSettings,
) -> DayEstimate:
    expected_chunks = _expected_chunk_count(eligible_messages, settings)
    expected_summaries = len([count for count in hour_counts if count > 0])
    token_estimate = {
        str(tokens_per_message): _token_totals(
            eligible_messages,
            hour_counts,
            expected_summaries,
            only,
            tokens_per_message,
            settings,
        )
        for tokens_per_message in TOKEN_ASSUMPTIONS_PER_MESSAGE
    }
    return DayEstimate(
        day=day.isoformat(),
        eligible_messages=eligible_messages,
        expected_chunks=expected_chunks if only in ("all", "chunks") else 0,
        expected_summaries=expected_summaries if only in ("all", "summaries") else 0,
        token_estimate=token_estimate,
        estimated_cost_usd=_cost_estimate(token_estimate, settings),
    )


def _expected_chunk_count(eligible_messages: int, settings: BatchDryRunSettings) -> int:
    if eligible_messages < settings.min_chunk_messages:
        return 0
    return 1 + ((eligible_messages - settings.min_chunk_messages) // settings.chunk_stride_messages)


def _token_totals(
    eligible_messages: int,
    hour_counts: list[int],
    expected_summaries: int,
    only: str,
    tokens_per_message: int,
    settings: BatchDryRunSettings,
) -> dict[str, int]:
    chunk_embedding = 0
    if only in ("all", "chunks"):
        chunk_embedding = _chunk_embedding_tokens(eligible_messages, tokens_per_message, settings)

    summary_input = 0
    summary_output = 0
    summary_embedding = 0
    if only in ("all", "summaries"):
        summary_input = sum(
            min(count * tokens_per_message, settings.summary_input_token_budget)
            for count in hour_counts
            if count >= settings.min_summary_llm_messages
        )
        llm_summary_count = len(
            [count for count in hour_counts if count >= settings.min_summary_llm_messages]
        )
        summary_output = llm_summary_count * settings.summary_max_output_tokens
        summary_embedding = sum(
            min(count * tokens_per_message, settings.summary_input_token_budget)
            if count < settings.min_summary_llm_messages
            else settings.summary_max_output_tokens
            for count in hour_counts
            if count > 0
        )

    return {
        "chunk_embedding_tokens": chunk_embedding,
        "summary_llm_input_tokens": summary_input,
        "summary_llm_output_tokens": summary_output,
        "summary_embedding_tokens": summary_embedding,
        "embedding_tokens_total": chunk_embedding + summary_embedding,
    }


def _chunk_embedding_tokens(
    eligible_messages: int, tokens_per_message: int, settings: BatchDryRunSettings
) -> int:
    total = 0
    start = 0
    while eligible_messages - start >= settings.min_chunk_messages:
        total += min(settings.chunk_window_messages, eligible_messages - start) * tokens_per_message
        start += settings.chunk_stride_messages
    return total


def _cost_estimate(
    token_estimate: dict[str, dict[str, int]], settings: BatchDryRunSettings
) -> dict[str, str] | str:
    if _missing_price_names(settings, token_estimate):
        return "unknown"

    costs: dict[str, str] = {}
    for key, tokens in token_estimate.items():
        cost = (
            Decimal(tokens["embedding_tokens_total"]) * (settings.embedding_price_per_1m_tokens or Decimal("0"))
            + Decimal(tokens["summary_llm_input_tokens"])
            * (settings.summary_input_price_per_1m_tokens or Decimal("0"))
            + Decimal(tokens["summary_llm_output_tokens"])
            * (settings.summary_output_price_per_1m_tokens or Decimal("0"))
        ) / Decimal(1_000_000)
        costs[key] = str(cost.quantize(Decimal("0.000001")))
    return costs


def _totals(day_estimates: list[DayEstimate], warnings: list[str]) -> dict[str, Any]:
    total_tokens: dict[str, dict[str, int]] = {
        str(tokens): {
            "chunk_embedding_tokens": 0,
            "summary_llm_input_tokens": 0,
            "summary_llm_output_tokens": 0,
            "summary_embedding_tokens": 0,
            "embedding_tokens_total": 0,
        }
        for tokens in TOKEN_ASSUMPTIONS_PER_MESSAGE
    }
    for day in day_estimates:
        for key, values in day.token_estimate.items():
            for field, value in values.items():
                total_tokens[key][field] += value

    return {
        "eligible_messages": sum(day.eligible_messages for day in day_estimates),
        "expected_chunks": sum(day.expected_chunks for day in day_estimates),
        "expected_summaries": sum(day.expected_summaries for day in day_estimates),
        "token_estimate": total_tokens,
        "estimated_cost_usd": "unknown" if warnings else _total_cost(day_estimates),
    }


def _total_cost(day_estimates: list[DayEstimate]) -> dict[str, str] | str:
    totals = {str(tokens): Decimal("0") for tokens in TOKEN_ASSUMPTIONS_PER_MESSAGE}
    for day in day_estimates:
        if not isinstance(day.estimated_cost_usd, dict):
            return "unknown"
        for key, value in day.estimated_cost_usd.items():
            totals[key] += Decimal(value)
    return {key: str(value.quantize(Decimal("0.000001"))) for key, value in totals.items()}


def _eligible_counts_by_day(conn: Any, start_at: datetime, end_at: datetime) -> dict[date, int]:
    rows = conn.execute(
        """
        SELECT (created_at AT TIME ZONE 'Asia/Taipei')::date AS day, count(*)
        FROM raw_messages
        WHERE is_rag_eligible = true
          AND created_at >= %(start_at)s
          AND created_at < %(end_at)s
        GROUP BY day
        """,
        {"start_at": start_at, "end_at": end_at},
    ).fetchall()
    return {_row_day(row[0]): int(row[1]) for row in rows}


def _eligible_counts_by_hour(
    conn: Any, start_at: datetime, end_at: datetime
) -> dict[date, list[int]]:
    rows = conn.execute(
        """
        SELECT
          (created_at AT TIME ZONE 'Asia/Taipei')::date AS day,
          date_trunc('hour', created_at AT TIME ZONE 'Asia/Taipei') AS hour_start,
          count(*)
        FROM raw_messages
        WHERE is_rag_eligible = true
          AND created_at >= %(start_at)s
          AND created_at < %(end_at)s
        GROUP BY day, hour_start
        """,
        {"start_at": start_at, "end_at": end_at},
    ).fetchall()
    counts: dict[date, list[int]] = {}
    for row in rows:
        counts.setdefault(_row_day(row[0]), []).append(int(row[2]))
    return counts


def _utc_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    start_at = datetime.combine(start_date, time.min, tzinfo=TAIPEI).astimezone(timezone.utc)
    end_at = datetime.combine(end_date + _one_day(), time.min, tzinfo=TAIPEI).astimezone(timezone.utc)
    return start_at, end_at


def _one_day() -> timedelta:
    return timedelta(days=1)


def _date_range(start_date: date, end_date: date) -> list[date]:
    days: list[date] = []
    current = start_date
    while current <= end_date:
        days.append(current)
        current = current + _one_day()
    return days


def _row_day(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise RuntimeError(f"Unsupported day value from database: {value!r}")


def _warnings(settings: BatchDryRunSettings, day_estimates: list[DayEstimate]) -> list[str]:
    missing = sorted(
        {
            name
            for day in day_estimates
            for name in _missing_price_names(settings, day.token_estimate)
        }
    )
    if not missing:
        return []
    return [f"cost estimate is unknown because unit prices are missing: {', '.join(missing)}"]


def _missing_price_names(
    settings: BatchDryRunSettings, token_estimate: dict[str, dict[str, int]]
) -> list[str]:
    needs_embedding = any(tokens["embedding_tokens_total"] > 0 for tokens in token_estimate.values())
    needs_summary_input = any(tokens["summary_llm_input_tokens"] > 0 for tokens in token_estimate.values())
    needs_summary_output = any(tokens["summary_llm_output_tokens"] > 0 for tokens in token_estimate.values())

    missing = []
    if needs_embedding and settings.embedding_price_per_1m_tokens is None:
        missing.append("EMBEDDING_PRICE_PER_1M_TOKENS")
    if needs_summary_input and settings.summary_input_price_per_1m_tokens is None:
        missing.append("SUMMARY_INPUT_PRICE_PER_1M_TOKENS")
    if needs_summary_output and settings.summary_output_price_per_1m_tokens is None:
        missing.append("SUMMARY_OUTPUT_PRICE_PER_1M_TOKENS")
    return missing


def _validate_chunk_settings(settings: BatchDryRunSettings) -> None:
    if settings.min_chunk_messages > settings.chunk_window_messages:
        raise RuntimeError("MIN_CHUNK_MESSAGES must be <= CHUNK_WINDOW_MESSAGES")


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


def _optional_decimal(name: str) -> Decimal | None:
    value = os.getenv(name)
    if not value:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise RuntimeError(f"{name} must be a decimal number") from exc
    if parsed < 0:
        raise RuntimeError(f"{name} must not be negative")
    return parsed
