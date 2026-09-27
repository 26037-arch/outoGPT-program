"""Read-only probe for the current project conversation route.

The report records URL path shapes and conversation API paths only. It omits
cookies, headers, titles, response bodies, and raw message contents.
"""

from __future__ import annotations

import json
import re
import base64
import difflib
from pathlib import Path
from urllib.parse import urlsplit

from cli_gpt.browser import BrowserSession
from cli_gpt.config import load_archive_root, load_project_url
from cli_gpt.project import (
    ProjectChat,
    _conversation_sample,
    _normalize_conversation_messages,
    discover_project_chats,
    extract_project_id,
    project_chat_url,
)


def _redact(path: str, project_id: str) -> str:
    path = path.replace(project_id, "{project_id}")
    return re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "{conversation_id}",
        path,
        flags=re.IGNORECASE,
    )


report = {"kind": "project-route-probe", "links": [], "navigation": None, "error": None}


def _shape(value, depth=0):
    if depth >= 5:
        return type(value).__name__
    if isinstance(value, dict):
        if len(value) > 30:
            first = next(iter(value.values()), None)
            return {"type": "dict", "length": len(value), "item": _shape(first, depth + 1)}
        return {str(key): _shape(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "item": _shape(value[0], depth + 1) if value else None,
        }
    return type(value).__name__

try:
    project_url = load_project_url()
    project_id = extract_project_id(project_url)
    with BrowserSession() as browser:
        page = browser.new_page()
        discovery = discover_project_chats(page, project_url)
        report["discovery"] = {
            "chat_count": len(discovery.chats),
            "complete": discovery.complete,
            "diagnostic": discovery.diagnostic,
        }
        links = page.locator('a[href*="/c/"]').evaluate_all(
            """nodes => nodes.map(node => ({
              href: node.href,
              visible: !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length)
            }))"""
        )
        for item in links:
            parts = urlsplit(item["href"])
            report["links"].append(
                {"path": _redact(parts.path, project_id), "visible": item["visible"]}
            )
        chats = list(discovery.chats)
        if not chats:
            archive_root = load_archive_root()
            for checkpoint in (
                sorted(archive_root.glob(f"{project_id}*/update-progress.json"))
                if archive_root
                else []
            ):
                state = json.loads(checkpoint.read_text(encoding="utf-8"))
                chat_id = state.get("pending_chat_id")
                if isinstance(chat_id, str) and chat_id:
                    chats.append(
                        ProjectChat(chat_id, project_chat_url(project_url, chat_id), "")
                    )
                    report["discovery"]["checkpoint_fallback"] = True
                    break
        if chats:
            target = chats[0].chat_url
            target_page = browser.new_page()
            session = target_page.context.new_cdp_session(target_page)
            tracked = {}
            finished = []
            network_message_ids = set()
            network_messages = {}

            def request(event):
                value = event.get("request", {})
                parts = urlsplit(value.get("url", ""))
                if "/backend-api/conversation" not in parts.path:
                    return
                descriptor = {
                    "request_id": event.get("requestId"),
                    "method": value.get("method"),
                    "path": _redact(parts.path, project_id),
                    "post_shape": None,
                    "status": None,
                    "response_shape": None,
                }
                post_data = value.get("postData")
                if post_data:
                    try:
                        descriptor["post_shape"] = _shape(json.loads(post_data))
                    except (json.JSONDecodeError, TypeError):
                        descriptor["post_shape"] = "non-json"
                tracked[event.get("requestId")] = descriptor

            def response(event):
                descriptor = tracked.get(event.get("requestId"))
                if descriptor is not None:
                    descriptor["status"] = event.get("response", {}).get("status")

            def complete(event):
                if event.get("requestId") in tracked:
                    finished.append(event.get("requestId"))

            session.on("Network.requestWillBeSent", request)
            session.on("Network.responseReceived", response)
            session.on("Network.loadingFinished", complete)
            session.send("Network.enable", {"maxTotalBufferSize": 100_000_000})
            session.send("Network.setCacheDisabled", {"cacheDisabled": True})
            target_page.goto(target, wait_until="domcontentloaded", timeout=60_000)
            for _ in range(30):
                target_page.wait_for_timeout(250)
                while finished:
                    request_id = finished.pop(0)
                    descriptor = tracked[request_id]
                    try:
                        raw = session.send(
                            "Network.getResponseBody", {"requestId": request_id}
                        )
                        contents = raw["body"]
                        if raw.get("base64Encoded"):
                            contents = base64.b64decode(contents).decode("utf-8")
                        payload = json.loads(contents)
                        descriptor["response_shape"] = _shape(payload)
                        candidates = payload if isinstance(payload, list) else [payload]
                        conversations = [
                            item
                            for item in candidates
                            if isinstance(item, dict)
                            and isinstance(item.get("page_info"), dict)
                        ]
                        descriptor["conversation_pages"] = []
                        for item in conversations:
                            messages = item.get("messages")
                            mapping = item.get("mapping")
                            ids = (
                                [message.get("id") for message in messages]
                                if isinstance(messages, list)
                                else list(mapping) if isinstance(mapping, dict) else []
                            )
                            network_message_ids.update(
                                identifier for identifier in ids if isinstance(identifier, str)
                            )
                            message_values = (
                                messages
                                if isinstance(messages, list)
                                else [
                                    node.get("message")
                                    for node in mapping.values()
                                    if isinstance(node, dict)
                                ]
                                if isinstance(mapping, dict)
                                else []
                            )
                            for message in message_values:
                                if isinstance(message, dict) and isinstance(
                                    message.get("id"), str
                                ):
                                    network_messages[message["id"]] = message
                            info = item["page_info"]
                            descriptor["conversation_pages"].append(
                                {
                                    "message_count": len(ids),
                                    "has_previous_page": info.get("has_previous_page"),
                                    "has_start_cursor": bool(info.get("start_cursor")),
                                }
                            )
                    except Exception as error:
                        descriptor["body_error"] = type(error).__name__
            dom_values = target_page.evaluate(
                """() => Object.fromEntries([
                  'data-turn-key',
                  'data-conversation-role',
                  'data-chatgpt-selection-message-id',
                  'data-sidebar-chatgpt-conversation-key'
                ].map(name => [name, [...document.querySelectorAll(`[${name}]`)]
                  .map(node => node.getAttribute(name)).filter(Boolean)]))"""
            )
            turn_summary = target_page.evaluate(
                """() => [...document.querySelectorAll('[data-turn-key]')].map(turn => ({
                  attributes: turn.getAttributeNames(),
                  conversationRoles: [...new Set([...turn.querySelectorAll('[data-conversation-role]')]
                    .map(node => node.getAttribute('data-conversation-role')))],
                  userBubbles: turn.querySelectorAll('[data-user-message-bubble]').length,
                  agentStarts: turn.querySelectorAll('[data-chatgpt-agent-turn-start]').length,
                  selectionIds: turn.querySelectorAll('[data-chatgpt-selection-message-id]').length,
                  virtualizedContents: turn.querySelectorAll('[data-virtualized-turn-content]').length,
                  markdownNodes: turn.querySelectorAll('.markdown').length,
                  textLength: String(turn.innerText || '').length
                }))"""
            )
            rendered_messages = _normalize_conversation_messages(
                _conversation_sample(target_page).get("messages") or ()
            )
            comparisons = []
            for rendered in rendered_messages:
                message = network_messages.get(rendered.get("messageId"))
                if not message:
                    continue
                parts = (message.get("content") or {}).get("parts") or []
                source = "\n\n".join(part for part in parts if isinstance(part, str))
                shown = rendered.get("markdown") or ""
                def canonical(value):
                    return re.sub(r"[\s`*_#>|\\]+", "", value)

                source_value = canonical(source)
                shown_value = canonical(shown)
                if source_value != shown_value:
                    def semantic(value):
                        return "".join(
                            character.casefold()
                            for character in value
                            if character.isalnum()
                        )

                    source_semantic = semantic(source)
                    shown_semantic = semantic(shown)
                    matcher = difflib.SequenceMatcher(
                        None, source_semantic, shown_semantic, autojunk=False
                    )
                    matched = sum(block.size for block in matcher.get_matching_blocks())
                    fast_matcher = difflib.SequenceMatcher(
                        None, source_semantic, shown_semantic
                    )
                    fast_matched = sum(
                        block.size for block in fast_matcher.get_matching_blocks()
                    )
                    source_tokens = re.findall(r"[^\W_]+", source.casefold())
                    shown_tokens = re.findall(r"[^\W_]+", shown.casefold())
                    source_index = 0
                    token_matches = 0
                    for token in shown_tokens:
                        while (
                            source_index < len(source_tokens)
                            and source_tokens[source_index] != token
                        ):
                            source_index += 1
                        if source_index >= len(source_tokens):
                            break
                        token_matches += 1
                        source_index += 1
                    token_matcher = difflib.SequenceMatcher(
                        None, source_tokens, shown_tokens, autojunk=False
                    )
                    token_block_matches = sum(
                        block.size for block in token_matcher.get_matching_blocks()
                    )
                    comparisons.append(
                        {
                            "role": (message.get("author") or {}).get("role"),
                            "channel": message.get("channel"),
                            "content_type": (message.get("content") or {}).get(
                                "content_type"
                            ),
                            "parts": len(parts),
                            "source_chars": len(source_value),
                            "rendered_chars": len(shown_value),
                            "source_in_rendered": source_value in shown_value,
                            "rendered_in_source": shown_value in source_value,
                            "semantic_equal": source_semantic == shown_semantic,
                            "source_semantic_chars": len(source_semantic),
                            "rendered_semantic_chars": len(shown_semantic),
                            "source_semantic_in_rendered": source_semantic
                            in shown_semantic,
                            "rendered_semantic_in_source": shown_semantic
                            in source_semantic,
                            "sequence_ratio": round(matcher.ratio(), 6),
                            "source_coverage": round(
                                matched / max(1, len(source_semantic)), 6
                            ),
                            "rendered_coverage": round(
                                matched / max(1, len(shown_semantic)), 6
                            ),
                            "fast_source_coverage": round(
                                fast_matched / max(1, len(source_semantic)), 6
                            ),
                            "fast_rendered_coverage": round(
                                fast_matched / max(1, len(shown_semantic)), 6
                            ),
                            "source_token_coverage": round(
                                token_matches / max(1, len(source_tokens)), 6
                            ),
                            "rendered_token_coverage": round(
                                token_matches / max(1, len(shown_tokens)), 6
                            ),
                            "source_token_block_coverage": round(
                                token_block_matches / max(1, len(source_tokens)), 6
                            ),
                            "rendered_token_block_coverage": round(
                                token_block_matches / max(1, len(shown_tokens)), 6
                            ),
                        }
                    )
            scroll_summary = target_page.evaluate(
                """() => {
                  const turn = document.querySelector('[data-turn-key]');
                  const values = [];
                  for (let node = turn; node; node = node.parentElement) {
                    const style = getComputedStyle(node);
                    if (node.scrollHeight > node.clientHeight + 1
                        || ['auto', 'scroll', 'overlay'].includes(style.overflowY)) {
                      values.push({
                        tag: node.tagName,
                        attributes: node.getAttributeNames(),
                        overflowY: style.overflowY,
                        flexDirection: style.flexDirection,
                        direction: style.direction,
                        scrollTop: node.scrollTop,
                        scrollHeight: node.scrollHeight,
                        clientHeight: node.clientHeight
                      });
                    }
                  }
                  return values;
                }"""
            )
            report["navigation"] = {
                "target_path": _redact(urlsplit(target).path, project_id),
                "loaded_path": _redact(urlsplit(target_page.url).path, project_id),
                "conversation_requests": list(tracked.values()),
                "article_count": target_page.locator("article").count(),
                "message_id_count": target_page.locator("[data-message-id]").count(),
                "identity_attributes": {
                    name: {
                        "count": len(values),
                        "unique": len(set(values)),
                        "network_message_overlap": len(set(values) & network_message_ids),
                    }
                    for name, values in dom_values.items()
                },
                "conversation_roles": sorted(
                    set(dom_values.get("data-conversation-role", ()))
                ),
                "turn_summary": turn_summary,
                "visible_mismatches": comparisons,
                "scroll_summary": scroll_summary,
                "dom": target_page.evaluate(
                    """() => ({
                      textLength: String(document.body?.innerText || '').length,
                      mainCount: document.querySelectorAll('main').length,
                      iframeCount: document.querySelectorAll('iframe').length,
                      testids: [...new Set([...document.querySelectorAll('[data-testid]')]
                        .map(node => node.getAttribute('data-testid')))].slice(0, 200),
                      roles: [...new Set([...document.querySelectorAll('[data-message-author-role]')]
                        .map(node => node.getAttribute('data-message-author-role')))],
                      messageAttributes: [...new Set([...document.querySelectorAll('*')]
                        .flatMap(node => [...node.getAttributeNames()])
                        .filter(name => /message|turn|conversation/i.test(name)))],
                      classHints: [...new Set([...document.querySelectorAll('[class]')]
                        .flatMap(node => [...node.classList])
                        .filter(name => /message|turn|conversation/i.test(name)))].slice(0, 200)
                    })"""
                ),
            }
            session.detach()
            target_page.close()
        page.close()
except BaseException as error:
    report["error"] = f"{type(error).__name__}: {error}"

Path("outputs").mkdir(exist_ok=True)
Path("outputs/project-route-probe.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps({"links": len(report["links"]), "error": report["error"]}))
