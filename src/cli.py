from __future__ import annotations

import argparse
import sys

from src.migration import apply_schema, check_schema, database_url_from_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate_parser = subparsers.add_parser("migrate")
    migrate_subparsers = migrate_parser.add_subparsers(dest="migrate_command", required=True)
    migrate_subparsers.add_parser("up")
    migrate_subparsers.add_parser("check")

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

    parser.error("unsupported command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
