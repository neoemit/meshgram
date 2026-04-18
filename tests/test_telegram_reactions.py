from datetime import datetime, timezone
import unittest

from telegram import (
    Chat,
    MessageReactionCountUpdated,
    MessageReactionUpdated,
    ReactionCount,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
)

from meshgram.app import MeshgramApp, _extract_first_unicode_reaction_emoji
from meshgram.config import MeshgramSettings


class TelegramReactionParsingTests(unittest.TestCase):
    def _app(self) -> MeshgramApp:
        settings = MeshgramSettings(
            telegram_bot_token="token",
            telegram_group_id=-1000,
            config_path="config.yaml",
            plugins=[],
        )
        return MeshgramApp(settings)

    def _build_update(self, new_reaction):
        return MessageReactionUpdated(
            chat=Chat(id=-1000, type="group"),
            message_id=123,
            date=datetime.now(timezone.utc),
            old_reaction=[],
            new_reaction=new_reaction,
        )

    def test_extracts_first_unicode_emoji(self):
        reaction_update = self._build_update(
            [
                ReactionTypeEmoji("❤"),
                ReactionTypeEmoji("🔥"),
            ]
        )

        self.assertEqual(_extract_first_unicode_reaction_emoji(reaction_update), "❤")

    def test_returns_none_for_removal(self):
        reaction_update = self._build_update([])
        self.assertIsNone(_extract_first_unicode_reaction_emoji(reaction_update))

    def test_returns_none_for_custom_emoji_only(self):
        reaction_update = self._build_update(
            [ReactionTypeCustomEmoji(custom_emoji_id="1234567890")]
        )
        self.assertIsNone(_extract_first_unicode_reaction_emoji(reaction_update))

    def _build_count_update(self, reactions):
        return MessageReactionCountUpdated(
            chat=Chat(id=-1000, type="group"),
            message_id=123,
            date=datetime.now(timezone.utc),
            reactions=reactions,
        )

    def test_count_update_uses_positive_delta_and_supports_first_event(self):
        app = self._app()
        baseline = self._build_count_update([ReactionCount(type=ReactionTypeEmoji("❤"), total_count=2)])
        first_event = app._build_telegram_reaction_event_from_count_update(baseline)
        self.assertIsNotNone(first_event)
        assert first_event is not None
        self.assertEqual(first_event.emoji, "❤")

        incremented = self._build_count_update([ReactionCount(type=ReactionTypeEmoji("❤"), total_count=3)])
        event = app._build_telegram_reaction_event_from_count_update(incremented)

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.emoji, "❤")
        self.assertEqual(event.message_id, 123)

    def test_count_update_ignores_removals(self):
        app = self._app()
        baseline = self._build_count_update([ReactionCount(type=ReactionTypeEmoji("❤"), total_count=3)])
        app._build_telegram_reaction_event_from_count_update(baseline)

        decreased = self._build_count_update([ReactionCount(type=ReactionTypeEmoji("❤"), total_count=2)])
        event = app._build_telegram_reaction_event_from_count_update(decreased)
        self.assertIsNone(event)

    def test_count_update_ignores_recent_bot_reaction_write(self):
        app = self._app()
        baseline = self._build_count_update([ReactionCount(type=ReactionTypeEmoji("❤"), total_count=1)])
        app._build_telegram_reaction_event_from_count_update(baseline)
        app._record_telegram_reaction_write(-1000, 123, "❤")

        incremented = self._build_count_update([ReactionCount(type=ReactionTypeEmoji("❤"), total_count=2)])
        event = app._build_telegram_reaction_event_from_count_update(incremented)
        self.assertIsNone(event)


if __name__ == "__main__":
    unittest.main()
