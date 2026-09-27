"""Inspect non-text user message shapes in the second project chat.

Only schema keys, value types, and counts are written. Message contents, IDs,
titles, cursors, headers, and cookies are omitted.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from cli_gpt.browser import BrowserSession
from cli_gpt.config import load_project_url
from cli_gpt.project import _scroll_conversation_history_to_top, discover_project_chats
from cli_gpt.selectors import MESSAGE_ATTACHMENT_NODES


def value_shape(value):
    if isinstance(value, dict):
        return {"type": "dict", "keys": sorted(value)}
    if isinstance(value, list):
        return {"type": "list", "length": len(value)}
    return {"type": type(value).__name__}


report = {"kind": "other-chat-message-shapes", "samples": [], "error": None}
try:
    with BrowserSession() as browser:
        discovery = discover_project_chats(browser.new_page(), load_project_url())
        if not discovery.complete or len(discovery.chats) < 2:
            raise RuntimeError("A complete project with two chats is required.")
        page = browser.new_page()
        session = page.context.new_cdp_session(page)
        tracked = set()
        finished = []
        zero_user_hashes = set()
        dom_turns = {}

        def token(value):
            return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]

        def request(event):
            path = urlsplit(event.get("request", {}).get("url", "")).path
            if path.startswith("/backend-api/conversation"):
                tracked.add(event.get("requestId"))

        def complete(event):
            if event.get("requestId") in tracked:
                finished.append(event.get("requestId"))

        def drain():
            while finished:
                request_id = finished.pop(0)
                raw = session.send("Network.getResponseBody", {"requestId": request_id})
                body = raw["body"]
                if raw.get("base64Encoded"):
                    body = base64.b64decode(body).decode("utf-8")
                payload = json.loads(body)
                conversations = payload if isinstance(payload, list) else [payload]
                for conversation in conversations:
                    if not isinstance(conversation, dict):
                        continue
                    for message in conversation.get("messages") or []:
                        if not isinstance(message, dict):
                            continue
                        if (message.get("author") or {}).get("role") != "user":
                            continue
                        content = message.get("content") or {}
                        parts = content.get("parts")
                        text_chars = sum(
                            len(part) for part in parts or [] if isinstance(part, str)
                        )
                        if text_chars:
                            continue
                        metadata = message.get("metadata") or {}
                        report["samples"].append(
                            {
                                "message_id_sha256": token(message.get("id")),
                                "content_type": content.get("content_type"),
                                "content_keys": sorted(content),
                                "parts_shape": value_shape(parts),
                                "part_shapes": [value_shape(part) for part in parts or []],
                                "metadata_keys": sorted(metadata),
                                "attachments_shape": value_shape(
                                    metadata.get("attachments")
                                ),
                                "hidden": metadata.get(
                                    "is_visually_hidden_from_conversation"
                                ),
                                "recipient": message.get("recipient"),
                                "end_turn": message.get("end_turn"),
                            }
                        )
                        zero_user_hashes.add(token(message.get("id")))

        session.on("Network.requestWillBeSent", request)
        session.on("Network.loadingFinished", complete)
        session.send("Network.enable", {"maxTotalBufferSize": 100_000_000})
        session.send("Network.setCacheDisabled", {"cacheDisabled": True})
        page.goto(
            discovery.chats[1].chat_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        for _ in range(40):
            page.wait_for_timeout(250)
            drain()
        for _ in range(180):
            drain()
            values = page.evaluate(
                """attachmentSelectors => [...document.querySelectorAll('[data-turn-key]')]
                  .map(turn => {
                    const matches = [];
                    for (const selector of attachmentSelectors) {
                      try { matches.push(...turn.querySelectorAll(selector)); } catch (_) {}
                    }
                    return {
                      key: turn.getAttribute('data-turn-key'),
                      attributes: turn.getAttributeNames(),
                      userBubbles: turn.querySelectorAll('[data-user-message-bubble]').length,
                      selectionIds: turn.querySelectorAll('[data-chatgpt-selection-message-id]').length,
                      attachmentNodes: [...new Set(matches)].map(node => ({
                        tag: node.tagName.toLowerCase(),
                        attributes: node.getAttributeNames(),
                        testid: node.getAttribute('data-testid') || null
                      })),
                      textLength: String(turn.innerText || '').length
                    };
                  })""",
                list(MESSAGE_ATTACHMENT_NODES),
            )
            for value in values:
                key = token(value.pop("key"))
                dom_turns[key] = value
            scroll = _scroll_conversation_history_to_top(page)
            page.wait_for_timeout(100)
            if scroll.get("atTop") and scroll.get("wasAtTop"):
                drain()
                break
        report["network_zero_user_count"] = len(zero_user_hashes)
        report["dom_turn_count"] = len(dom_turns)
        report["zero_users_seen_as_dom_turns"] = sorted(
            zero_user_hashes & set(dom_turns)
        )
        report["matching_dom_profiles"] = [
            dom_turns[key] for key in sorted(zero_user_hashes & set(dom_turns))
        ]
        report["scroll"] = scroll
        session.detach()
except BaseException as error:
    report["error"] = f"{type(error).__name__}: {str(error).strip()}"

output = Path("outputs/other-chat-message-shapes.json")
output.parent.mkdir(exist_ok=True)
output.write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps(report, ensure_ascii=False))
