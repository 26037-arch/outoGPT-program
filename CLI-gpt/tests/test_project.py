"""Synthetic navigation/CDP/DOM integration tests (not live Chrome)."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cli_gpt.errors import (
    ConversationHistoryIncomplete,
    ConversationLoadingUnknown,
    LoginRequired,
)
from cli_gpt.project import (
    ProjectChat,
    discover_project_chats,
    read_conversation,
    _normalize_conversation_messages,
)
from test_pagination import CDP, history, node

PROJECT = "https://chatgpt.com/g/g-p-project/project"
CHAT = ProjectChat("chat", "https://chatgpt.com/g/g-p-project/c/chat", "Chat")
INITIAL = "https://chatgpt.com/backend-api/conversation/chat"
LIST = "https://chatgpt.com/backend-api/gizmos/g-p-project/conversations"


def dom(identifier, role="user", text="Question"):
    return {
        "messageId": identifier,
        "turnId": "data-message-id:" + identifier,
        "role": role,
        "markdown": text,
    }


class Page:
    def __init__(self, samples=(), events=None):
        self.cdp = CDP()
        self.context = SimpleNamespace(new_cdp_session=lambda _: self.cdp)
        self.samples = list(samples)
        self.events = events or {}
        self.tick = 0
        self.sample_index = 0
        self.url = CHAT.chat_url
        self.scrolled = 0
        self.arguments = []

    def goto(self, url, **_):
        assert self.cdp.enabled, "Network monitoring must precede navigation"
        assert len(self.cdp.handlers) == 4
        self.url = url
        for event in self.events.get(0, []):
            event(self.cdp)

    def wait_for_timeout(self, _):
        self.tick += 1
        for event in self.events.get(self.tick, []):
            event(self.cdp)

    def evaluate(self, script, argument):
        self.arguments.append(argument)
        if "OUTOGPT_PROJECT_DISCOVERY" in script:
            return {
                "recognized": True,
                "ready": True,
                "name": "Project",
                "chats": [],
                "explicitEnd": True,
                "explicitEmpty": True,
            }
        if "EXTRACTION" in script:
            sample = self.samples[min(self.sample_index, len(self.samples) - 1)]
            self.sample_index += 1
            return {
                "recognized": True,
                "title": "Chat",
                "messages": sample,
                "invalidTurns": 0,
                "explicitEmpty": not sample,
            }
        self.scrolled += 1
        return {"found": True, "atTop": True, "wasAtTop": True, "atEnd": True}


def fetch(url, payload, **kwargs):
    return lambda cdp: cdp.fetch(url, payload, **kwargs)


@patch("cli_gpt.project.project_access_error_visible", return_value=False)
@patch("cli_gpt.project.login_or_challenge_visible", return_value=False)
@patch("cli_gpt.project.generation_in_progress", return_value=False)
class ProjectDomTests(unittest.TestCase):
    def read(self, page, **kwargs):
        return read_conversation(page, CHAT, max_rounds=12, poll_ms=0, **kwargs)

    def test_navigation_registers_monitor_and_waits_for_initial_request(self, *_):
        p = Page(
            [[dom("u"), dom("a", "assistant", "Answer")]],
            {
                3: [
                    fetch(
                        INITIAL,
                        history([node("u"), node("a", "u", "assistant", "Answer")]),
                    )
                ]
            },
        )
        result = self.read(p)
        self.assertEqual(result.qa_pairs[0].assistant, "Answer")
        self.assertGreaterEqual(p.tick, 5)
        self.assertTrue(p.cdp.detached)

    def test_stable_dom_without_terminal_network_never_completes(self, *_):
        p = Page(
            [[dom("u")]],
            {0: [fetch(INITIAL, history([node("u")], previous=True, start="u"))]},
        )
        with self.assertRaises(ConversationHistoryIncomplete):
            self.read(p)

    def test_terminal_does_not_finish_until_body_is_parsed(self, *_):
        p = Page(
            [[dom("u")]],
            {
                0: [fetch(INITIAL, history([node("u")]), finish=False)],
                5: [lambda cdp: cdp.emit("loadingFinished", {"requestId": "request"})],
            },
        )
        result = self.read(p)
        self.assertEqual(len(result.messages), 1)
        self.assertGreaterEqual(p.tick, 7)

    def test_disjoint_virtualized_windows_accumulate_by_message_uuid(self, *_):
        p = Page(
            [[dom("a", "assistant", "Answer")], [], [dom("u")]],
            {
                0: [
                    fetch(
                        INITIAL,
                        history(
                            [node("a", "u", "assistant", "Answer")],
                            previous=True,
                            start="u",
                        ),
                    )
                ],
                2: [
                    fetch(
                        INITIAL + "/messages?before=u",
                        history(
                            [
                                node("u", "sys"),
                                node("sys", role="system", text="hidden"),
                            ]
                        ),
                        rid="older",
                    )
                ],
            },
        )
        result = self.read(p)
        self.assertEqual([m["id"] for m in result.messages], ["u", "a"])
        self.assertEqual(result.non_ui_messages[0]["id"], "sys")
        self.assertEqual(len(result.qa_pairs), 1)

    def test_missing_message_and_same_count_wrong_content_block_completion(self, *_):
        for sample in [[dom("u")], [dom("u"), dom("a", "assistant", "Wrong")]]:
            p = Page(
                [sample],
                {
                    0: [
                        fetch(
                            INITIAL,
                            history([node("u"), node("a", "u", "assistant", "Answer")]),
                        )
                    ]
                },
            )
            with self.assertRaises(ConversationHistoryIncomplete):
                self.read(p)

    def test_delayed_content_hydration_uses_latest_uuid_value(self, *_):
        p = Page(
            [[dom("u"), dom("a", "assistant", "partial")]] * 5
            + [[dom("u"), dom("a", "assistant", "Answer")]],
            {
                0: [
                    fetch(
                        INITIAL,
                        history([node("u"), node("a", "u", "assistant", "Answer")]),
                    )
                ]
            },
        )
        self.assertEqual(self.read(p).qa_pairs[0].assistant, "Answer")
        self.assertGreaterEqual(p.tick, 7)

    def test_missing_uuid_is_not_replaced_with_dom_turn_position(self, *_):
        p = Page(
            [[{"role": "user", "markdown": "Question", "turnId": "turn-0"}]],
            {0: [fetch(INITIAL, history([node("u")]))]},
        )
        with self.assertRaises(ConversationHistoryIncomplete):
            self.read(p)

    def test_unfinished_network_assistant_cannot_be_saved(self, *_):
        p = Page(
            [[dom("u"), dom("a", "assistant", "partial")]],
            {
                0: [
                    fetch(
                        INITIAL,
                        history(
                            [
                                node("u"),
                                node(
                                    "a",
                                    "u",
                                    "assistant",
                                    "partial",
                                    status="in_progress",
                                ),
                            ]
                        ),
                    )
                ]
            },
        )
        with self.assertRaises(ConversationHistoryIncomplete):
            self.read(p)

    def test_generation_waits_then_requires_fresh_authoritative_read(
        self, generating, *_
    ):
        generating.side_effect = [True, True, False]
        p = Page([[]], {0: [fetch(INITIAL, history([]))]})
        with self.assertRaises(ConversationLoadingUnknown):
            self.read(p)
        self.assertEqual(p.tick, 2)
        self.assertEqual(p.sample_index, 0)

    def test_generation_timeout_is_error_not_skip(self, generating, *_):
        generating.return_value = True
        p = Page([[]], {0: [fetch(INITIAL, history([]))]})
        with self.assertRaises(ConversationHistoryIncomplete):
            self.read(p)

    def test_login_is_reported_and_monitor_detached(self, _generation, login, *_):
        login.return_value = True
        p = Page([[]])
        with self.assertRaises(LoginRequired):
            self.read(p)
        self.assertTrue(p.cdp.detached)

    def test_project_waits_for_cursor_null_despite_static_scroll_end(self, *_):
        p = Page(
            events={
                0: [
                    fetch(
                        LIST, {"items": [{"id": "a", "title": "A"}], "cursor": "page2"}
                    )
                ],
                5: [
                    fetch(
                        LIST + "?cursor=page2",
                        {"items": [{"id": "b", "title": "B"}], "cursor": None},
                        rid="p2",
                    )
                ],
            }
        )
        result = discover_project_chats(p, PROJECT, max_rounds=12, poll_ms=0)
        self.assertTrue(result.complete)
        self.assertEqual([c.chat_id for c in result.chats], ["a", "b"])
        self.assertGreaterEqual(p.tick, 7)

    def test_project_missing_network_is_partial_even_if_ui_says_empty(self, *_):
        result = discover_project_chats(Page(), PROJECT, max_rounds=4, poll_ms=0)
        self.assertFalse(result.complete)

    def test_explicit_empty_network_conversation_is_verified(self, *_):
        p = Page([[]], {0: [fetch(INITIAL, history([]))]})
        self.assertEqual(self.read(p).messages, ())

    def test_unknown_role_and_visible_branch_are_rejected(self, *_):
        for nodes in [
            [node("u", role="unknown")],
            [
                node("u"),
                node("a", "u", "assistant", "Answer"),
                node("b", "u", "assistant", "Other"),
            ],
        ]:
            p = Page(
                [
                    [
                        dom("u"),
                        dom("a", "assistant", "Answer"),
                        dom("b", "assistant", "Other"),
                    ]
                ],
                {0: [fetch(INITIAL, history(nodes))]},
            )
            with self.assertRaises(ConversationHistoryIncomplete):
                self.read(p)

    def test_existing_converter_arguments_and_attachments_are_preserved(self, *_):
        p = Page([[dom("u")]], {0: [fetch(INITIAL, history([node("u")]))]})
        self.read(p)
        self.assertIn(
            'article[data-testid^="conversation-turn"]', p.arguments[0]["turnSelectors"]
        )
        self.assertIn('[data-testid*="branch" i]', p.arguments[0]["exclusions"])
        message = _normalize_conversation_messages(
            [
                {
                    "role": "user",
                    "markdown": "Analyze",
                    "attachments": [{"kind": "file", "name": "report.pdf"}],
                }
            ]
        )[0]
        self.assertEqual(message["markdown"], "Analyze\n\n[Attachment: report.pdf]")

    def test_file_and_image_attachments_require_network_and_dom_evidence(self, *_):
        file_node = node(
            "u",
            text="Analyze",
            metadata={"attachments": [{"id": "file-1", "name": "report.pdf"}]},
        )
        file_dom = {
            **dom("u", text="Analyze"),
            "attachments": [{"kind": "file", "name": "report.pdf"}],
        }
        p = Page([[file_dom]], {0: [fetch(INITIAL, history([file_node]))]})
        self.assertIn("report.pdf", self.read(p).messages[0]["markdown"])
        image_node = node(
            "u",
            content={
                "content_type": "multimodal_text",
                "parts": [
                    "Describe",
                    {"content_type": "image_asset_pointer", "asset_pointer": "image-1"},
                ],
            },
        )
        image_dom = {
            **dom("u", text="Describe"),
            "attachments": [{"kind": "image", "name": ""}],
        }
        p = Page([[image_dom]], {0: [fetch(INITIAL, history([image_node]))]})
        self.assertIn("[Image attachment]", self.read(p).messages[0]["markdown"])
        p = Page(
            [[dom("u", text="Describe")]], {0: [fetch(INITIAL, history([image_node]))]}
        )
        with self.assertRaises(ConversationHistoryIncomplete):
            self.read(p)
