import unittest

from meshgram.text_utils import normalized_exact_word, split_for_meshtastic, utf8_len


class TextUtilsTests(unittest.TestCase):
    def test_no_chunk_when_message_fits(self):
        chunks = split_for_meshtastic(
            text="hello mesh",
            payload_limit=50,
            prefix_template="({index}/{total}) ",
            chunking_enabled=True,
        )
        self.assertEqual(chunks, ["hello mesh"])

    def test_chunking_is_byte_aware(self):
        message = "hello 😀😀😀😀 world"
        chunks = split_for_meshtastic(
            text=message,
            payload_limit=18,
            prefix_template="({index}/{total}) ",
            chunking_enabled=True,
        )

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(utf8_len(chunk), 18)

    def test_chunk_prefix_added(self):
        chunks = split_for_meshtastic(
            text="alpha beta gamma delta",
            payload_limit=15,
            prefix_template="({index}/{total}) ",
            chunking_enabled=True,
        )

        self.assertTrue(all(chunk.startswith("(") for chunk in chunks))
        self.assertIn("1/", chunks[0])

    def test_hard_split_for_long_single_token(self):
        chunks = split_for_meshtastic(
            text="supercalifragilisticexpialidocious",
            payload_limit=12,
            prefix_template="({index}/{total}) ",
            chunking_enabled=True,
        )

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(utf8_len(chunk), 12)

    def test_prefix_indices_are_consistent_after_convergence(self):
        message = " ".join(["😀emoji"] * 120)
        chunks = split_for_meshtastic(
            text=message,
            payload_limit=32,
            prefix_template="({index}/{total}) ",
            chunking_enabled=True,
        )

        total = len(chunks)
        self.assertGreater(total, 9)
        for index, chunk in enumerate(chunks, start=1):
            self.assertTrue(chunk.startswith(f"({index}/{total}) "))
            self.assertLessEqual(utf8_len(chunk), 32)

    def test_normalized_exact_word_for_ping(self):
        self.assertEqual(normalized_exact_word("  !!!Ping???  "), "ping")
        self.assertNotEqual(normalized_exact_word("ping me"), "ping")


if __name__ == "__main__":
    unittest.main()
