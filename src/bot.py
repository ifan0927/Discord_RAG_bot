import discord
from discord.ext import commands

from src.config import Settings


class GroupMemoryBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings

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

        if self.user not in message.mentions:
            return

        await message.channel.send("我收到 mention 了；實際回覆流程尚未接上。")
