from datetime import datetime, timezone
import unittest

from src.member_identity import (
    MemberIdentity,
    build_member_identity_context,
    build_retrieval_intent,
    parse_mentioned_user_ids,
)


class MemberIdentityTest(unittest.TestCase):
    def test_parse_mentioned_user_ids_excludes_bot_and_dedupes(self):
        ids = parse_mentioned_user_ids("<@999> <@123> <@!123> <@456>", bot_user_id=999)

        self.assertEqual(ids, (123, 456))

    def test_build_retrieval_intent_uses_mentions_and_taipei_day_window(self):
        identity = build_member_identity_context(
            caller_id=111,
            caller_display_name="Alice",
            mentioned_members=(MemberIdentity(222, "Bob", "payload"),),
        )

        intent = build_retrieval_intent(
            "昨天 Bob 有說 postgres 嗎",
            identity,
            datetime(2026, 6, 15, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(intent.target_author_ids, (222,))
        self.assertIsNotNone(intent.target_time_window)
        self.assertEqual(
            intent.target_time_window.start_at.isoformat(),
            "2026-06-13T16:00:00+00:00",
        )
        self.assertEqual(
            intent.target_time_window.end_at.isoformat(),
            "2026-06-14T16:00:00+00:00",
        )

    def test_unresolved_member_uses_fallback_display_name(self):
        identity = build_member_identity_context(
            caller_id=111,
            caller_display_name=None,
            mentioned_members=(MemberIdentity(222, "member:222", "fallback"),),
            unknown_member_ids=(222,),
        )

        self.assertEqual(identity.caller.display_name, "member:111")
        self.assertEqual(identity.mentioned_members[0].display_name, "member:222")
        self.assertEqual(identity.unknown_member_ids, (222,))

    def test_invalid_date_does_not_create_time_window(self):
        identity = build_member_identity_context(
            caller_id=111,
            caller_display_name="Alice",
        )

        intent = build_retrieval_intent(
            "2026-02-31 有聊過什麼",
            identity,
            datetime(2026, 6, 15, 2, tzinfo=timezone.utc),
        )

        self.assertIsNone(intent.target_time_window)


if __name__ == "__main__":
    unittest.main()
