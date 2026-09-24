import unittest
from unittest.mock import patch

from cli_gpt.errors import LoginRequired, PageStructureChanged, ProjectAccessFailed
from cli_gpt.project import (
    ProjectChat,
    _normalize_conversation_messages,
    discover_project_chats,
    pair_messages,
    read_conversation,
)


PROJECT_URL = "https://chatgpt.com/g/g-p-project/project"


def turn(turn_id, role, markdown, *, attachments=()):
    return {
        "turnId": turn_id,
        "role": role,
        "markdown": markdown,
        "attachments": list(attachments),
    }


def qa_turns(first, last):
    messages = []
    for index in range(first, last + 1):
        messages.extend(
            [
                turn(f"u{index}", "user", f"Q{index}"),
                turn(f"a{index}", "assistant", f"A{index}"),
            ]
        )
    return messages


def conversation_sample(messages, *, title="Conversation"):
    return {
        "recognized": True,
        "explicitEmpty": False,
        "title": title,
        "turnCount": len(messages),
        "invalidTurns": 0,
        "messages": messages,
    }


def scroll_state(*, before=0, height=100, kind="ancestor"):
    return {
        "found": True,
        "containerKind": kind,
        "before": before,
        "after": 0,
        "wasAtTop": before <= 1,
        "atTop": True,
        "scrollHeight": height,
        "clientHeight": 100,
    }


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


class FakeConversationPage:
    def __init__(self, samples, scroll_results=(), chat_id="chat"):
        self.url = f"https://chatgpt.com/g/g-p-project/c/{chat_id}"
        self.samples = list(samples)
        self.scroll_results = list(scroll_results)
        self.sample_index = 0
        self.scroll_index = 0
        self.waits = []
        self.evaluate_arguments = []
        self.scroll_scripts = []

    def goto(self, url, **kwargs):
        self.url = url

    def evaluate(self, script, argument):
        if "OUTOGPT_CONVERSATION_EXTRACTION" in script:
            self.evaluate_arguments.append(argument)
            index = min(self.sample_index, len(self.samples) - 1)
            self.sample_index += 1
            return self.samples[index]
        if "OUTOGPT_CONVERSATION_SCROLL_TOP" in script:
            self.scroll_scripts.append(script)
            if self.scroll_results:
                index = min(self.scroll_index, len(self.scroll_results) - 1)
                self.scroll_index += 1
                return self.scroll_results[index]
            self.scroll_index += 1
            return {
                "found": True,
                "containerKind": "ancestor",
                "before": 0,
                "after": 0,
                "wasAtTop": True,
                "atTop": True,
                "scrollHeight": 100,
                "clientHeight": 100,
            }
        raise AssertionError("Unexpected page evaluation")

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
    def test_recognized_list_without_links_is_reported_partial(self, _login, _access):
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
        result = discover_project_chats(page, PROJECT_URL, stable_rounds=1, poll_ms=0)
        self.assertFalse(result.complete)
        self.assertEqual(result.chats, ())
        self.assertIn("partial", result.diagnostic)

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
    def test_scan_limit_returns_explicit_partial_discovery(self, _login, _access):
        sample = {
            "recognized": True,
            "name": "Project",
            "explicitEmpty": False,
            "chats": [{"href": "/g/g-p-project/c/a", "title": "A"}],
        }
        page = FakeProjectPage([sample], [True, True])
        result = discover_project_chats(
            page, PROJECT_URL, max_rounds=2, stable_rounds=1, poll_ms=0
        )
        self.assertFalse(result.complete)
        self.assertEqual([chat.chat_id for chat in result.chats], ["a"])

    @patch("cli_gpt.project.project_access_error_visible", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_ten_visible_chats_do_not_hide_later_lazy_loaded_chats(self, _login, _access):
        first = {
            "recognized": True,
            "name": "Project",
            "explicitEmpty": False,
            "chats": [
                {"href": f"/g/g-p-project/c/{index}", "title": str(index)}
                for index in range(10)
            ],
        }
        later = {
            **first,
            "chats": [
                {"href": f"/g/g-p-project/c/{index}", "title": str(index)}
                for index in range(15)
            ],
        }
        page = FakeProjectPage(
            [first, first, later, later, later],
            [True, True, True, False, False],
        )
        result = discover_project_chats(page, PROJECT_URL, stable_rounds=2, poll_ms=0)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.chats), 15)

    @patch("cli_gpt.project.project_access_error_visible", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_unscoped_generic_conversation_links_are_not_claimed(self, _login, _access):
        sample = {
            "recognized": True,
            "name": "Project",
            "explicitEmpty": False,
            "projectScoped": False,
            "chats": [
                {"href": "/c/unrelated", "title": "Sidebar chat"},
                {"href": "/g/g-p-project/c/member", "title": "Member"},
                {"href": "/g/g-p-other/c/other", "title": "Other"},
            ],
        }
        page = FakeProjectPage([sample, sample], [False, False])
        result = discover_project_chats(page, PROJECT_URL, stable_rounds=1, poll_ms=0)
        self.assertEqual([chat.chat_id for chat in result.chats], ["member"])

    @patch("cli_gpt.project.project_access_error_visible", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=True)
    def test_login_page_is_reported_explicitly(self, _login, _access):
        page = FakeProjectPage([])
        with self.assertRaises(LoginRequired):
            discover_project_chats(page, PROJECT_URL, poll_ms=0)

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_conversation_turn_sequence_produces_two_pairs(
        self, _login, _generating
    ):
        sample = {
            "recognized": True,
            "explicitEmpty": False,
            "title": "Conversation",
            "turnCount": 4,
            "invalidTurns": 0,
            "messages": [
                {"role": "user", "markdown": "Q1"},
                {"role": "assistant", "markdown": "A1"},
                {"role": "user", "markdown": "Q2"},
                {"role": "assistant", "markdown": "A2"},
            ],
        }
        page = FakeConversationPage([sample])
        chat = ProjectChat("chat", page.url, "Conversation")

        snapshot = read_conversation(page, chat, stable_rounds=1, poll_ms=0)

        self.assertEqual(
            [(pair.user, pair.assistant) for pair in snapshot.qa_pairs],
            [("Q1", "A1"), ("Q2", "A2")],
        )
        self.assertEqual(page.scroll_index, 1)
        self.assertEqual(page.waits, [])

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_overlapping_role_selectors_still_return_one_message_per_turn(
        self, _login, _generating
    ):
        sample = {
            "recognized": True,
            "explicitEmpty": False,
            "title": "Conversation",
            "turnCount": 2,
            "invalidTurns": 0,
            "messages": [
                {"role": "user", "markdown": "one user message"},
                {"role": "assistant", "markdown": "one assistant message"},
            ],
        }
        page = FakeConversationPage([sample])
        chat = ProjectChat("chat", page.url, "Conversation")

        snapshot = read_conversation(page, chat, stable_rounds=1, poll_ms=0)

        self.assertEqual(len(snapshot.qa_pairs), 1)
        arguments = page.evaluate_arguments[0]
        self.assertNotIn("messageSelectors", arguments)
        self.assertIn(
            'article[data-testid^="conversation-turn"]', arguments["turnSelectors"]
        )

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_hidden_branch_is_excluded_from_current_turn_sequence(
        self, _login, _generating
    ):
        sample = {
            "recognized": True,
            "explicitEmpty": False,
            "title": "Branched",
            "turnCount": 2,
            "invalidTurns": 0,
            "messages": [
                {"role": "user", "markdown": "visible user A prime"},
                {"role": "assistant", "markdown": "visible assistant B"},
            ],
        }
        page = FakeConversationPage([sample])
        chat = ProjectChat("chat", page.url, "Branched")

        snapshot = read_conversation(page, chat, stable_rounds=1, poll_ms=0)

        self.assertEqual(snapshot.qa_pairs[0].user, "visible user A prime")
        self.assertNotIn("hidden user A", snapshot.qa_pairs[0].user)
        self.assertIn("[hidden]", page.evaluate_arguments[0]["exclusions"])

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_branch_controls_are_not_in_assistant_markdown(
        self, _login, _generating
    ):
        sample = {
            "recognized": True,
            "explicitEmpty": False,
            "title": "Branched",
            "turnCount": 2,
            "invalidTurns": 0,
            "messages": [
                {"role": "user", "markdown": "Question"},
                {"role": "assistant", "markdown": "Answer text"},
            ],
        }
        page = FakeConversationPage([sample])
        chat = ProjectChat("chat", page.url, "Branched")

        snapshot = read_conversation(page, chat, stable_rounds=1, poll_ms=0)

        answer = snapshot.qa_pairs[0].assistant
        self.assertEqual(answer, "Answer text")
        self.assertNotIn("1 / 2", answer)
        self.assertIn(
            '[data-testid*="branch" i]', page.evaluate_arguments[0]["exclusions"]
        )

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_delayed_conversation_turns_do_not_stabilize_as_empty(
        self, _login, _generating
    ):
        loading = {
            "recognized": False,
            "explicitEmpty": False,
            "title": "",
            "turnCount": 0,
            "invalidTurns": 0,
            "messages": [],
        }
        ready = {
            "recognized": True,
            "explicitEmpty": False,
            "title": "Conversation",
            "turnCount": 2,
            "invalidTurns": 0,
            "messages": [
                {"role": "user", "markdown": "Question"},
                {"role": "assistant", "markdown": "Answer"},
            ],
        }
        page = FakeConversationPage([loading, loading, ready, ready])
        chat = ProjectChat("chat", page.url, "Conversation")

        snapshot = read_conversation(page, chat, max_rounds=4, poll_ms=0)

        self.assertEqual(len(snapshot.qa_pairs), 1)
        self.assertEqual(page.sample_index, 4)
        self.assertEqual(page.waits, [0, 0, 0])

    @patch("cli_gpt.project.pair_messages", wraps=pair_messages)
    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_long_conversation_pairs_only_after_older_history_is_loaded(
        self, _login, _generating, strict_pairing
    ):
        recent = conversation_sample(
            [turn("a5", "assistant", "A5"), *qa_turns(6, 7)]
        )
        middle = conversation_sample(
            [turn("a3", "assistant", "A3"), *qa_turns(4, 7)]
        )
        complete = conversation_sample(qa_turns(1, 7))
        page = FakeConversationPage(
            [recent, middle, complete, complete],
            [
                scroll_state(before=900, height=1000),
                scroll_state(before=500, height=1400),
                scroll_state(before=0, height=1800),
                scroll_state(before=0, height=1800),
            ],
        )
        chat = ProjectChat("chat", page.url, "Long")

        snapshot = read_conversation(page, chat, stable_rounds=2, poll_ms=0)

        self.assertEqual(len(snapshot.qa_pairs), 7)
        strict_pairing.assert_called_once()
        self.assertEqual(page.scroll_index, 4)

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_history_requires_repeated_top_scrolls_and_three_stable_rounds(
        self, _login, _generating
    ):
        first = conversation_sample(qa_turns(20, 30))
        second = conversation_sample(qa_turns(10, 30))
        complete = conversation_sample(qa_turns(1, 30))
        page = FakeConversationPage(
            [first, second, complete, complete, complete, complete],
            [
                scroll_state(before=600, height=900),
                scroll_state(before=400, height=1300),
                scroll_state(before=200, height=1700),
                scroll_state(before=0, height=1700),
                scroll_state(before=0, height=1700),
                scroll_state(before=0, height=1700),
            ],
        )
        chat = ProjectChat("chat", page.url, "Long")

        snapshot = read_conversation(
            page, chat, stable_rounds=3, max_rounds=6, poll_ms=0
        )

        self.assertEqual(len(snapshot.qa_pairs), 30)
        self.assertEqual(page.sample_index, 6)
        self.assertEqual(page.scroll_index, 6)

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_same_turn_count_with_a_new_first_turn_resets_stability(
        self, _login, _generating
    ):
        later = conversation_sample(qa_turns(20, 40))
        earlier = conversation_sample(qa_turns(10, 30))
        page = FakeConversationPage(
            [later, earlier, earlier],
            [scroll_state(), scroll_state(), scroll_state()],
        )
        chat = ProjectChat("chat", page.url, "Shifted")

        snapshot = read_conversation(
            page, chat, stable_rounds=2, max_rounds=3, poll_ms=0
        )

        self.assertEqual(page.sample_index, 3)
        self.assertEqual(len(snapshot.qa_pairs), 31)

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_stable_turn_id_uses_the_latest_hydrated_markdown(
        self, _login, _generating
    ):
        partial = conversation_sample(
            [turn("u1", "user", "Q"), turn("a1", "assistant", "partial")]
        )
        complete = conversation_sample(
            [turn("u1", "user", "Q"), turn("a1", "assistant", "complete answer")]
        )
        page = FakeConversationPage(
            [partial, complete, complete],
            [scroll_state(), scroll_state(), scroll_state()],
        )
        chat = ProjectChat("chat", page.url, "Hydrated")

        snapshot = read_conversation(
            page, chat, stable_rounds=2, max_rounds=3, poll_ms=0
        )

        self.assertEqual(snapshot.qa_pairs[0].assistant, "complete answer")

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_virtualized_overlapping_windows_are_accumulated_by_turn_id(
        self, _login, _generating
    ):
        recent = conversation_sample(qa_turns(50, 80))
        middle = conversation_sample(qa_turns(25, 55))
        oldest = conversation_sample(qa_turns(1, 30))
        page = FakeConversationPage(
            [recent, middle, oldest, oldest],
            [
                scroll_state(before=800, height=1000),
                scroll_state(before=600, height=1000),
                scroll_state(before=0, height=1000),
                scroll_state(before=0, height=1000),
            ],
        )
        chat = ProjectChat("chat", page.url, "Virtualized")

        snapshot = read_conversation(page, chat, stable_rounds=2, poll_ms=0)

        self.assertEqual(len(snapshot.qa_pairs), 80)
        self.assertEqual(snapshot.qa_pairs[0].user, "Q1")
        self.assertEqual(snapshot.qa_pairs[-1].assistant, "A80")

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_virtualization_without_stable_turn_ids_is_rejected(
        self, _login, _generating
    ):
        recent = conversation_sample(
            [turn("", "user", "Q3"), turn("", "assistant", "A3")]
        )
        older = conversation_sample(
            [turn("", "user", "Q1"), turn("", "assistant", "A1")]
        )
        page = FakeConversationPage(
            [recent, older],
            [scroll_state(before=100), scroll_state(before=0)],
        )
        chat = ProjectChat("chat", page.url, "Unsafe virtualization")

        with self.assertRaises(PageStructureChanged):
            read_conversation(page, chat, stable_rounds=1, max_rounds=2, poll_ms=0)

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_scroll_action_reports_the_ancestor_container(self, _login, _generating):
        sample = conversation_sample(qa_turns(1, 1))
        page = FakeConversationPage(
            [sample], [scroll_state(before=0, kind="ancestor")]
        )
        chat = ProjectChat("chat", page.url, "Scrolled")

        read_conversation(page, chat, stable_rounds=1, poll_ms=0)

        self.assertEqual(page.scroll_index, 1)
        script = page.scroll_scripts[0]
        self.assertIn("style.overflowY", script)
        self.assertIn("document.scrollingElement", script)
        self.assertNotIn("window.scrollTo", script)

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_attachment_only_user_gets_a_named_placeholder(
        self, _login, _generating
    ):
        sample = conversation_sample(
            [
                turn(
                    "u1",
                    "user",
                    "",
                    attachments=({"kind": "file", "name": "report.pdf"},),
                ),
                turn("a1", "assistant", "Analysis"),
            ]
        )
        page = FakeConversationPage([sample])
        chat = ProjectChat("chat", page.url, "Attachment")

        snapshot = read_conversation(page, chat, stable_rounds=1, poll_ms=0)

        self.assertEqual(snapshot.qa_pairs[0].user, "[Attachment: report.pdf]")

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_image_only_user_gets_an_image_placeholder(self, _login, _generating):
        sample = conversation_sample(
            [
                turn(
                    "u1",
                    "user",
                    "",
                    attachments=({"kind": "image", "name": ""},),
                ),
                turn("a1", "assistant", "Description"),
            ]
        )
        page = FakeConversationPage([sample])
        chat = ProjectChat("chat", page.url, "Image")

        snapshot = read_conversation(page, chat, stable_rounds=1, poll_ms=0)

        self.assertEqual(snapshot.qa_pairs[0].user, "[Image attachment]")

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_text_and_attachment_are_both_preserved(self, _login, _generating):
        sample = conversation_sample(
            [
                turn(
                    "u1",
                    "user",
                    "Analyze this file",
                    attachments=({"kind": "file", "name": "report.pdf"},),
                ),
                turn("a1", "assistant", "Analysis"),
            ]
        )
        page = FakeConversationPage([sample])
        chat = ProjectChat("chat", page.url, "Attachment")

        snapshot = read_conversation(page, chat, stable_rounds=1, poll_ms=0)

        self.assertEqual(
            snapshot.qa_pairs[0].user,
            "Analyze this file\n\n[Attachment: report.pdf]",
        )

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_empty_turn_without_attachment_evidence_is_rejected(
        self, _login, _generating
    ):
        sample = conversation_sample(
            [turn("u1", "user", ""), turn("a1", "assistant", "Answer")]
        )
        page = FakeConversationPage([sample])
        chat = ProjectChat("chat", page.url, "Empty")

        with self.assertRaises(PageStructureChanged):
            read_conversation(page, chat, stable_rounds=1, poll_ms=0)

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_history_that_never_stabilizes_is_rejected(self, _login, _generating):
        first = conversation_sample(qa_turns(2, 4))
        second = conversation_sample(qa_turns(1, 3))
        page = FakeConversationPage(
            [first, second, first, second],
            [scroll_state(), scroll_state(), scroll_state(), scroll_state()],
        )
        chat = ProjectChat("chat", page.url, "Unstable")

        with self.assertRaises(PageStructureChanged):
            read_conversation(
                page, chat, stable_rounds=2, max_rounds=4, poll_ms=0
            )

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
                turn("u1", "user", "", attachments=({"kind": "file", "name": "자료.pdf"},)),
                turn("u1", "user", "", attachments=({"kind": "file", "name": "자료.pdf"},)),
                turn("a1", "assistant", "Answer"),
            ]
        )
        self.assertEqual(len(normalized), 2)
        self.assertEqual(normalized[0]["markdown"], "[Attachment: 자료.pdf]")

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_loading_state_that_never_resolves_is_unknown(self, _login, _generating):
        loading = {
            "recognized": False,
            "loading": True,
            "explicitEmpty": False,
            "title": "",
            "turnCount": 0,
            "invalidTurns": 0,
            "messages": [],
        }
        page = FakeConversationPage([loading, loading])
        chat = ProjectChat("chat", page.url, "Conversation")
        snapshot = read_conversation(page, chat, max_rounds=2, poll_ms=0)
        self.assertEqual(snapshot.status, "unknown")
        self.assertFalse(snapshot.generating)

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

    @patch("cli_gpt.project.generation_in_progress", return_value=False)
    @patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
    def test_ambiguous_primary_role_in_one_turn_is_rejected(
        self, _login, _generating
    ):
        sample = {
            "recognized": True,
            "explicitEmpty": False,
            "title": "Ambiguous",
            "turnCount": 1,
            "invalidTurns": 1,
            "messages": [],
        }
        page = FakeConversationPage([sample])
        chat = ProjectChat("chat", page.url, "Ambiguous")

        with self.assertRaises(PageStructureChanged):
            read_conversation(page, chat, stable_rounds=1, poll_ms=0)

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
