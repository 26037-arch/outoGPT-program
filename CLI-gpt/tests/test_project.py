import unittest
from unittest.mock import patch

from cli_gpt.errors import PageStructureChanged
from cli_gpt.project import (
    ProjectChat,
    discover_project_chats,
    pair_messages,
    read_conversation,
)


PROJECT_URL = "https://chatgpt.com/g/g-p-project/project"


class FakeProjectPage:
    def __init__(self, samples, scroll_results=()):
        self.url = PROJECT_URL
        self.samples = list(samples)
        self.scroll_results = list(scroll_results)
        self.sample_index = 0
        self.scroll_index = 0
        self.waits = []

    def goto(self, url, **kwargs):
        self.url = url

    def evaluate(self, script, argument):
        if "OUTOGPT_PROJECT_DISCOVERY" in script:
            index = min(self.sample_index, len(self.samples) - 1)
            self.sample_index += 1
            return self.samples[index]
        index = min(self.scroll_index, len(self.scroll_results) - 1)
        self.scroll_index += 1
        return self.scroll_results[index] if self.scroll_results else False

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class ProjectDomTests(unittest.TestCase):
    @patch("cli_gpt.project.project_access_error_visible", return_value=False)
    def test_lazy_loaded_links_are_discovered_and_deduplicated(self, _):
        first = {
            "recognized": True,
            "name": "Project",
            "explicitEmpty": False,
            "chats": [
                {"href": "/g/g-p-project/c/a", "title": "A"},
                {"href": "/g/g-p-project/c/a", "title": "A duplicate"},
            ],
        }
        second = {
            **first,
            "chats": [
                {"href": "/g/g-p-project/c/a", "title": "A"},
                {"href": "/g/g-p-project/c/b", "title": "B"},
            ],
        }
        page = FakeProjectPage(
            [first, second, second, second, second],
            [True, True, False, False, False],
        )
        result = discover_project_chats(page, PROJECT_URL, poll_ms=0)
        self.assertEqual([chat.chat_id for chat in result.chats], ["a", "b"])

    @patch("cli_gpt.project.project_access_error_visible", return_value=False)
    def test_unrecognized_empty_result_is_a_structure_error(self, _):
        page = FakeProjectPage(
            [
                {
                    "recognized": True,
                    "name": "Project",
                    "explicitEmpty": False,
                    "chats": [],
                }
            ]
        )
        with self.assertRaises(PageStructureChanged):
            discover_project_chats(page, PROJECT_URL, stable_rounds=1, poll_ms=0)

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

    def test_unsafe_message_sequence_is_rejected(self):
        with self.assertRaises(PageStructureChanged):
            pair_messages([{"role": "assistant", "markdown": "orphan"}])
        with self.assertRaises(PageStructureChanged):
            pair_messages(
                [
                    {"role": "user", "markdown": "one"},
                    {"role": "user", "markdown": "two"},
                ]
            )

    @patch("cli_gpt.project._conversation_sample")
    @patch("cli_gpt.project.generation_in_progress", return_value=True)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_generation_is_checked_before_message_extraction(
        self, _login, _generating, sample
    ):
        page = FakeProjectPage([])
        chat = ProjectChat(
            "active",
            "https://chatgpt.com/g/g-p-project/c/active",
            "Active",
        )
        snapshot = read_conversation(page, chat)
        self.assertTrue(snapshot.generating)
        sample.assert_not_called()


if __name__ == "__main__":
    unittest.main()
