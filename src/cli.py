from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

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
