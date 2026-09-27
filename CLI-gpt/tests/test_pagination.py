"""Synthetic CDP protocol tests; no live Chrome/account claims."""

import base64
import json
import unittest
from types import SimpleNamespace

from cli_gpt.errors import ConversationHistoryIncomplete, ProjectDiscoveryIncomplete
from cli_gpt.pagination import PaginationEvidence, RequestSignature


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


def message_history(messages, *, previous=False, start="hidden"):
    return {
        "messages": messages,
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
        if method in {"Network.enable", "Network.setCacheDisabled"}:
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
    @staticmethod
    def project_payload(ids, cursor):
        return {
            "items": [
                {"id": identifier, "gizmo_id": "g-p-project"}
                for identifier in ids
            ],
            "cursor": cursor,
        }

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

    def test_plural_conversation_endpoint_and_message_list_are_supported(self):
        e, cdp = self.observer()
        newest = [node("b")["message"], node("c", role="assistant")["message"]]
        oldest = [node("a")["message"]]
        cdp.fetch(
            "https://chatgpt.com/backend-api/conversations/chat",
            message_history(newest, previous=True, start="older"),
        )
        cdp.fetch(
            "https://chatgpt.com/backend-api/conversations/chat/messages?before=older",
            message_history(oldest, start="oldest"),
            rid="older",
        )
        e.drain()
        self.assertEqual([item["id"] for item in e.ordered_nodes()], ["a", "b", "c"])

    def test_singular_and_plural_routes_share_one_cursor_chain(self):
        e, cdp = self.observer()
        cdp.fetch(
            "https://chatgpt.com/backend-api/conversation/chat",
            history([], previous=True, start="older"),
        )
        cdp.fetch(
            "https://chatgpt.com/backend-api/conversations/chat/messages?before=older",
            history([]),
            rid="older",
        )
        e.drain()
        self.assertEqual(len(e.chain()), 2)

    def test_duplicate_page_ignores_unrelated_volatile_metadata(self):
        e = PaginationEvidence("conversation", "chat")
        first = message_history([node("a")["message"]], start="a")
        second = {**first, "update_time": 2, "owner": {"volatile": True}}
        e.accept(None, first)
        e.accept(None, second)
        self.assertEqual([item["id"] for item in e.ordered_nodes()], ["a"])

    def test_duplicate_page_still_rejects_conflicting_messages(self):
        e = PaginationEvidence("conversation", "chat")
        e.accept(None, message_history([node("a")["message"]]))
        with self.assertRaises(ConversationHistoryIncomplete):
            e.accept(
                None,
                message_history([node("a", text="changed")["message"]]),
            )

    def test_plural_endpoint_accepts_single_conversation_list_wrapper(self):
        e, cdp = self.observer()
        payload = {
            **message_history([node("a")["message"]]),
            "conversation_id": "chat",
        }
        cdp.fetch(
            "https://chatgpt.com/backend-api/conversations/chat",
            [payload],
        )
        e.drain()
        self.assertEqual([item["id"] for item in e.ordered_nodes()], ["a"])

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
        e.accept(None, self.project_payload(["a"], None))
        with self.assertRaises(ProjectDiscoveryIncomplete):
            e.accept(None, {"items": [], "cursor": None})

    def test_same_cursor_with_different_request_conditions_is_not_a_conflict(self):
        e, cdp = self.observer("project", "g-p-project")
        smaller = self.project_payload([f"c{index}" for index in range(5)], "small-next")
        larger = self.project_payload([f"c{index}" for index in range(10)], "large-next")
        cdp.fetch(
            "https://chatgpt.com/backend-api/gizmos/g-p-project/conversations?cursor=0&limit=5&owned_only=true",
            smaller,
            rid="owned",
        )
        cdp.fetch(
            "https://chatgpt.com/backend-api/gizmos/g-p-project/conversations?cursor=0",
            larger,
            rid="full",
        )
        e.drain()
        self.assertEqual(len(e.pages), 2)
        self.assertEqual(e.selected_project_pages(), [larger])
        self.assertIsNone(e.chain())

    def test_owned_only_false_is_a_full_project_chain(self):
        e, cdp = self.observer("project", "g-p-project")
        cdp.fetch(
            "https://chatgpt.com/backend-api/gizmos/g-p-project/conversations?cursor=0&limit=50&owned_only=false",
            self.project_payload(["a", "b"], None),
        )
        e.drain()
        self.assertEqual(len(e.chain()), 1)
        self.assertEqual(
            dict(e.project_signature().filters),
            {"limit": ("50",), "owned_only": ("false",)},
        )

    def test_conditioned_pages_are_order_independent(self):
        for order in (("owned", "full"), ("full", "owned")):
            with self.subTest(order=order):
                e, cdp = self.observer("project", "g-p-project")
                payloads = {
                    "owned": (
                        "https://chatgpt.com/backend-api/gizmos/g-p-project/conversations?cursor=0&limit=5&owned_only=true",
                        self.project_payload(["a"], "owned-next"),
                    ),
                    "full": (
                        "https://chatgpt.com/backend-api/gizmos/g-p-project/conversations?cursor=0",
                        self.project_payload(["a", "b"], None),
                    ),
                }
                for name in order:
                    url, payload = payloads[name]
                    cdp.fetch(url, payload, rid=name)
                e.drain()
                self.assertEqual(
                    [item["id"] for item in e.chain()[0]["items"]], ["a", "b"]
                )

    def test_project_cursor_zero_is_the_verified_initial_page(self):
        e, cdp = self.observer("project", "g-p-project")
        cdp.fetch(
            "https://chatgpt.com/backend-api/gizmos/g-p-project/conversations?cursor=opaque",
            self.project_payload(["b"], None),
        )
        e.drain()
        self.assertIsNone(e.chain())
        cdp.fetch(
            "https://chatgpt.com/backend-api/gizmos/g-p-project/conversations?cursor=0",
            self.project_payload(["a"], "opaque"),
            rid="initial",
        )
        e.drain()
        self.assertEqual(len(e.chain()), 2)

    def test_same_condition_same_cursor_conflict_is_rejected(self):
        signature = RequestSignature(
            "project",
            "/backend-api/gizmos/g-p-project/conversations",
            (("limit", ("10",)),),
        )
        e = PaginationEvidence("project", "g-p-project")
        e.accept("0", self.project_payload(["a"], None), signature=signature)
        with self.assertRaises(ProjectDiscoveryIncomplete):
            e.accept("0", self.project_payload(["b"], None), signature=signature)

    def test_global_history_is_tracked_but_never_used_as_project_root(self):
        e, cdp = self.observer("project", "g-p-project")
        cdp.fetch(
            "https://chatgpt.com/backend-api/conversations?offset=0&limit=28",
            {"items": [{"id": "foreign", "gizmo_id": "g-p-other"}], "total": 1},
        )
        e.drain()
        self.assertEqual(len(e.auxiliary_pages), 1)
        self.assertEqual(e.selected_project_pages(), [])
        self.assertIsNone(e.chain())

    def test_aborted_global_history_does_not_abort_project_chain(self):
        e, cdp = self.observer("project", "g-p-project")
        cdp.emit(
            "requestWillBeSent",
            {
                "requestId": "global",
                "request": {
                    "url": "https://chatgpt.com/backend-api/conversations?offset=0&limit=28"
                },
            },
        )
        cdp.emit(
            "loadingFailed", {"requestId": "global", "errorText": "net::ERR_ABORTED"}
        )
        cdp.fetch(
            "https://chatgpt.com/backend-api/gizmos/g-p-project/conversations?cursor=0",
            self.project_payload(["a"], None),
            rid="project",
        )
        e.drain()
        self.assertEqual(len(e.chain()), 1)
        self.assertIn("ERR_ABORTED", e.auxiliary_errors[0])

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
