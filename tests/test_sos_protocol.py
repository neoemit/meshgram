import unittest

from meshgram import sos_protocol as protocol


class SosProtocolTests(unittest.TestCase):
    """The same cases as MeshSOS's test_protocol.cpp: both sides must read a message alike."""

    def test_first_word_decides_the_kind(self):
        for text in ("ack", "ACK #a3f1 I am aware", "ok.", "Okay", "aware!"):
            self.assertEqual(protocol.classify(text), protocol.ACK, text)
        for text in ("resolved", "RESOLVED #a3f1 all OK", "safe!!", "Resolve"):
            self.assertEqual(protocol.classify(text), protocol.RESOLVED, text)
        for text in ("SOS! Need help.", "fire at the gate", "acknowledge", "!!!", "is ok"):
            self.assertEqual(protocol.classify(text), protocol.ALARM, text)
        self.assertIsNone(protocol.classify("   "))

    def test_incident_id_is_a_four_digit_hex_token(self):
        self.assertEqual(protocol.find_incident_id("SOS! Need help. bat 78% #A3F1"), "a3f1")
        self.assertEqual(protocol.find_incident_id("ACK #00ff I am aware"), "00ff")
        for text in ("no id", "abc#a3f1", "#a3f", "#a3f12", "#zzzz"):
            self.assertIsNone(protocol.find_incident_id(text), text)

    def test_relayed_name(self):
        self.assertEqual(protocol.split_relayed("<Anna> ack"), ("Anna", "ack"))
        self.assertEqual(protocol.split_relayed("<LMB-T HQ>"), ("LMB-T HQ", ""))
        for text in ("<Anna>ack", "hello <Anna> ack", "<> ack", "<An:na> ack", "<" + "x" * 32 + "> ack", "<  > ack"):
            self.assertEqual(protocol.split_relayed(text), (None, text), text)

    def test_parse_names_the_person_a_gateway_sent_for(self):
        message = protocol.parse("LMBot", "<Anna> ACK #a3f1 I am aware")
        self.assertEqual((message.sender, message.via, message.kind, message.incident_id), ("Anna", "LMBot", protocol.ACK, "a3f1"))
        message = protocol.parse("Thomas", "SOS! Need help. bat 78% #a3f1")
        self.assertEqual((message.sender, message.via, message.kind), ("Thomas", None, protocol.ALARM))
        self.assertIsNone(protocol.parse("Thomas", "  "))

    def test_display_text_drops_the_id(self):
        self.assertEqual(protocol.display_text("SOS! Need help. bat 78% #a3f1"), "SOS! Need help. bat 78%")

    def test_what_is_built_reads_back(self):
        sos = protocol.parse("Gw", protocol.relayed("Anna", protocol.build_sos("00ab")))
        self.assertEqual((sos.sender, sos.kind, sos.incident_id), ("Anna", protocol.ALARM, "00ab"))
        self.assertEqual(protocol.display_text(sos.body), protocol.SOS_TEXT)
        self.assertEqual(protocol.classify(protocol.build_ack("00ab")), protocol.ACK)
        self.assertEqual(protocol.classify(protocol.build_ack(None)), protocol.ACK)
        self.assertEqual(protocol.classify(protocol.build_resolved(None)), protocol.RESOLVED)
        self.assertEqual(protocol.find_incident_id(protocol.build_resolved("00ab")), "00ab")
        self.assertRegex(protocol.new_incident_id(), r"^[0-9a-f]{4}$")


if __name__ == "__main__":
    unittest.main()
