"""Fail-closed CDP pagination evidence. No credentials or direct API requests."""

from __future__ import annotations

import base64
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import ConversationHistoryIncomplete, ProjectDiscoveryIncomplete


@dataclass(frozen=True)
class RequestSignature:
    """The request conditions that give a cursor its meaning."""

    scope: str
    path: str
    filters: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class RequestRecord:
    request_id: str
    url: str
    path: str
    cursor: str | None
    page_size: int | None
    filters: tuple[tuple[str, tuple[str, ...]], ...]
    signature: RequestSignature


@dataclass(frozen=True)
class PageKey:
    signature: RequestSignature
    cursor: str | None


class PaginationEvidence:
    """Connect pages only when endpoint, filters, page size, and cursor agree.

    Events only enqueue work: synchronous CDP calls inside event callbacks can
    deadlock Playwright. Pending IDs include finished responses until ``drain``
    has fetched, decoded, and validated their bodies.
    """

    def __init__(self, kind: str, identifier: str):
        if kind not in {"project", "conversation"}:
            raise ValueError(f"Unsupported pagination kind: {kind}")
        self.kind = kind
        self.identifier = identifier
        self.requests: dict[str, RequestRecord] = {}
        self.pending: set[str] = set()
        self.responded: set[str] = set()
        self.finished: list[str] = []
        self.pages: dict[PageKey, dict] = {}
        self.auxiliary_pages: dict[PageKey, dict] = {}
        self.errors: list[str] = []
        self.auxiliary_errors: list[str] = []
        self.session: Any = None
        self.revision = 0
        self.parsed_responses = 0

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
            self.session.send("Network.setCacheDisabled", {"cacheDisabled": True})
        except Exception as exc:
            self.close()
            self.fail(
                "Could not register CDP network monitoring before navigation: "
                f"{type(exc).__name__}: {exc}"
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

    @staticmethod
    def _page_size(query: dict[str, list[str]]) -> int | None:
        values = query.get("limit", [])
        if len(values) != 1:
            return None
        try:
            value = int(values[0])
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _record(self, event: dict) -> RequestRecord | None:
        request = event.get("request", {})
        url = str(request.get("url", ""))
        parts = urlsplit(url)
        if parts.hostname != "chatgpt.com" or not parts.path.startswith(
            "/backend-api/"
        ):
            return None
        query = parse_qs(parts.query, keep_blank_values=True)
        if self.kind == "project":
            project_path = f"/backend-api/gizmos/{self.identifier}/conversations"
            alternate_path = f"/backend-api/projects/{self.identifier}/conversations"
            if parts.path.rstrip("/") in {project_path, alternate_path}:
                scope = "project"
                cursor_key = "cursor"
                logical_path = project_path
            elif parts.path.rstrip("/") == "/backend-api/conversations":
                scope = "global"
                cursor_key = "offset"
                logical_path = "/backend-api/conversations"
            else:
                return None
        else:
            target = re.fullmatch(
                r"/backend-api/conversations?/([^/]+)(/messages)?/?", parts.path
            )
            if not target or unquote(target[1]) != self.identifier:
                return None
            scope = "conversation"
            cursor_key = "before"
            # ChatGPT has used both the singular and plural route. They carry
            # the same before/page_info chain, so normalize only this spelling.
            logical_path = f"/backend-api/conversations/{self.identifier}"
            values = query.get(cursor_key, [])
            if bool(target[2]) != bool(values):
                self.errors.append(
                    "Initial/history request does not have the expected before cursor."
                )

        cursor_values = query.get(cursor_key, [])
        if len(cursor_values) > 1 or (
            cursor_values and not cursor_values[0]
        ):
            self.errors.append(f"Malformed request {cursor_key} cursor.")
        cursor = cursor_values[0] if cursor_values else None
        filters = tuple(
            (key, tuple(values))
            for key, values in sorted(query.items())
            if key != cursor_key
        )
        signature = RequestSignature(scope, logical_path, filters)
        return RequestRecord(
            request_id=str(event["requestId"]),
            url=url,
            path=parts.path,
            cursor=cursor,
            page_size=self._page_size(query),
            filters=filters,
            signature=signature,
        )

    def request(self, event: dict):
        request_id = str(event.get("requestId", ""))
        if request_id in self.requests and event.get("redirectResponse"):
            self.errors.append("A pagination request was redirected.")
            return
        record = self._record(event)
        if record is None:
            return
        if event.get("redirectResponse"):
            self.errors.append("A pagination request was redirected.")
        self.requests[record.request_id] = record
        self.pending.add(record.request_id)
        self.revision += 1

    def response(self, event: dict):
        request_id = str(event.get("requestId", ""))
        if request_id not in self.requests:
            return
        status = event.get("response", {}).get("status", 0)
        if not 200 <= status < 300:
            record = self.requests[request_id]
            message = (
                f"{record.signature.scope} pagination response has HTTP status {status}."
            )
            if record.signature.scope == "global":
                self.auxiliary_errors.append(message)
            else:
                self.errors.append(message)
        self.responded.add(request_id)

    def finish(self, event: dict):
        request_id = str(event.get("requestId", ""))
        if request_id in self.pending and request_id not in self.finished:
            self.finished.append(request_id)

    def failed(self, event: dict):
        request_id = str(event.get("requestId", ""))
        if request_id in self.pending:
            record = self.requests[request_id]
            message = (
                f"{record.signature.scope} pagination loading failed: "
                f"{event.get('errorText', 'unknown')}"
            )
            if record.signature.scope == "global":
                self.auxiliary_errors.append(message)
                self.pending.discard(request_id)
                self.revision += 1
            else:
                self.errors.append(message)

    def drain(self):
        if self.errors:
            self.fail(self.errors[0])
        while self.finished:
            request_id = self.finished.pop(0)
            record = self.requests[request_id]
            try:
                if request_id not in self.responded:
                    self.fail("Pagination finished without an observed HTTP response.")
                result = self.session.send(
                    "Network.getResponseBody", {"requestId": request_id}
                )
                body = result["body"]
                if result.get("base64Encoded"):
                    body = base64.b64decode(body).decode("utf-8")
                self.accept_record(record, json.loads(body))
            except (ProjectDiscoveryIncomplete, ConversationHistoryIncomplete):
                raise
            except Exception as exc:
                message = (
                    "Pagination body validation failed at "
                    f"{record.path}: {type(exc).__name__}: {exc}"
                )
                if record.signature.scope == "global":
                    self.auxiliary_errors.append(message)
                else:
                    self.errors.append(message)
                    self.fail(self.errors[-1])
            finally:
                self.pending.discard(request_id)
        if self.errors:
            self.fail(self.errors[0])

    def _validate_project_items(self, payload: dict, *, require_membership: bool):
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
            membership = item.get("gizmo_id", item.get("project_id"))
            if require_membership and membership != self.identifier:
                self.fail("Project response contains a chat from another project.")

    def _store(self, target: dict[PageKey, dict], key: PageKey, payload: dict):
        old = target.get(key)
        if old is not None and self._page_identity(old) != self._page_identity(payload):
            filters = dict(key.signature.filters)
            self.fail(
                "The same endpoint, filters, page size, and request cursor returned "
                f"conflicting pages (scope={key.signature.scope}, filters={filters})."
            )
        target[key] = payload
        self.parsed_responses += 1
        self.revision += 1

    def _page_identity(self, payload: dict):
        """Compare the page itself, excluding unrelated volatile metadata."""
        if self.kind == "project":
            return (
                payload.get("cursor"),
                tuple(
                    item.get("id") if isinstance(item, dict) else None
                    for item in payload.get("items", ())
                ),
            )
        return (
            payload.get("page_info"),
            payload.get("messages")
            if isinstance(payload.get("messages"), list)
            else payload.get("mapping"),
        )

    def _accept_global(self, record: RequestRecord, payload: dict):
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            self.fail("Global conversation response lacks an items list.")
        for item in payload["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                self.fail("Global conversation item is malformed.")
        self._store(
            self.auxiliary_pages,
            PageKey(record.signature, record.cursor),
            payload,
        )

    def accept_record(self, record: RequestRecord, payload: dict | list):
        if self.kind == "conversation" and isinstance(payload, list):
            matching = [
                item
                for item in payload
                if isinstance(item, dict)
                and item.get("conversation_id") == self.identifier
            ]
            if len(matching) != 1:
                self.fail(
                    "Conversation response list does not contain exactly one requested chat."
                )
            payload = matching[0]
        if not isinstance(payload, dict):
            self.fail("Pagination response is not an object.")
        if record.signature.scope == "global":
            self._accept_global(record, payload)
            return
        self._accept_page(PageKey(record.signature, record.cursor), payload)

    def _accept_page(self, key: PageKey, payload: dict):
        if self.kind == "project":
            self._validate_project_items(payload, require_membership=True)
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
            self.nodes(payload)
        self._store(self.pages, key, payload)

    def _default_signature(self) -> RequestSignature:
        if self.kind == "project":
            path = f"/backend-api/gizmos/{self.identifier}/conversations"
        else:
            path = f"/backend-api/conversations/{self.identifier}"
        return RequestSignature(self.kind, path, ())

    def accept(
        self,
        cursor: str | None,
        payload: dict,
        *,
        signature: RequestSignature | None = None,
    ):
        """Test/fixture boundary for a response whose request was already known."""
        self._accept_page(PageKey(signature or self._default_signature(), cursor), payload)

    def _chain_for(
        self, signature: RequestSignature, root: str | None
    ) -> list[dict] | None:
        cursor = root
        seen: set[str | None] = set()
        pages = []
        available = {
            key.cursor for key in self.pages if key.signature == signature
        }
        while PageKey(signature, cursor) in self.pages:
            if cursor in seen:
                self.fail("Pagination cursor cycle detected.")
            seen.add(cursor)
            page = self.pages[PageKey(signature, cursor)]
            pages.append(page)
            terminal = (
                page["cursor"] is None
                if self.kind == "project"
                else page["page_info"]["has_previous_page"] is False
            )
            if terminal:
                if seen != available:
                    self.fail(
                        "Responses are disconnected from the initial pagination request."
                    )
                return pages
            cursor = (
                page["cursor"]
                if self.kind == "project"
                else page["page_info"]["start_cursor"]
            )
        return None

    @staticmethod
    def _project_rank(signature: RequestSignature) -> tuple[int, int]:
        filters = dict(signature.filters)
        owned_values = tuple(value.casefold() for value in filters.get("owned_only", ()))
        if owned_values and all(value in {"false", "0"} for value in owned_values):
            filters.pop("owned_only", None)
        extra = set(filters) - {"limit"}
        if extra:
            return (2, 0)
        if "limit" not in filters:
            return (0, 0)
        try:
            size = int(filters["limit"][0])
        except (ValueError, TypeError, IndexError):
            size = 0
        return (1, -size)

    def project_signature(self) -> RequestSignature | None:
        signatures = {
            record.signature
            for record in self.requests.values()
            if record.signature.scope == "project"
        } | {
            key.signature
            for key in self.pages
            if key.signature.scope == "project"
        }
        eligible = [item for item in signatures if self._project_rank(item)[0] < 2]
        return min(eligible, key=self._project_rank) if eligible else None

    def selected_project_pages(self) -> list[dict]:
        signature = self.project_signature()
        if signature is None:
            return []
        return [
            payload
            for key, payload in self.pages.items()
            if key.signature == signature
        ]

    def project_chain(self) -> list[dict] | None:
        signature = self.project_signature()
        if signature is None:
            return None
        if PageKey(signature, None) in self.pages:
            root = None
        elif PageKey(signature, "0") in self.pages:
            # The observed endpoint uses cursor=0 as its explicit first page.
            # Other opaque cursors are never promoted to roots.
            root = "0"
        else:
            return None
        chain = self._chain_for(signature, root)
        return chain if chain is not None and not self.pending else None

    def chain(self) -> list[dict] | None:
        if self.errors:
            self.fail(self.errors[0])
        if self.kind == "project":
            return self.project_chain()
        signature = self._default_signature()
        initial = [
            key.signature
            for key in self.pages
            if key.cursor is None and key.signature.scope == "conversation"
        ]
        if initial:
            signature = initial[0]
        chain = self._chain_for(signature, None)
        return chain if chain is not None and not self.pending else None

    def debug_state(self) -> dict[str, Any]:
        scopes = Counter(record.signature.scope for record in self.requests.values())
        selected = self.project_signature() if self.kind == "project" else None
        variants = []
        if self.kind == "project":
            for signature in sorted(
                {
                    record.signature
                    for record in self.requests.values()
                    if record.signature.scope == "project"
                },
                key=lambda item: repr(item.filters),
            ):
                cursors = [
                    key.cursor
                    for key in self.pages
                    if key.signature == signature
                ]
                variants.append(
                    {
                        "filters": dict(signature.filters),
                        "pages": len(cursors),
                        "has_initial": None in cursors or "0" in cursors,
                    }
                )
        return {
            "requests": len(self.requests),
            "responses": self.parsed_responses,
            "project_pages": len(self.pages),
            "auxiliary_pages": len(self.auxiliary_pages),
            "pending": len(self.pending),
            "scopes": dict(scopes),
            "selected_filters": dict(selected.filters) if selected else None,
            "project_variants": variants,
            "auxiliary_errors": list(self.auxiliary_errors[-3:]),
            "verification_wait": getattr(self, "verification_wait", None),
        }

    def nodes(self, payload: dict) -> dict[str, dict]:
        mapping = payload.get("mapping")
        messages = payload.get("messages")
        if isinstance(messages, list):
            nodes = {}
            parent = None
            for message in messages:
                if not isinstance(message, dict):
                    self.fail("Malformed conversation message list entry.")
                identifier = message.get("id")
                if not isinstance(identifier, str) or not identifier:
                    self.fail("Conversation message list entry lacks an ID.")
                if identifier in nodes:
                    self.fail("Conversation message list contains a duplicate ID.")
                if not isinstance(message.get("author"), dict) or not isinstance(
                    message.get("content"), dict
                ):
                    self.fail("Message lacks author or content.")
                nodes[identifier] = {
                    "id": identifier,
                    "message": message,
                    "parent": parent,
                    "children": [],
                }
                if parent is not None:
                    nodes[parent]["children"].append(identifier)
                parent = identifier
            return nodes
        if not isinstance(mapping, dict):
            self.fail(
                "Conversation response lacks a message mapping or message list; "
                "schema requires review."
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
        uses_message_lists = [isinstance(page.get("messages"), list) for page in pages]
        if any(uses_message_lists):
            if not all(uses_message_lists):
                self.fail("Conversation pages mix mapping and message-list schemas.")
            ordered: list[dict] = []
            messages_by_id: dict[str, dict] = {}
            previous = None
            # The initial page is newest. Following before cursors move toward
            # the past, while each response's list is chronological.
            for page in reversed(pages):
                for identifier, source_node in self.nodes(page).items():
                    message = source_node["message"]
                    old = messages_by_id.get(identifier)
                    if old is not None:
                        if old != message:
                            self.fail("Overlapping pages disagree about a message.")
                        continue
                    node = {
                        "id": identifier,
                        "message": message,
                        "parent": previous,
                        "children": [],
                    }
                    if ordered:
                        ordered[-1]["children"].append(identifier)
                    ordered.append(node)
                    messages_by_id[identifier] = message
                    previous = identifier
            return ordered
        nodes: dict[str, dict] = {}
        for page in pages:
            for identifier, node in self.nodes(page).items():
                old = nodes.get(identifier)
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
