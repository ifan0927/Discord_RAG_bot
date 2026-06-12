import discord
from discord.ext import commands

from src.config import Settings
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
                is_bot_mentioned=self.user in message.mentions,
                bot_user_id=int(self.user.id),
                has_attachments=bool(message.attachments),
                has_stickers=bool(message.stickers),
                has_embeds=bool(message.embeds),
                is_system_message=message.is_system(),
            ),
            message.channel.send,
        )

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
