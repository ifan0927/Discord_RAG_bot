from dataclasses import dataclass
import os

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    guild_id: int
    channel_id: int


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_settings() -> Settings:
    return Settings(
        bot_token=_required("BOT_TOKEN"),
        guild_id=int(_required("GUILD_ID")),
        channel_id=int(_required("CHANNEL_ID")),
    )
