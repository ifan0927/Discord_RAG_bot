import asyncio
import logging

from src.bot import GroupMemoryBot
from src.config import load_settings


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = load_settings()
    bot = GroupMemoryBot(settings)
    async with bot:
        await bot.start(settings.bot_token)


if __name__ == "__main__":
    asyncio.run(main())
