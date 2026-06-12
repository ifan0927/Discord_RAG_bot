from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

from dotenv import load_dotenv

from src.batch_check import check_batch_day
from src.batch_chunks import DeterministicFakeEmbeddingProvider, EmbeddingProvider, run_chunks_batch
from src.batch_dry_run import parse_day, run_batch_dry_run
from src.batch_summaries import run_summaries_batch
from src.jsonl_import import import_jsonl_directory
from src.migration import apply_schema, check_schema, database_url_from_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate_parser = subparsers.add_parser("migrate")
    migrate_subparsers = migrate_parser.add_subparsers(dest="migrate_command", required=True)
    migrate_subparsers.add_parser("up")
    migrate_subparsers.add_parser("check")

    import_parser = subparsers.add_parser("import-jsonl")
    import_parser.add_argument("--input-dir", required=True)
    import_parser.add_argument("--bot-user-id", type=int, default=None)

    run_batch_parser = subparsers.add_parser("run-batch")
    run_batch_parser.add_argument("--date", required=True)
    run_batch_parser.add_argument("--only", choices=("all", "chunks", "summaries"), default="all")
    run_batch_parser.add_argument("--dry-run", action="store_true")

    backfill_parser = subparsers.add_parser("backfill")
    backfill_parser.add_argument("--start", required=True)
    backfill_parser.add_argument("--end", required=True)
    backfill_parser.add_argument("--only", choices=("all", "chunks", "summaries"), required=True)
    backfill_parser.add_argument("--resume", action="store_true")
    backfill_parser.add_argument("--dry-run", action="store_true")

    check_batch_parser = subparsers.add_parser("check-batch")
    check_batch_parser.add_argument("--date", required=True)
    check_batch_parser.add_argument("--only", choices=("all", "chunks", "summaries"), default="all")
    check_batch_parser.add_argument("--json", action="store_true", required=True)

    args = parser.parse_args(argv)
    if args.command == "migrate":
        database_url = database_url_from_env()
        if args.migrate_command == "up":
            apply_schema(database_url)
            print("migration applied")
            return 0
        if args.migrate_command == "check":
            result = check_schema(database_url)
            print(result.to_json())
            return 0 if result.ok else 1
    if args.command == "import-jsonl":
        guild_id, channel_id = _import_target_ids_from_env()
        result = import_jsonl_directory(
            input_dir=Path(args.input_dir),
            database_url=database_url_from_env(),
            guild_id=guild_id,
            channel_id=channel_id,
            bot_user_id=args.bot_user_id,
        )
        print(result.summary())
        return 0
    if args.command == "run-batch":
        day = parse_day(args.date)
        if not args.dry_run:
            payload = _run_real_batch_day(
                database_url=database_url_from_env(),
                day=day,
                only=args.only,
                embedding_provider=_embedding_provider_from_env(),
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if payload["status"] in ("success", "success_empty") else 1
        result = run_batch_dry_run(
            database_url=database_url_from_env(),
            start_date=day,
            end_date=day,
            only=args.only,
            command="run-batch",
        )
        print(result.to_json())
        return 0
    if args.command == "backfill":
        if not args.dry_run:
            payload = _run_backfill(
                database_url=database_url_from_env(),
                start_date=parse_day(args.start),
                end_date=parse_day(args.end),
                only=args.only,
                resume=args.resume,
                embedding_provider=_embedding_provider_from_env(),
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 1 if payload["failed_days"] else 0
        result = run_batch_dry_run(
            database_url=database_url_from_env(),
            start_date=parse_day(args.start),
            end_date=parse_day(args.end),
            only=args.only,
            command="backfill",
        )
        print(result.to_json())
        return 0
    if args.command == "check-batch":
        try:
            result = check_batch_day(
                database_url=database_url_from_env(),
                day=parse_day(args.date),
                only=args.only,
            )
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "day": args.date,
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 1
        print(result.to_json())
        return 0 if result.ok else 1

    parser.error("unsupported command")
    return 2


def _import_target_ids_from_env() -> tuple[int, int]:
    load_dotenv()
    guild_id = os.getenv("GUILD_ID")
    channel_id = os.getenv("CHANNEL_ID")
    if not guild_id:
        raise RuntimeError("Missing required environment variable: GUILD_ID")
    if not channel_id:
        raise RuntimeError("Missing required environment variable: CHANNEL_ID")
    return int(guild_id), int(channel_id)


def _embedding_provider_from_env() -> EmbeddingProvider:
    load_dotenv()
    provider = os.getenv("BATCH_EMBEDDING_PROVIDER")
    if provider == "fake":
        return DeterministicFakeEmbeddingProvider()
    raise RuntimeError(
        "Missing supported BATCH_EMBEDDING_PROVIDER. Set BATCH_EMBEDDING_PROVIDER=fake for local "
        "validation, or run an ops issue that explicitly authorizes real provider calls."
    )


def _run_backfill(
    *,
    database_url: str,
    start_date: date,
    end_date: date,
    only: str,
    resume: bool,
    embedding_provider: EmbeddingProvider,
) -> dict[str, Any]:
    if end_date < start_date:
        raise RuntimeError("end date must be on or after start date")

    days = _date_range(start_date, end_date)
    results = []
    failed_days = []
    skipped_days = []
    for day in days:
        try:
            if resume:
                check = check_batch_day(database_url=database_url, day=day, only=only)
                if check.ok:
                    payload = {
                        "day": day.isoformat(),
                        "status": "skipped",
                        "reason": "resume_artifacts_match_expected_keys",
                        "check": json.loads(check.to_json()),
                    }
                    results.append(payload)
                    skipped_days.append(day.isoformat())
                    continue

            payload = _run_real_batch_day(
                database_url=database_url,
                day=day,
                only=only,
                embedding_provider=embedding_provider,
            )
            results.append(payload)
            if payload["status"] not in ("success", "success_empty"):
                failed_days.append(day.isoformat())
        except Exception as exc:
            results.append({"day": day.isoformat(), "status": "failed", "error": str(exc)})
            failed_days.append(day.isoformat())

    return {
        "command": "backfill",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "only": only,
        "resume": resume,
        "days": results,
        "skipped_days": skipped_days,
        "failed_days": failed_days,
        "status": "failed" if failed_days else "success",
    }


def _run_real_batch_day(
    *,
    database_url: str,
    day: date,
    only: str,
    embedding_provider: EmbeddingProvider,
) -> dict[str, Any]:
    if only == "chunks":
        result = run_chunks_batch(
            database_url=database_url,
            day=day,
            embedding_provider=embedding_provider,
        )
        return json.loads(result.to_json())
    if only == "summaries":
        result = run_summaries_batch(
            database_url=database_url,
            day=day,
            embedding_provider=embedding_provider,
        )
        return json.loads(result.to_json())

    chunks_result = run_chunks_batch(
        database_url=database_url,
        day=day,
        embedding_provider=embedding_provider,
    )
    if chunks_result.status not in ("success", "success_empty"):
        return json.loads(chunks_result.to_json())
    summaries_result = run_summaries_batch(
        database_url=database_url,
        day=day,
        embedding_provider=embedding_provider,
    )
    if summaries_result.status not in ("success", "success_empty"):
        manifest_path = _write_all_batch_manifest(
            day,
            "partial_failed",
            chunks_result,
            summaries_result,
        )
        _log_all_batch_event(day, "partial_failed", chunks_result, summaries_result, "batch_partial_failed")
        _log_all_batch_event(day, "partial_failed", chunks_result, summaries_result)
        return {
            "day": day.isoformat(),
            "status": "partial_failed",
            "manifest_path": str(manifest_path),
            "chunks": json.loads(chunks_result.to_json()),
            "summaries": json.loads(summaries_result.to_json()),
        }
    manifest_path = _write_all_batch_manifest(day, "success", chunks_result, summaries_result)
    _log_all_batch_event(day, "success", chunks_result, summaries_result)
    return {
        "day": day.isoformat(),
        "status": "success",
        "manifest_path": str(manifest_path),
        "chunks": json.loads(chunks_result.to_json()),
        "summaries": json.loads(summaries_result.to_json()),
    }


def _date_range(start_date: date, end_date: date) -> list[date]:
    days = []
    current = start_date
    while current <= end_date:
        days.append(current)
        current = current + timedelta(days=1)
    return days


def _write_all_batch_manifest(
    day: date,
    status: str,
    chunks_result: Any,
    summaries_result: Any,
) -> Path:
    staging_dir = Path(summaries_result.staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = staging_dir / "all_manifest.json"
    manifest = {
        "batch_id": f"all-{day.isoformat()}",
        "chunk_batch_id": chunks_result.batch_id,
        "summary_batch_id": summaries_result.batch_id,
        "day": day.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "only": "all",
        "status": status,
        "step_status": {
            "chunks": chunks_result.status,
            "summaries": summaries_result.status,
        },
        "chunks": json.loads(chunks_result.to_json()),
        "summaries": json.loads(summaries_result.to_json()),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _log_all_batch_event(
    day: date,
    status: str,
    chunks_result: Any,
    summaries_result: Any,
    event_name: str = "batch_finished",
) -> None:
    event = {
        "event": event_name,
        "run_id": f"all-{day.isoformat()}",
        "batch_id": f"all-{day.isoformat()}",
        "date": day.isoformat(),
        "only": "all",
        "status": status,
        "chunk_batch_id": chunks_result.batch_id,
        "summary_batch_id": summaries_result.batch_id,
        "step_status": {
            "chunks": chunks_result.status,
            "summaries": summaries_result.status,
        },
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(event, ensure_ascii=False, sort_keys=True), file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
