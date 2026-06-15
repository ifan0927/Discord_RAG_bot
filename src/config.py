from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    guild_id: int
    channel_id: int
    database_url: str = ""
    openai_api_key: str = ""
    chunk_strategy_version: str = ""
    summary_strategy_version: str = ""
    answer_model: str = "gpt-5.4-mini"
    fallback_model: str = "gpt-5.4-mini"
    router_model: str = "gpt-5.4-mini"
    embedding_model: str = "text-embedding-3-small"
    answer_system_prompt_path: Path = Path("prompts/answer_system.md")
    router_prompt_path: Path = Path("prompts/router_should_retrieve.md")
    raw_upsert_timeout_seconds: float = 2
    session_db_timeout_seconds: float = 3
    session_timeout_minutes: int = 15
    session_prompt_turns: int = 8
    session_stored_turns: int = 20
    router_timeout_seconds: float = 3
    router_session_turns: int = 4
    router_max_output_tokens: int = 128
    query_embedding_timeout_seconds: float = 8
    summary_top_k: int = 3
    aligned_chunks_per_summary: int = 2
    aligned_chunks_max: int = 6
    global_chunk_fallback_top_k: int = 2
    retrieval_db_query_timeout_seconds: float = 3
    prompt_input_token_budget: int = 12000
    rag_context_budget_ratio: float = 0.4
    answer_max_output_tokens: int = 1024
    answer_timeout_seconds: float = 45
    fallback_timeout_seconds: float = 15
    discord_send_retry_count: int = 1
    session_update_retry_count: int = 1


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _optional(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def _required_int(name: str) -> int:
    return int(_required(name))


def _positive_int(name: str, default: int | None = None) -> int:
    value = int(_required(name) if default is None else _optional(name, str(default)))
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than 0")
    return value


def _non_negative_int(name: str, default: int) -> int:
    value = int(_optional(name, str(default)))
    if value < 0:
        raise RuntimeError(f"{name} must not be negative")
    return value


def _positive_float(name: str, default: float) -> float:
    value = float(_optional(name, str(default)))
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than 0")
    return value


def _ratio(name: str, default: float) -> float:
    value = float(_optional(name, str(default)))
    if value <= 0 or value > 1:
        raise RuntimeError(f"{name} must be between 0 and 1")
    return value


def _readable_path(name: str, default: str) -> Path:
    path = Path(_optional(name, default))
    if not path.is_file():
        raise RuntimeError(f"{name} must point to a readable file: {path}")
    return path


def load_settings() -> Settings:
    return Settings(
        bot_token=_required("BOT_TOKEN"),
        guild_id=_required_int("GUILD_ID"),
        channel_id=_required_int("CHANNEL_ID"),
        database_url=_required("DATABASE_URL"),
        openai_api_key=_required("OPENAI_API_KEY"),
        chunk_strategy_version=_required("CHUNK_STRATEGY_VERSION"),
        summary_strategy_version=_required("SUMMARY_STRATEGY_VERSION"),
        answer_model=_optional("ANSWER_MODEL", "gpt-5.4-mini"),
        fallback_model=_optional("FALLBACK_MODEL", "gpt-5.4-mini"),
        router_model=_optional("ROUTER_MODEL", "gpt-5.4-mini"),
        embedding_model=_optional("EMBEDDING_MODEL", "text-embedding-3-small"),
        answer_system_prompt_path=_readable_path(
            "ANSWER_SYSTEM_PROMPT_PATH", "prompts/answer_system.md"
        ),
        router_prompt_path=_readable_path(
            "ROUTER_PROMPT_PATH", "prompts/router_should_retrieve.md"
        ),
        raw_upsert_timeout_seconds=_positive_float("RAW_UPSERT_TIMEOUT_SECONDS", 2),
        session_db_timeout_seconds=_positive_float("SESSION_DB_TIMEOUT_SECONDS", 3),
        session_timeout_minutes=_positive_int("SESSION_TIMEOUT_MINUTES", 15),
        session_prompt_turns=_positive_int("SESSION_PROMPT_TURNS", 8),
        session_stored_turns=_positive_int("SESSION_STORED_TURNS", 20),
        router_timeout_seconds=_positive_float("ROUTER_TIMEOUT_SECONDS", 3),
        router_session_turns=_positive_int("ROUTER_SESSION_TURNS", 4),
        router_max_output_tokens=_positive_int("ROUTER_MAX_OUTPUT_TOKENS", 128),
        query_embedding_timeout_seconds=_positive_float(
            "QUERY_EMBEDDING_TIMEOUT_SECONDS", 8
        ),
        summary_top_k=_non_negative_int("SUMMARY_TOP_K", 3),
        aligned_chunks_per_summary=_non_negative_int("ALIGNED_CHUNKS_PER_SUMMARY", 2),
        aligned_chunks_max=_non_negative_int("ALIGNED_CHUNKS_MAX", 6),
        global_chunk_fallback_top_k=_non_negative_int(
            "GLOBAL_CHUNK_FALLBACK_TOP_K", 2
        ),
        retrieval_db_query_timeout_seconds=_positive_float(
            "RETRIEVAL_DB_QUERY_TIMEOUT_SECONDS", 3
        ),
        prompt_input_token_budget=_positive_int("PROMPT_INPUT_TOKEN_BUDGET", 12000),
        rag_context_budget_ratio=_ratio("RAG_CONTEXT_BUDGET_RATIO", 0.4),
        answer_max_output_tokens=_positive_int("ANSWER_MAX_OUTPUT_TOKENS", 1024),
        answer_timeout_seconds=_positive_float("ANSWER_TIMEOUT_SECONDS", 45),
        fallback_timeout_seconds=_positive_float("FALLBACK_TIMEOUT_SECONDS", 15),
        discord_send_retry_count=_non_negative_int("DISCORD_SEND_RETRY_COUNT", 1),
        session_update_retry_count=_non_negative_int("SESSION_UPDATE_RETRY_COUNT", 1),
    )
