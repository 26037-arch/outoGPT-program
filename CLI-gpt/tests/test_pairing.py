import unittest
from cli_gpt.errors import PageStructureChanged
from cli_gpt.project import pair_messages, _normalize_conversation_messages


def turn(turn_id, role, markdown, *, attachments=()):
    return {
        "turnId": turn_id,
        "role": role,
        "markdown": markdown,
        "attachments": attachments,
    }


class PairingTests(unittest.TestCase):
    def test_explicit_pairing_ignores_only_a_trailing_user(self):
        pairs = pair_messages(
            [
                {"role": "user", "markdown": "Q1"},
                {"role": "assistant", "markdown": "A1"},
                {"role": "user", "markdown": "incomplete"},
            ]
        )
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].assistant, "A1")

    def test_consecutive_assistant_segments_are_preserved_as_one_answer(self):
        pairs = pair_messages(
            [
                {"role": "user", "markdown": "Question"},
                {"role": "assistant", "markdown": "First segment"},
                {"role": "assistant", "markdown": "Second segment"},
            ]
        )
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].assistant, "First segment\n\nSecond segment")

    def test_normalization_drops_auxiliary_and_duplicate_render_nodes(self):
        normalized = _normalize_conversation_messages(
            [
                {"auxiliary": True, "role": "", "markdown": ""},
                turn(
                    "u1",
                    "user",
                    "",
                    attachments=({"kind": "file", "name": "자료.pdf"},),
                ),
                turn(
                    "u1",
                    "user",
                    "",
                    attachments=({"kind": "file", "name": "자료.pdf"},),
                ),
                turn("a1", "assistant", "Answer"),
            ]
        )
        self.assertEqual(len(normalized), 2)
        self.assertEqual(normalized[0]["markdown"], "[Attachment: 자료.pdf]")

    def test_unsafe_message_sequence_is_rejected(self):
        with self.assertRaises(PageStructureChanged):
            pair_messages([{"role": "assistant", "markdown": "orphan"}])
        with self.assertRaises(PageStructureChanged):
            pair_messages(
                [
                    {"role": "user", "markdown": "one"},
                    {"role": "user", "markdown": "two"},
                    {"role": "assistant", "markdown": "answer"},
                ]
            )
