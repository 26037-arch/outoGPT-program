import unittest
from unittest.mock import patch

from cli_gpt.errors import LoginRequired, PageStructureChanged, ProjectAccessFailed
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
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_lazy_loaded_links_are_discovered_and_deduplicated(self, _login, _access):
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
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_delayed_project_dom_waits_for_readiness_before_discovery(
        self, _login, _access
    ):
        loading = {
            "recognized": False,
            "name": "",
            "explicitEmpty": False,
            "chats": [],
        }
        ready = {
            "recognized": True,
            "name": "Test Project",
            "explicitEmpty": False,
            "chats": [{"href": "/g/g-p-project/c/a", "title": "A"}],
        }
        page = FakeProjectPage([loading, loading, ready, ready])

        result = discover_project_chats(
            page, PROJECT_URL, stable_rounds=1, max_rounds=4, poll_ms=0
        )

        self.assertEqual(result.project_name, "Test Project")
        self.assertEqual([chat.chat_id for chat in result.chats], ["a"])

    @patch("cli_gpt.project.project_access_error_visible", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_three_zero_chat_loading_rounds_do_not_end_stabilization(
        self, _login, _access
    ):
        loading = {
            "recognized": True,
            "name": "Test Project",
            "explicitEmpty": False,
            "chats": [],
        }
        ready = {
            "recognized": True,
            "name": "Test Project",
            "explicitEmpty": False,
            "chats": [
                {"href": f"/g/g-p-project/c/{chat_id}", "title": chat_id.upper()}
                for chat_id in ("a", "b", "c")
            ],
        }
        page = FakeProjectPage([loading, loading, loading, ready, ready])

        result = discover_project_chats(
            page, PROJECT_URL, stable_rounds=1, max_rounds=5, poll_ms=0
        )

        self.assertEqual(len(result.chats), 3)
        self.assertGreaterEqual(page.sample_index, 5)

    @patch("cli_gpt.project.project_access_error_visible", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_unrecognized_empty_result_is_a_structure_error(self, _login, _access):
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

    @patch("cli_gpt.project.project_access_error_visible", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_explicit_empty_project_is_not_a_selector_error(self, _login, _access):
        page = FakeProjectPage(
            [
                {
                    "recognized": True,
                    "name": "Empty Project",
                    "explicitEmpty": True,
                    "chats": [],
                }
            ]
        )
        result = discover_project_chats(
            page, PROJECT_URL, stable_rounds=1, poll_ms=0
        )
        self.assertEqual(result.project_name, "Empty Project")
        self.assertEqual(result.chats, ())

    @patch("cli_gpt.project.project_access_error_visible", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_never_recognized_project_is_a_structure_error(self, _login, _access):
        page = FakeProjectPage(
            [
                {
                    "recognized": False,
                    "name": "",
                    "explicitEmpty": False,
                    "chats": [],
                }
            ]
        )

        with self.assertRaises(PageStructureChanged):
            discover_project_chats(page, PROJECT_URL, max_rounds=3, poll_ms=0)

    @patch("cli_gpt.project.project_access_error_visible", return_value=False)
    @patch(
        "cli_gpt.project.login_or_challenge_visible",
        side_effect=[False, False, True],
    )
    def test_login_during_readiness_polling_is_reported(self, _login, _access):
        page = FakeProjectPage(
            [
                {
                    "recognized": False,
                    "name": "",
                    "explicitEmpty": False,
                    "chats": [],
                }
            ]
        )

        with self.assertRaises(LoginRequired):
            discover_project_chats(page, PROJECT_URL, max_rounds=3, poll_ms=0)

    @patch("cli_gpt.project.project_access_error_visible", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_redirect_during_readiness_polling_is_reported(self, _login, _access):
        loading = {
            "recognized": False,
            "name": "",
            "explicitEmpty": False,
            "chats": [],
        }
        page = FakeProjectPage([loading])
        original_wait = page.wait_for_timeout

        def redirect_after_wait(milliseconds):
            original_wait(milliseconds)
            page.url = "https://chatgpt.com/g/g-p-other/project"

        page.wait_for_timeout = redirect_after_wait

        with self.assertRaises(ProjectAccessFailed):
            discover_project_chats(page, PROJECT_URL, max_rounds=3, poll_ms=0)

    @patch("cli_gpt.project.project_access_error_visible", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_ready_project_reuses_first_sample_without_readiness_wait(
        self, _login, _access
    ):
        ready = {
            "recognized": True,
            "name": "Ready Project",
            "explicitEmpty": False,
            "chats": [{"href": "/g/g-p-project/c/a", "title": "A"}],
        }
        page = FakeProjectPage([ready, ready])

        result = discover_project_chats(
            page, PROJECT_URL, stable_rounds=1, max_rounds=2, poll_ms=0
        )

        self.assertEqual(result.project_name, "Ready Project")
        self.assertEqual([chat.chat_id for chat in result.chats], ["a"])
        self.assertEqual(page.sample_index, 2)
        self.assertEqual(page.waits, [0])

    @patch("cli_gpt.project.project_access_error_visible", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_scan_limit_rejects_a_list_that_never_stabilizes(self, _login, _access):
        sample = {
            "recognized": True,
            "name": "Project",
            "explicitEmpty": False,
            "chats": [{"href": "/g/g-p-project/c/a", "title": "A"}],
        }
        page = FakeProjectPage([sample], [True, True])
        with self.assertRaises(PageStructureChanged):
            discover_project_chats(
                page, PROJECT_URL, max_rounds=2, stable_rounds=1, poll_ms=0
            )

    @patch("cli_gpt.project.project_access_error_visible", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=True)
    def test_login_page_is_reported_explicitly(self, _login, _access):
        page = FakeProjectPage([])
        with self.assertRaises(LoginRequired):
            discover_project_chats(page, PROJECT_URL, poll_ms=0)

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

    @patch(
        "cli_gpt.project._conversation_sample",
        return_value={
            "recognized": True,
            "explicitEmpty": True,
            "title": "Empty",
            "messages": [],
        },
    )
    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_explicit_empty_conversation_returns_zero_pairs(
        self, _login, _generating, _sample
    ):
        page = FakeProjectPage([])
        chat = ProjectChat(
            "empty",
            "https://chatgpt.com/g/g-p-project/c/empty",
            "Empty",
        )
        snapshot = read_conversation(page, chat, poll_ms=0)
        self.assertFalse(snapshot.generating)
        self.assertEqual(snapshot.qa_pairs, ())


if __name__ == "__main__":
    unittest.main()
