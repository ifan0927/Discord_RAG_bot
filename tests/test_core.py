import unittest

from src.config import Settings


class CoreLogicTest(unittest.TestCase):
    def test_settings_shape_is_minimal(self):
        settings = Settings(bot_token="token", guild_id=1, channel_id=2)

        self.assertEqual(settings.guild_id, 1)
        self.assertEqual(settings.channel_id, 2)


if __name__ == "__main__":
    unittest.main()
