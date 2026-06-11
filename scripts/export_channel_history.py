import argparse
import asyncio
import json
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import aiofiles
import discord
from dotenv import dotenv_values


@dataclass(frozen=True)
class MessageStat:
    day: date
    author_name: str
    has_content: bool


@dataclass(frozen=True)
class ChannelExportResult:
    channel_id: int
    ok: bool
    output_dir: Path
    total_messages: int = 0
    messages_with_content: int = 0
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Discord channel history with a bot token from .env.")
    parser.add_argument("channel_id", type=int, nargs="?", help="Discord text channel ID to export.")
    parser.add_argument("--channels", type=int, nargs="+", default=[], help="Additional channel IDs to export in parallel.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum messages per channel. Default: 100.")
    parser.add_argument("--all", action="store_true", help="Export all available messages. Overrides --limit.")
    parser.add_argument("--format", default="jsonl", help="Kept for compatibility; output is always daily JSONL.")
    parser.add_argument("--output", type=Path, default=Path("exports"), help="Output root directory. Default: exports.")
    parser.add_argument("--before", default=None, help="Only messages before this ISO datetime, e.g. 2026-06-08T15:00:00+08:00.")
    parser.add_argument("--after", default=None, help="Only messages after this ISO datetime, e.g. 2026-06-08T09:00:00+08:00.")
    parser.add_argument("--oldest-first", action="store_true", help="Export oldest messages first.")
    parser.add_argument("--progress-every", type=int, default=500, help="Print progress every N messages. Default: 500.")
    return parser.parse_args()


def channel_ids_from_args(args: argparse.Namespace) -> list[int]:
    channel_ids = []
    if args.channel_id is not None:
        channel_ids.append(args.channel_id)
    channel_ids.extend(args.channels)
    deduped = list(dict.fromkeys(channel_ids))
    if not deduped:
        raise RuntimeError("Provide a channel_id positional argument or at least one --channels ID")
    return deduped


def parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def output_root_from_args(args: argparse.Namespace) -> Path:
    if args.output.suffix:
        print(f"warning=output_path_is_file using_parent={args.output.parent}", flush=True)
        return args.output.parent
    return args.output


def message_to_dict(message: discord.Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "channel_id": message.channel.id,
        "guild_id": message.guild.id if message.guild else None,
        "author_id": message.author.id,
        "author_name": str(message.author),
        "created_at": message.created_at.isoformat(),
        "edited_at": message.edited_at.isoformat() if message.edited_at else None,
        "content": message.content,
        "jump_url": message.jump_url,
        "attachments": [
            {
                "id": attachment.id,
                "filename": attachment.filename,
                "url": attachment.url,
                "content_type": attachment.content_type,
                "size": attachment.size,
            }
            for attachment in message.attachments
        ],
        "embeds": [embed.to_dict() for embed in message.embeds],
    }


def daily_path(channel_dir: Path, day: date) -> Path:
    return channel_dir / f"{day:%Y}" / f"{day:%m}" / f"{day:%Y-%m-%d}.jsonl"


class DailyJsonlWriter:
    def __init__(self, channel_dir: Path) -> None:
        self.channel_dir = channel_dir

    async def write(self, day: date, row: dict[str, Any]) -> None:
        path = daily_path(self.channel_dir, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "a", encoding="utf-8") as handle:
            await handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def close(self) -> None:
        return None


async def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(path, "w", encoding="utf-8") as handle:
        await handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def build_report(channel_id: int, stats: list[MessageStat], duration_seconds: float) -> dict[str, Any]:
    per_day = Counter(stat.day.isoformat() for stat in stats)
    per_author = Counter(stat.author_name for stat in stats)
    days = sorted(per_day)
    return {
        "channel_id": channel_id,
        "total_messages": len(stats),
        "messages_with_content": sum(1 for stat in stats if stat.has_content),
        "date_range": {
            "first": days[0] if days else None,
            "last": days[-1] if days else None,
        },
        "per_day": dict(sorted(per_day.items())),
        "per_author": dict(per_author.most_common()),
        "export_duration_seconds": round(duration_seconds, 3),
    }


def print_summary(result: ChannelExportResult, report_path: Path | None = None) -> None:
    if not result.ok:
        print(f"channel={result.channel_id} status=failed error={result.error}", flush=True)
        return
    print(
        f"channel={result.channel_id} status=ok total={result.total_messages} "
        f"with_content={result.messages_with_content} output={result.output_dir} report={report_path}",
        flush=True,
    )


async def export_channel(
    client: discord.Client,
    channel_id: int,
    args: argparse.Namespace,
    output_root: Path,
    before: datetime | None,
    after: datetime | None,
) -> ChannelExportResult:
    start_time = time.monotonic()
    channel_dir = output_root / f"channel_{channel_id}"
    stats: list[MessageStat] = []
    writer = DailyJsonlWriter(channel_dir)

    try:
        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        if not hasattr(channel, "history"):
            raise RuntimeError(f"Channel {channel_id} does not support message history")

        count = 0
        async for message in channel.history(
            limit=None if args.all else args.limit,
            before=before,
            after=after,
            oldest_first=args.oldest_first,
        ):
            row = message_to_dict(message)
            message_day = message.created_at.date()
            await writer.write(message_day, row)
            stats.append(MessageStat(day=message_day, author_name=row["author_name"], has_content=bool(row["content"])))
            count += 1
            if args.progress_every > 0 and count % args.progress_every == 0:
                print(
                    f"channel={channel_id} progress={count} last_created_at={row['created_at']}",
                    flush=True,
                )

        duration = time.monotonic() - start_time
        report = build_report(channel_id, stats, duration)
        report_path = channel_dir / "report.json"
        await write_json(report_path, report)
        result = ChannelExportResult(
            channel_id=channel_id,
            ok=True,
            output_dir=channel_dir,
            total_messages=report["total_messages"],
            messages_with_content=report["messages_with_content"],
        )
        print_summary(result, report_path)
        return result
    except discord.Forbidden as exc:
        return ChannelExportResult(
            channel_id=channel_id,
            ok=False,
            output_dir=channel_dir,
            error=f"forbidden: {exc}; grant View Channel and Read Message History permissions",
        )
    except Exception as exc:
        return ChannelExportResult(
            channel_id=channel_id,
            ok=False,
            output_dir=channel_dir,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        await writer.close()


async def run_export(args: argparse.Namespace) -> None:
    token = dotenv_values(".env").get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is missing from .env")
    if args.format != "jsonl":
        print(f"warning=format_ignored requested={args.format} actual=jsonl", flush=True)

    channel_ids = channel_ids_from_args(args)
    before = parse_dt(args.before)
    after = parse_dt(args.after)
    output_root = output_root_from_args(args)
    output_root.mkdir(parents=True, exist_ok=True)

    intents = discord.Intents.default()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            results = await asyncio.gather(
                *(export_channel(client, channel_id, args, output_root, before, after) for channel_id in channel_ids)
            )
            for result in results:
                if not result.ok:
                    print_summary(result)
        finally:
            await client.close()

    try:
        await client.start(token)
    except discord.PrivilegedIntentsRequired:
        print("error=privileged_intent_required")
        print("hint=enable Message Content Intent in Discord Developer Portal > Application > Bot")


def main() -> None:
    asyncio.run(run_export(parse_args()))


if __name__ == "__main__":
    main()
