from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

from src.batch_dry_run import parse_day, run_batch_dry_run
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
        if not args.dry_run:
            raise RuntimeError("run-batch currently supports only --dry-run")
        day = parse_day(args.date)
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
            raise RuntimeError("backfill currently supports only --dry-run")
        result = run_batch_dry_run(
            database_url=database_url_from_env(),
            start_date=parse_day(args.start),
            end_date=parse_day(args.end),
            only=args.only,
            command="backfill",
        )
        print(result.to_json())
        return 0

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


if __name__ == "__main__":
    sys.exit(main())
