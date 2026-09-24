"""Fail-closed CDP pagination evidence. No credentials or direct API requests."""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import ConversationHistoryIncomplete, ProjectDiscoveryIncomplete


class PaginationEvidence:
    """Connect response pages by the cursor on their actual outgoing request.

    Events only enqueue work: synchronous CDP calls inside event callbacks can
    deadlock Playwright. Pending IDs include finished responses until drain()
    has fetched, decoded, and validated their bodies.
    """

    def __init__(self, kind: str, identifier: str):
        self.kind = kind
        self.identifier = identifier
        self.requests: dict[str, str | None] = {}
        self.pending: set[str] = set()
        self.responded: set[str] = set()
        self.finished: list[str] = []
        self.pages: dict[str | None, dict] = {}
        self.errors: list[str] = []
        self.session: Any = None
        self.revision = 0

    def fail(self, message: str):
        error = (
            ProjectDiscoveryIncomplete
            if self.kind == "project"
            else ConversationHistoryIncomplete
        )
        raise error(message)

    def __enter__(self):
        return self

    def start(self, page: Any):
        try:
            self.session = page.context.new_cdp_session(page)
            self.session.on("Network.requestWillBeSent", self.request)
            self.session.on("Network.responseReceived", self.response)
            self.session.on("Network.loadingFinished", self.finish)
            self.session.on("Network.loadingFailed", self.failed)
            self.session.send(
                "Network.enable",
                {
                    "maxTotalBufferSize": 100_000_000,
                    "maxResourceBufferSize": 50_000_000,
                },
            )
        except Exception as exc:
            self.close()
            self.fail(
                f"Could not register CDP network monitoring before navigation: {exc}"
            )
        return self

    def close(self):
        if self.session is not None:
            try:
                self.session.detach()
            except Exception:
                pass
            self.session = None

    def __exit__(self, *_):
        self.close()

    def request(self, event: dict):
        if event.get("requestId") in self.requests and event.get("redirectResponse"):
            self.errors.append("A pagination request was redirected.")
            return
        request = event.get("request", {})
        parts = urlsplit(request.get("url", ""))
        if parts.hostname != "chatgpt.com" or not parts.path.startswith(
            "/backend-api/"
        ):
            return
        if self.kind == "project":
            target = re.fullmatch(
                r"/backend-api/(?:gizmos|projects)/([^/]+)/conversations/?", parts.path
            )
            key = "cursor"
        else:
            target = re.fullmatch(
                r"/backend-api/conversation/([^/]+)(/messages)?/?", parts.path
            )
            key = "before"
        if not target or unquote(target[1]) != self.identifier:
            return
        rid = event["requestId"]
        self.revision += 1
        if event.get("redirectResponse"):
            self.errors.append("A pagination request was redirected.")
        query = parse_qs(parts.query, keep_blank_values=True)
        values = query.get(key, [])
        if len(values) > 1 or (values and not values[0]):
            self.errors.append("Malformed request cursor.")
        cursor = values[0] if values else None
        if self.kind == "conversation" and bool(target[2]) != (cursor is not None):
            self.errors.append(
                "Initial/history request does not have the expected before cursor."
            )
        self.requests[rid] = cursor
        self.pending.add(rid)

    def response(self, event: dict):
        if event.get("requestId") not in self.requests:
            return
        if not 200 <= event.get("response", {}).get("status", 0) < 300:
            self.errors.append("Pagination response has a non-success HTTP status.")
        self.responded.add(event["requestId"])

    def finish(self, event: dict):
        rid = event.get("requestId")
        if rid in self.pending and rid not in self.finished:
            self.finished.append(rid)

    def failed(self, event: dict):
        if event.get("requestId") in self.pending:
            self.errors.append(
                f"Pagination loading failed: {event.get('errorText', 'unknown')}"
            )

    def drain(self):
        if self.errors:
            self.fail(self.errors[0])
        while self.finished:
            rid = self.finished.pop(0)
            try:
                if rid not in self.responded:
                    self.fail("Pagination finished without an observed HTTP response.")
                result = self.session.send(
                    "Network.getResponseBody", {"requestId": rid}
                )
                body = result["body"]
                if result.get("base64Encoded"):
                    body = base64.b64decode(body).decode("utf-8")
                self.accept(self.requests[rid], json.loads(body))
            except Exception as exc:
                self.errors.append(f"Pagination body validation failed: {exc}")
                self.fail(self.errors[-1])
            self.pending.remove(rid)
        if self.errors:
            self.fail(self.errors[0])

    def accept(self, cursor: str | None, payload: dict):
        if not isinstance(payload, dict):
            self.fail("Pagination response is not an object.")
        if self.kind == "project":
            if "cursor" not in payload or not isinstance(payload.get("items"), list):
                self.fail("Project response lacks items or explicit cursor.")
            next_cursor = payload["cursor"]
            if next_cursor is not None and (
                not isinstance(next_cursor, str) or not next_cursor
            ):
                self.fail("Invalid project response cursor.")
            for item in payload["items"]:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item["id"])
                ):
                    self.fail("Project item lacks a safe conversation ID.")
                membership = item.get(
                    "gizmo_id", item.get("project_id", self.identifier)
                )
                if membership != self.identifier:
                    self.fail("Project response contains a chat from another project.")
        else:
            info = payload.get("page_info")
            if (
                not isinstance(info, dict)
                or type(info.get("has_previous_page")) is not bool
            ):
                self.fail(
                    "Conversation response lacks boolean page_info.has_previous_page."
                )
            if "start_cursor" not in info or (
                info["start_cursor"] is not None
                and (
                    not isinstance(info["start_cursor"], str)
                    or not info["start_cursor"]
                )
            ):
                self.fail("Conversation response lacks a valid start_cursor.")
            if info["has_previous_page"] and not info["start_cursor"]:
                self.fail("Previous page exists without a start_cursor.")
            self.nodes(payload)  # Validate even disconnected/duplicate responses.
        old = self.pages.get(cursor)
        if old is not None and old != payload:
            self.fail(
                "The same request cursor returned conflicting pages; reload this stage."
            )
        self.pages[cursor] = payload
        self.revision += 1

    def chain(self) -> list[dict] | None:
        if self.errors:
            self.fail(self.errors[0])
        cursor = None
        seen = set()
        pages = []
        while cursor in self.pages:
            if cursor in seen:
                self.fail("Pagination cursor cycle detected.")
            seen.add(cursor)
            page = self.pages[cursor]
            pages.append(page)
            terminal = (
                page["cursor"] is None
                if self.kind == "project"
                else page["page_info"]["has_previous_page"] is False
            )
            if terminal:
                if seen != set(self.pages):
                    self.fail(
                        "Responses are disconnected from the initial pagination request."
                    )
                return pages if not self.pending else None
            cursor = (
                page["cursor"]
                if self.kind == "project"
                else page["page_info"]["start_cursor"]
            )
        return None

    def nodes(self, payload: dict) -> dict[str, dict]:
        mapping = payload.get("mapping")
        if not isinstance(mapping, dict):
            self.fail(
                "Conversation response lacks a message mapping; schema requires review."
            )
        nodes = {}
        for identifier, node in mapping.items():
            if (
                not isinstance(node, dict)
                or node.get("id") != identifier
                or "parent" not in node
                or "message" not in node
                or (node["parent"] is not None and not isinstance(node["parent"], str))
            ):
                self.fail("Malformed conversation mapping node.")
            message = node.get("message")
            if message is not None:
                if not isinstance(message, dict) or message.get("id") != identifier:
                    self.fail("Message UUID does not match its mapping node.")
                if not isinstance(message.get("author"), dict) or not isinstance(
                    message.get("content"), dict
                ):
                    self.fail("Message lacks author or content.")
            nodes[identifier] = node
        return nodes

    def ordered_nodes(self) -> list[dict] | None:
        pages = self.chain()
        if pages is None:
            return None
        nodes: dict[str, dict] = {}
        for page in pages:
            for identifier, node in self.nodes(page).items():
                old = nodes.get(identifier)
                # Children can grow between pages; message and parent cannot.
                if old and (old.get("message"), old["parent"]) != (
                    node.get("message"),
                    node["parent"],
                ):
                    self.fail("Overlapping pages disagree about a message.")
                nodes[identifier] = node
        ordered: list[dict] = []
        visited: set[str] = set()
        for identifier in nodes:
            trail = []
            visiting = set()
            current = identifier
            while current is not None and current not in visited:
                if current in visiting:
                    self.fail("Message parent cycle.")
                if current not in nodes:
                    self.fail(
                        "Message parent is missing from the completed page chain."
                    )
                visiting.add(current)
                trail.append(current)
                current = nodes[current]["parent"]
            for current in reversed(trail):
                visited.add(current)
                ordered.append(nodes[current])
        return ordered
