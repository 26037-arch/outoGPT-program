"""Read-only project network schema probe using the existing Chrome setup.

The report omits headers, cookies, message bodies, titles, IDs, and raw cursors.
It records request parameters, response shapes, item counts, project membership,
and hashed cursor relationships needed to diagnose UI pagination.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cli_gpt.browser import BrowserSession
from cli_gpt.config import load_project_url
from cli_gpt.project import extract_project_id


def _token(value):
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _shape(value, depth=0):
    if depth >= 4:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(key): _shape(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return {"type": "list", "length": len(value), "item": _shape(value[0], depth + 1) if value else None}
    return type(value).__name__


def _page_candidates(value, project_id, pointer="$"):
    found = []
    if isinstance(value, dict):
        if isinstance(value.get("items"), list) and "cursor" in value:
            items = value["items"]
            memberships = {}
            for item in items:
                if not isinstance(item, dict):
                    key = "non-object"
                else:
                    membership = item.get("gizmo_id", item.get("project_id"))
                    key = "current" if membership == project_id else "none" if membership is None else "other"
                memberships[key] = memberships.get(key, 0) + 1
            found.append({
                "pointer": pointer,
                "item_count": len(items),
                "membership": memberships,
                "next_cursor": _token(value.get("cursor")),
                "item_keys": sorted(items[0]) if items and isinstance(items[0], dict) else [],
            })
        for key, item in value.items():
            found.extend(_page_candidates(item, project_id, f"{pointer}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_page_candidates(item, project_id, f"{pointer}[{index}]"))
    return found


report = {"kind": "project-network-details", "requests": [], "error": None}

try:
    project_url = load_project_url()
    project_id = extract_project_id(project_url)
    with BrowserSession() as browser:
        page = browser.new_page()
        session = page.context.new_cdp_session(page)
        tracked = {}
        finished = []

        def request(event):
            request_value = event.get("request", {})
            parts = urlsplit(request_value.get("url", ""))
            relevant = (
                parts.path == "/backend-api/conversations"
                or parts.path == "/backend-api/gizmos/bootstrap"
                or parts.path == f"/backend-api/gizmos/{project_id}"
                or parts.path == f"/backend-api/gizmos/{project_id}/conversations"
            )
            if not relevant:
                return
            query = parse_qs(parts.query, keep_blank_values=True)
            descriptor = {
                "request_id": event["requestId"],
                "path": parts.path,
                "parameters": {
                    key: [_token(item) if key in {"cursor", "before"} else item for item in values]
                    for key, values in sorted(query.items())
                },
                "status": None,
                "body": None,
            }
            tracked[event["requestId"]] = descriptor
            report["requests"].append(descriptor)

        def response(event):
            descriptor = tracked.get(event.get("requestId"))
            if descriptor is not None:
                descriptor["status"] = event.get("response", {}).get("status")

        def complete(event):
            request_id = event.get("requestId")
            if request_id in tracked:
                finished.append(request_id)

        session.on("Network.requestWillBeSent", request)
        session.on("Network.responseReceived", response)
        session.on("Network.loadingFinished", complete)
        session.send("Network.enable", {"maxTotalBufferSize": 100_000_000, "maxResourceBufferSize": 50_000_000})
        page.goto(project_url, wait_until="domcontentloaded", timeout=60_000)
        for _ in range(60):
            page.wait_for_timeout(250)
            while finished:
                request_id = finished.pop(0)
                descriptor = tracked[request_id]
                try:
                    raw = session.send("Network.getResponseBody", {"requestId": request_id})
                    contents = raw["body"]
                    if raw.get("base64Encoded"):
                        contents = base64.b64decode(contents).decode("utf-8")
                    payload = json.loads(contents)
                    descriptor["body"] = {
                        "shape": _shape(payload),
                        "page_candidates": _page_candidates(payload, project_id),
                    }
                except Exception as error:
                    descriptor["body_error"] = f"{type(error).__name__}: {error}"
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        report["page_origin"] = urlsplit(page.url).netloc
        report["page_path"] = urlsplit(page.url).path
        session.detach()
        page.close()
except BaseException as error:
    report["error"] = f"{type(error).__name__}: {error}"

Path("outputs").mkdir(exist_ok=True)
Path("outputs/project-network-details.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps({"request_count": len(report["requests"]), "error": report["error"]}))
