from types import SimpleNamespace
import unittest

from src.bot import GroupMemoryBot
from src.config import Settings


def settings() -> Settings:
    return Settings(
        bot_token="token",
        guild_id=1,
        channel_id=2,
        database_url="postgresql://local/test",
        chunk_strategy_version="timegap-v1",
        summary_strategy_version="hourly-v1",
        member_lookup_timeout_seconds=0.01,
    )


class BotIdentityTest(unittest.IsolatedAsyncioTestCase):
    async def test_mentioned_member_identity_uses_mentions_payload(self):
        async def fail_lookup(_message, _user_id):
            raise AssertionError("lookup should not be called for payload mentions")

        bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            settings=settings(),
            _lookup_member_identity=fail_lookup,
        )
        message = SimpleNamespace(
            content="<@999> <@123> hi",
            mentions=[
                SimpleNamespace(id=999, display_name="Bot"),
                SimpleNamespace(id=123, display_name="Bob"),
            ],
        )

        identities = await GroupMemoryBot._mentioned_member_identities(bot, message)

        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0].user_id, 123)
        self.assertEqual(identities[0].display_name, "Bob")
        self.assertEqual(identities[0].source, "payload")

    async def test_lookup_member_identity_falls_back_when_fetch_fails(self):
        class FakeGuild:
            def get_member(self, _user_id):
                return None

            async def fetch_member(self, _user_id):
                raise RuntimeError("missing access")

        bot = SimpleNamespace(settings=settings())
        message = SimpleNamespace(guild=FakeGuild())

        identity = await GroupMemoryBot._lookup_member_identity(bot, message, 123)

        self.assertEqual(identity.user_id, 123)
        self.assertEqual(identity.display_name, "member:123")
        self.assertEqual(identity.source, "fallback")


if __name__ == "__main__":
    unittest.main()
