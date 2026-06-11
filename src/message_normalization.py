from dataclasses import dataclass
from datetime import datetime
import re
import unicodedata


_CUSTOM_EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_]+:\d+>")
_DISCORD_MENTION_RE = re.compile(r"<(?:@!?\d+|@&\d+|#\d+)>")
_KEYCAP_EMOJI_RE = re.compile(r"[0-9#*]+\ufe0f?\u20e3")


@dataclass(frozen=True)
class MessageNormalizationInput:
    message_id: int | str
    created_at: datetime
    author_id: int | str
    raw_content: str
    is_bot_author: bool
    is_dm: bool
    is_thread: bool
    is_wrong_guild: bool
    is_wrong_channel: bool
    is_bot_mentioned: bool
    has_attachments: bool
    has_stickers: bool
    has_embeds: bool
    bot_user_id: int | str
    is_system_message: bool = False
    is_confirmed_human_text: bool = True


@dataclass(frozen=True)
class MessageNormalizationResult:
    normalized_content: str
    is_rag_eligible: bool
    eligibility_reason: str


def normalize_message(message: MessageNormalizationInput) -> MessageNormalizationResult:
    normalized_content = _normalize_content(message.raw_content, message.bot_user_id)
    ineligible_reason = _ineligible_reason(message, normalized_content)

    return MessageNormalizationResult(
        normalized_content=normalized_content,
        is_rag_eligible=ineligible_reason is None,
        eligibility_reason=ineligible_reason or "eligible",
    )


def _normalize_content(raw_content: str, bot_user_id: int | str | None) -> str:
    content = raw_content
    if bot_user_id is not None:
        content = _bot_mention_pattern(bot_user_id).sub(" ", content)

    return _collapse_whitespace(_clean_control_characters(content))


def _clean_control_characters(content: str) -> str:
    cleaned = []
    for char in content:
        if char.isspace():
            cleaned.append(" ")
            continue
        if unicodedata.category(char)[0] == "C":
            continue
        cleaned.append(char)
    return "".join(cleaned)


def _collapse_whitespace(content: str) -> str:
    return re.sub(r"\s+", " ", content).strip()


def _ineligible_reason(
    message: MessageNormalizationInput, normalized_content: str
) -> str | None:
    if message.is_bot_author:
        return "bot_author"
    if message.is_dm:
        return "dm"
    if message.is_thread:
        return "thread"
    if message.is_wrong_guild:
        return "wrong_guild"
    if message.is_wrong_channel:
        return "wrong_channel"
    if message.is_system_message or not message.is_confirmed_human_text:
        return "not_human_text"
    if message.is_bot_mentioned or _contains_bot_mention(
        message.raw_content, message.bot_user_id
    ):
        return "bot_mention"
    if not normalized_content:
        if message.has_stickers:
            return "pure_sticker"
        if message.has_attachments:
            return "pure_attachment"
        if message.has_embeds:
            return "pure_embed"
        return "empty_normalized_content"
    if _is_pure_mention(normalized_content):
        return "pure_mention"
    if _is_pure_emoji(normalized_content):
        return "pure_emoji"
    return None


def _is_pure_mention(content: str) -> bool:
    return _collapse_whitespace(_DISCORD_MENTION_RE.sub(" ", content)) == ""


def _is_pure_emoji(content: str) -> bool:
    content_without_custom_emoji = _CUSTOM_EMOJI_RE.sub(" ", content)
    has_emoji = content_without_custom_emoji != content
    content_without_keycap_emoji = _KEYCAP_EMOJI_RE.sub(
        " ", content_without_custom_emoji
    )
    has_emoji = has_emoji or content_without_keycap_emoji != content_without_custom_emoji
    remaining = []

    for char in content_without_keycap_emoji:
        if char.isspace():
            continue
        if _is_emoji_modifier(char):
            continue
        if _is_unicode_emoji(char):
            has_emoji = True
            continue
        remaining.append(char)

    return has_emoji and not remaining


def _is_unicode_emoji(char: str) -> bool:
    codepoint = ord(char)
    if 0x1F000 <= codepoint <= 0x1FAFF:
        return True
    if 0x2600 <= codepoint <= 0x27BF:
        return True
    return False


def _is_emoji_modifier(char: str) -> bool:
    codepoint = ord(char)
    if codepoint == 0xFE0F:
        return True
    if codepoint == 0x200D:
        return True
    if 0x1F3FB <= codepoint <= 0x1F3FF:
        return True
    return False


def _contains_bot_mention(raw_content: str, bot_user_id: int | str) -> bool:
    return _bot_mention_pattern(bot_user_id).search(raw_content) is not None


def _bot_mention_pattern(bot_user_id: int | str) -> re.Pattern[str]:
    bot_id = re.escape(str(bot_user_id))
    return re.compile(rf"<@!?{bot_id}>")
