import asyncio

import discord
from discord.ext import commands

from src.config import Settings
from src.member_identity import (
    MemberIdentity,
    fallback_display_name,
    parse_mentioned_user_ids,
)
from src.openai_runtime import OpenAIEmbeddingClient, OpenAIResponsesClient
from src.runtime import MentionRuntime, RuntimeMessage
from src.runtime_store import PostgresRuntimeStore


class GroupMemoryBot(commands.Bot):
    def __init__(self, settings: Settings, runtime: MentionRuntime | None = None) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.runtime = runtime

    async def on_ready(self) -> None:
        assert self.user is not None
        print(f"Logged in as {self.user} ({self.user.id})")

    async def on_message(self, message: discord.Message) -> None:
        if self.user is None or message.author.id == self.user.id:
            return
        if message.guild is None or message.guild.id != self.settings.guild_id:
            return
        if message.channel.id != self.settings.channel_id:
            return
        if message.author.bot:
            return

        await self.process_commands(message)

        runtime = self.runtime or self._build_runtime()
        self.runtime = runtime

        is_bot_mentioned = self.user in message.mentions
        mentioned_members = (
            await self._mentioned_member_identities(message) if is_bot_mentioned else ()
        )
        await runtime.handle_message(
            RuntimeMessage(
                message_id=int(message.id),
                created_at=message.created_at,
                author_id=int(message.author.id),
                raw_content=message.content,
                guild_id=int(message.guild.id) if message.guild else None,
                channel_id=int(message.channel.id),
                is_dm=message.guild is None,
                is_thread=isinstance(message.channel, discord.Thread),
                is_bot_author=message.author.bot,
                is_bot_mentioned=is_bot_mentioned,
                bot_user_id=int(self.user.id),
                has_attachments=bool(message.attachments),
                has_stickers=bool(message.stickers),
                has_embeds=bool(message.embeds),
                is_system_message=message.is_system(),
                caller_display_name=_display_name(message.author),
                mentioned_members=mentioned_members,
                unknown_member_ids=tuple(
                    member.user_id
                    for member in mentioned_members
                    if member.source == "fallback"
                ),
            ),
            message.channel.send,
        )

    async def _mentioned_member_identities(
        self, message: discord.Message
    ) -> tuple[MemberIdentity, ...]:
        assert self.user is not None
        mentioned_ids = parse_mentioned_user_ids(
            message.content, bot_user_id=int(self.user.id)
        )
        payload_members = {
            int(user.id): _member_identity(user, "payload")
            for user in message.mentions
            if int(user.id) in mentioned_ids
        }
        resolved = []
        for user_id in mentioned_ids:
            if user_id in payload_members:
                resolved.append(payload_members[user_id])
                continue
            resolved.append(await self._lookup_member_identity(message, user_id))
        return tuple(resolved)

    async def _lookup_member_identity(
        self, message: discord.Message, user_id: int
    ) -> MemberIdentity:
        if message.guild is None:
            return MemberIdentity(user_id, fallback_display_name(user_id), "fallback")
        cached = message.guild.get_member(user_id)
        if cached is not None:
            return _member_identity(cached, "cache")
        try:
            member = await asyncio.wait_for(
                message.guild.fetch_member(user_id),
                timeout=self.settings.member_lookup_timeout_seconds,
            )
        except Exception:
            return MemberIdentity(user_id, fallback_display_name(user_id), "fallback")
        return _member_identity(member, "lookup")

    def _build_runtime(self) -> MentionRuntime:
        store = PostgresRuntimeStore(self.settings.database_url)
        openai = (
            OpenAIResponsesClient(self.settings.openai_api_key)
            if self.settings.openai_api_key
            else None
        )
        embedding = (
            OpenAIEmbeddingClient(self.settings.openai_api_key)
            if self.settings.openai_api_key
            else None
        )
        return MentionRuntime(
            self.settings,
            store,
            embedding_client=embedding,
            answer_client=openai,
            fallback_client=openai,
            router_client=openai,
        )


def _member_identity(user: discord.abc.User, source: str) -> MemberIdentity:
    return MemberIdentity(
        int(user.id),
        _display_name(user) or fallback_display_name(int(user.id)),
        source,
    )


def _display_name(user: discord.abc.User) -> str:
    value = (
        getattr(user, "display_name", None)
        or getattr(user, "global_name", None)
        or getattr(user, "name", None)
        or ""
    )
    return str(value).strip()
