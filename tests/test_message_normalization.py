from datetime import datetime, timezone
import unittest

from src.message_normalization import (
    MessageNormalizationInput,
    normalize_message,
)


BOT_ID = 999


def make_input(**overrides):
    values = {
        "message_id": 1,
        "created_at": datetime(2026, 6, 11, tzinfo=timezone.utc),
        "author_id": 123,
        "raw_content": "hello group",
        "is_bot_author": False,
        "is_dm": False,
        "is_thread": False,
        "is_wrong_guild": False,
        "is_wrong_channel": False,
        "is_bot_mentioned": False,
        "has_attachments": False,
        "has_stickers": False,
        "has_embeds": False,
        "bot_user_id": BOT_ID,
    }
    values.update(overrides)
    return MessageNormalizationInput(**values)


class MessageNormalizationTest(unittest.TestCase):
    def test_human_main_channel_text_is_eligible(self):
        result = normalize_message(make_input(raw_content=" hello   group\nagain "))

        self.assertEqual(result.normalized_content, "hello group again")
        self.assertTrue(result.is_rag_eligible)
        self.assertEqual(result.eligibility_reason, "eligible")

    def test_raw_content_is_not_mutated_by_normalization(self):
        message = make_input(raw_content=f"  <@{BOT_ID}>   hello\tgroup  ")

        result = normalize_message(message)

        self.assertEqual(message.raw_content, f"  <@{BOT_ID}>   hello\tgroup  ")
        self.assertEqual(result.normalized_content, "hello group")

    def test_bot_mention_is_removed_and_message_is_ineligible(self):
        cases = [f"<@{BOT_ID}> hello", f"<@!{BOT_ID}> hello"]

        for raw_content in cases:
            with self.subTest(raw_content=raw_content):
                result = normalize_message(
                    make_input(raw_content=raw_content, is_bot_mentioned=True)
                )

                self.assertEqual(result.normalized_content, "hello")
                self.assertFalse(result.is_rag_eligible)
                self.assertEqual(result.eligibility_reason, "bot_mention")

    def test_bot_mention_token_is_detected_when_flag_is_missing(self):
        result = normalize_message(
            make_input(raw_content=f"<@!{BOT_ID}> hello", is_bot_mentioned=False)
        )

        self.assertEqual(result.normalized_content, "hello")
        self.assertFalse(result.is_rag_eligible)
        self.assertEqual(result.eligibility_reason, "bot_mention")

    def test_pure_bot_mention_uses_bot_mention_reason(self):
        result = normalize_message(
            make_input(raw_content=f"<@{BOT_ID}>", is_bot_mentioned=True)
        )

        self.assertEqual(result.normalized_content, "")
        self.assertFalse(result.is_rag_eligible)
        self.assertEqual(result.eligibility_reason, "bot_mention")

    def test_bot_mention_flag_is_authoritative_when_token_is_missing(self):
        result = normalize_message(
            make_input(raw_content="hello group", is_bot_mentioned=True)
        )

        self.assertEqual(result.normalized_content, "hello group")
        self.assertFalse(result.is_rag_eligible)
        self.assertEqual(result.eligibility_reason, "bot_mention")

    def test_rejects_disallowed_event_sources(self):
        cases = [
            ("bot_author", {"is_bot_author": True}),
            ("dm", {"is_dm": True}),
            ("thread", {"is_thread": True}),
            ("wrong_guild", {"is_wrong_guild": True}),
            ("wrong_channel", {"is_wrong_channel": True}),
            ("not_human_text", {"is_system_message": True}),
            ("not_human_text", {"is_confirmed_human_text": False}),
        ]

        for reason, overrides in cases:
            with self.subTest(reason=reason):
                result = normalize_message(make_input(**overrides))
                self.assertFalse(result.is_rag_eligible)
                self.assertEqual(result.eligibility_reason, reason)

    def test_rejects_empty_normalized_text(self):
        result = normalize_message(make_input(raw_content="\u0000 \n\t"))

        self.assertEqual(result.normalized_content, "")
        self.assertFalse(result.is_rag_eligible)
        self.assertEqual(result.eligibility_reason, "empty_normalized_content")

    def test_rejects_pure_mentions(self):
        result = normalize_message(make_input(raw_content="<@123> <#456> <@&789>"))

        self.assertEqual(result.normalized_content, "<@123> <#456> <@&789>")
        self.assertFalse(result.is_rag_eligible)
        self.assertEqual(result.eligibility_reason, "pure_mention")

    def test_rejects_pure_emoji(self):
        cases = [
            "😀 😄",
            "❤️",
            "1️⃣",
            "10️⃣",
            "👨‍💻",
            "🇹🇼",
            "<:party:1234567890> <a:dance:1234567891>",
        ]

        for raw_content in cases:
            with self.subTest(raw_content=raw_content):
                result = normalize_message(make_input(raw_content=raw_content))
                self.assertFalse(result.is_rag_eligible)
                self.assertEqual(result.eligibility_reason, "pure_emoji")

    def test_text_with_emoji_is_eligible(self):
        result = normalize_message(make_input(raw_content="hello 😀"))

        self.assertEqual(result.normalized_content, "hello 😀")
        self.assertTrue(result.is_rag_eligible)

    def test_rejects_pure_sticker_attachment_and_embed_messages(self):
        cases = [
            ("pure_sticker", {"raw_content": "", "has_stickers": True}),
            ("pure_attachment", {"raw_content": "", "has_attachments": True}),
            ("pure_embed", {"raw_content": "", "has_embeds": True}),
        ]

        for reason, overrides in cases:
            with self.subTest(reason=reason):
                result = normalize_message(make_input(**overrides))
                self.assertFalse(result.is_rag_eligible)
                self.assertEqual(result.eligibility_reason, reason)


if __name__ == "__main__":
    unittest.main()
