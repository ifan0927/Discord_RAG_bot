from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

from src.message_normalization import MessageNormalizationInput, normalize_message
from src.migration import check_schema


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
  'jsonl_export'
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


@dataclass(frozen=True)
class ImportJsonlResult:
    files: int
    imported: int
    upserted: int
    eligible: int

    def summary(self) -> str:
        return (
            f"files={self.files} imported={self.imported} "
            f"upserted={self.upserted} eligible={self.eligible}"
        )


def import_jsonl_directory(
    input_dir: Path,
    database_url: str,
    guild_id: int,
    channel_id: int,
    bot_user_id: int | None = None,
    schema_checker: Callable[[str], Any] = check_schema,
    connect: Callable[[str], Any] | None = None,
) -> ImportJsonlResult:
    if not input_dir.is_dir():
        raise RuntimeError(f"Input directory does not exist: {input_dir}")

    schema = schema_checker(database_url)
    if not schema.ok:
        raise RuntimeError("Database schema check failed; run migrate up before import-jsonl")

    if connect is None:
        import psycopg

        connect = psycopg.connect

    files = 0
    imported = 0
    upserted = 0
    eligible = 0

    with connect(database_url) as conn:
        for path in sorted(input_dir.rglob("*.jsonl")):
            files += 1
            with path.open(encoding="utf-8") as handle:
                for line_number, raw_line in enumerate(handle, 1):
                    if not raw_line.strip():
                        continue
                    row = _parse_json_line(path, line_number, raw_line)
                    message = _message_input_from_row(
                        row,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        bot_user_id=bot_user_id,
                        path=path,
                        line_number=line_number,
                    )
                    normalized = normalize_message(message)
                    conn.execute(
                        RAW_MESSAGE_UPSERT_SQL,
                        {
                            "message_id": int(message.message_id),
                            "created_at": message.created_at,
                            "author_id": int(message.author_id),
                            "raw_content": message.raw_content,
                            "normalized_content": normalized.normalized_content,
                            "is_rag_eligible": normalized.is_rag_eligible,
                        },
                    )
                    imported += 1
                    upserted += 1
                    if normalized.is_rag_eligible:
                        eligible += 1

    return ImportJsonlResult(files=files, imported=imported, upserted=upserted, eligible=eligible)


def _parse_json_line(path: Path, line_number: int, raw_line: str) -> dict[str, Any]:
    try:
        row = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSONL row at {path}:{line_number}") from exc
    if not isinstance(row, dict):
        raise RuntimeError(f"JSONL row must be an object at {path}:{line_number}")
    return row


def _message_input_from_row(
    row: dict[str, Any],
    *,
    guild_id: int,
    channel_id: int,
    bot_user_id: int | None,
    path: Path,
    line_number: int,
) -> MessageNormalizationInput:
    message_id = _required(row, ("id", "message_id"), path, line_number)
    row_guild_id = _required(row, ("guild_id",), path, line_number)
    row_channel_id = _required(row, ("channel_id",), path, line_number)
    author_id = _required(row, ("author_id",), path, line_number)
    raw_content = _required(row, ("content", "raw_content"), path, line_number)
    created_at = _parse_datetime(_required(row, ("created_at",), path, line_number), path, line_number)

    if not isinstance(raw_content, str):
        raise RuntimeError(f"Message content must be a string at {path}:{line_number}")

    return MessageNormalizationInput(
        message_id=int(message_id),
        created_at=created_at,
        author_id=int(author_id),
        raw_content=raw_content,
        is_bot_author=_truthy_any(row, ("is_bot_author", "author_is_bot", "author_bot")),
        is_dm=row_guild_id is None,
        is_thread=_truthy_any(row, ("is_thread", "thread_id")),
        is_wrong_guild=int(row_guild_id) != guild_id,
        is_wrong_channel=int(row_channel_id) != channel_id,
        is_bot_mentioned=_truthy_any(row, ("is_bot_mentioned", "mentions_bot")),
        has_attachments=bool(row.get("attachments")),
        has_stickers=bool(row.get("stickers") or row.get("sticker_items")),
        has_embeds=bool(row.get("embeds")),
        bot_user_id=bot_user_id,
        is_system_message=_is_system_message(row),
        is_confirmed_human_text=True,
    )


def _required(
    row: dict[str, Any], names: tuple[str, ...], path: Path, line_number: int
) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    joined = "/".join(names)
    raise RuntimeError(f"Missing required JSONL field {joined} at {path}:{line_number}")


def _parse_datetime(value: Any, path: Path, line_number: int) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError(f"created_at must be an ISO string at {path}:{line_number}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"created_at must be an ISO string at {path}:{line_number}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _truthy_any(row: dict[str, Any], names: tuple[str, ...]) -> bool:
    return any(_as_bool(row.get(name)) for name in names)


def _is_system_message(row: dict[str, Any]) -> bool:
    if _as_bool(row.get("is_system_message")):
        return True
    message_type = row.get("type")
    return message_type not in (None, 0, "0", "default")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)
