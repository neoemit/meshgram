from datetime import datetime, timezone
import unittest

from telegram import Chat, MessageReactionUpdated, ReactionTypeCustomEmoji, ReactionTypeEmoji

from meshgram.app import _extract_first_unicode_reaction_emoji


class TelegramReactionParsingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
