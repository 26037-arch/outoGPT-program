"""Synthetic CDP protocol tests; no live Chrome/account claims."""

import base64
import json
import unittest
from types import SimpleNamespace

from cli_gpt.errors import ConversationHistoryIncomplete, ProjectDiscoveryIncomplete
from cli_gpt.pagination import PaginationEvidence


def node(identifier, parent=None, role="user", text="Question", **overrides):
    message = {
        "id": identifier,
        "author": {"role": role},
        "content": {"content_type": "text", "parts": [text]},
        "status": "finished_successfully",
        **overrides,
    }
    return {"id": identifier, "parent": parent, "message": message}


def history(nodes, *, previous=False, start="hidden"):
    return {
        "mapping": {n["id"]: n for n in nodes},
        "page_info": {"has_previous_page": previous, "start_cursor": start},
    }


class CDP:
    def __init__(self):
        self.handlers = {}
        self.bodies = {}
        self.enabled = False
        self.detached = False
        self.body_hook = lambda: None

    def on(self, name, callback):
        self.handlers[name] = callback

    def send(self, method, args):
        if method == "Network.enable":
            self.enabled = True
            return {}
        if method == "Network.getResponseBody":
            self.body_hook()
            value = self.bodies[args["requestId"]]
            if isinstance(value, Exception):
                raise value
            return value
        raise AssertionError(method)

    def detach(self):
        self.detached = True

    def emit(self, name, data):
        self.handlers["Network." + name](data)

    def fetch(
        self, url, payload, *, rid="request", finish=True, status=200, encoded=False
    ):
        self.emit("requestWillBeSent", {"requestId": rid, "request": {"url": url}})
        self.emit(
            "responseReceived", {"requestId": rid, "response": {"status": status}}
        )
        body = json.dumps(payload)
        self.bodies[rid] = {
            "body": base64.b64encode(body.encode()).decode() if encoded else body,
            "base64Encoded": encoded,
        }
        if finish:
            self.emit("loadingFinished", {"requestId": rid})


class PaginationTests(unittest.TestCase):
    def test_thousands_of_messages_do_not_exhaust_python_recursion(self):
        e = PaginationEvidence("conversation", "chat")
        nodes = [node(str(i), str(i - 1) if i else None) for i in range(4000)]
        e.accept(None, history(list(reversed(nodes))))
        self.assertEqual(
            [n["id"] for n in e.ordered_nodes()], [str(i) for i in range(4000)]
        )

    def observer(self, kind="conversation", identifier="chat"):
        cdp = CDP()
        page = SimpleNamespace(context=SimpleNamespace(new_cdp_session=lambda _: cdp))
        return PaginationEvidence(kind, identifier).start(page), cdp

    def test_pending_includes_body_parsing_after_loading_finished(self):
        evidence, cdp = self.observer()
        cdp.fetch("https://chatgpt.com/backend-api/conversation/chat", history([]))
        self.assertIsNone(evidence.chain())
        cdp.body_hook = lambda: self.assertIn("request", evidence.pending)
        evidence.drain()
        self.assertIsNotNone(evidence.chain())

    def test_terminal_waits_for_another_inflight_request(self):
        evidence, cdp = self.observer()
        cdp.fetch("https://chatgpt.com/backend-api/conversation/chat", history([]))
        evidence.drain()
        cdp.fetch(
            "https://chatgpt.com/backend-api/conversation/chat",
            history([]),
            rid="late",
            finish=False,
        )
        self.assertIsNone(evidence.chain())
        cdp.emit("loadingFinished", {"requestId": "late"})
        evidence.drain()
        self.assertIsNotNone(evidence.chain())

    def test_out_of_order_chain_and_hidden_system_start(self):
        e = PaginationEvidence("conversation", "chat")
        e.accept("sys", history([node("root", role="system")]))
        self.assertIsNone(e.chain())
        e.accept(
            None,
            history(
                [node("sys", "root", "system"), node("u", "sys")],
                previous=True,
                start="sys",
            ),
        )
        self.assertEqual([n["id"] for n in e.ordered_nodes()], ["root", "sys", "u"])

    def test_missing_page_or_terminal_is_not_complete(self):
        e = PaginationEvidence("conversation", "chat")
        e.accept(None, history([], previous=True, start="older"))
        self.assertIsNone(e.chain())

    def test_cycle_and_disconnected_terminal_rejected(self):
        for second_cursor, second in [
            ("x", history([], previous=True, start="x")),
            ("orphan", history([])),
        ]:
            e = PaginationEvidence("conversation", "chat")
            e.accept(None, history([], previous=second_cursor == "x", start="x"))
            e.accept(second_cursor, second)
            with self.assertRaises(ConversationHistoryIncomplete):
                e.chain()

    def test_missing_fields_and_non_boolean_terminal_rejected(self):
        for payload in [
            {},
            {"mapping": {}, "page_info": {}},
            {
                "mapping": {},
                "page_info": {"has_previous_page": 0, "start_cursor": None},
            },
        ]:
            with self.assertRaises(ConversationHistoryIncomplete):
                PaginationEvidence("conversation", "chat").accept(None, payload)

    def test_failed_http_body_json_and_loading_are_not_ignored(self):
        for mode in ["http", "body", "json", "loading"]:
            with self.subTest(mode=mode):
                e, cdp = self.observer()
                cdp.fetch(
                    "https://chatgpt.com/backend-api/conversation/chat",
                    history([]),
                    status=503 if mode == "http" else 200,
                )
                if mode == "body":
                    cdp.bodies["request"] = RuntimeError("evicted body")
                if mode == "json":
                    cdp.bodies["request"] = {"body": "not json"}
                if mode == "loading":
                    cdp.emit(
                        "loadingFailed", {"requestId": "request", "errorText": "reset"}
                    )
                with self.assertRaises(ConversationHistoryIncomplete):
                    e.drain()

    def test_unrelated_requests_are_ignored_and_base64_is_decoded(self):
        e, cdp = self.observer()
        cdp.fetch(
            "https://example.org/backend-api/conversation/chat", {}, rid="unrelated"
        )
        cdp.fetch("https://chatgpt.com/backend-api/conversation/other", {}, rid="other")
        cdp.fetch(
            "https://chatgpt.com/backend-api/conversation/chat",
            history([]),
            encoded=True,
        )
        e.drain()
        self.assertEqual(set(e.requests), {"request"})
        e.close()
        self.assertTrue(cdp.detached)

    def test_actual_before_request_cursor_connects_pages(self):
        e, cdp = self.observer()
        cdp.fetch(
            "https://chatgpt.com/backend-api/conversation/chat",
            history([], previous=True, start="opaque+/="),
        )
        cdp.fetch(
            "https://chatgpt.com/backend-api/conversation/chat/messages?before=opaque%2B%2F%3D",
            history([]),
            rid="older",
        )
        e.drain()
        self.assertEqual(len(e.chain()), 2)

    def test_project_null_is_required_not_absent_empty_or_scroll_end(self):
        e = PaginationEvidence("project", "g-p-project")
        for payload in [{"items": []}, {"items": [], "cursor": ""}]:
            with self.assertRaises(ProjectDiscoveryIncomplete):
                e.accept(None, payload)
        e.accept(None, {"items": [], "cursor": "next"})
        self.assertIsNone(e.chain())
        e.accept("next", {"items": [], "cursor": None})
        self.assertEqual(len(e.chain()), 2)

    def test_project_membership_and_duplicate_conflicts_rejected(self):
        e = PaginationEvidence("project", "g-p-project")
        with self.assertRaises(ProjectDiscoveryIncomplete):
            e.accept(
                None, {"items": [{"id": "a", "gizmo_id": "g-p-other"}], "cursor": None}
            )
        e.accept(None, {"items": [{"id": "a"}], "cursor": None})
        with self.assertRaises(ProjectDiscoveryIncomplete):
            e.accept(None, {"items": [], "cursor": None})

    def test_missing_parent_and_conflicting_overlap_rejected(self):
        e = PaginationEvidence("conversation", "chat")
        e.accept(None, history([node("u", "missing")]))
        with self.assertRaises(ConversationHistoryIncomplete):
            e.ordered_nodes()
        e = PaginationEvidence("conversation", "chat")
        e.accept(None, history([node("u")], previous=True, start="u"))
        e.accept("u", history([node("u", text="different")]))
        with self.assertRaises(ConversationHistoryIncomplete):
            e.ordered_nodes()
