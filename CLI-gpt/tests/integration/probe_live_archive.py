"""Read-only live schema probe. Run from the repository root; uses existing setup.

Reports paths and schema keys, never headers, cookies, query strings or message text.
"""

import json
from pathlib import Path

from cli_gpt.browser import BrowserSession
from cli_gpt.config import load_project_url
from cli_gpt.pagination import PaginationEvidence
from cli_gpt.project import extract_project_id

report = {
    "kind": "live_chrome_schema_probe",
    "requests": [],
    "responses": [],
    "error": None,
}


class Probe(PaginationEvidence):
    def request(self, event):
        url = event.get("request", {}).get("url", "")
        if "/backend-api/" in url and ("conversation" in url or "gizmos/" in url):
            from urllib.parse import urlsplit

            report["requests"].append(urlsplit(url).path)
        super().request(event)

    def accept(self, cursor, payload):
        report["responses"].append(
            {
                "keys": list(payload),
                "cursor": payload.get("cursor"),
                "item_keys": list(payload.get("items", [{}])[0])
                if payload.get("items")
                else [],
            }
        )
        super().accept(cursor, payload)


try:
    with BrowserSession() as browser:
        page = browser.new_page()
        url = load_project_url()
        with Probe("project", extract_project_id(url)).start(page) as e:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for _ in range(30):
                page.wait_for_timeout(500)
                e.drain()
            report["page_title"] = page.title()
            from urllib.parse import urlsplit

            report["page_origin"] = urlsplit(page.url).netloc
            report["page_path"] = urlsplit(page.url).path
            from cli_gpt.selectors import login_or_challenge_visible

            report["login_or_challenge_visible"] = login_or_challenge_visible(page)
            report["connected_pages"] = len(e.chain() or [])
            report["chat_count"] = sum(len(p["items"]) for p in e.pages.values())
        page.close()
except Exception as error:
    report["error"] = str(error)
Path("outputs").mkdir(exist_ok=True)
Path("outputs/live-schema-probe.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(
    json.dumps({"response_count": len(report["responses"]), "error": report["error"]})
)
