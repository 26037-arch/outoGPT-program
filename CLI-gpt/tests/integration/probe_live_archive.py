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
    "conflicts": [],
    "error": None,
}


class Probe(PaginationEvidence):
    def request(self, event):
        from urllib.parse import urlsplit, parse_qs

        url = event.get("request", {}).get("url", "")
        parts = urlsplit(url)

        if "/backend-api/" in url and (
            "conversation" in url or "gizmos/" in url
        ):
            report["requests"].append(parts.path)

        # 기존 요청 추적 유지
        super().request(event)

        rid = str(event.get("requestId", ""))
        if rid not in self.requests:
            return

        params = parse_qs(parts.query, keep_blank_values=True)
        record = self.requests[rid]

        # 민감할 수 있는 원본 cursor는 출력하지 않음
        report.setdefault("matched_requests", []).append(
            {
                "scope": record.signature.scope,
                "has_cursor": record.cursor is not None,
                "page_size": record.page_size,
                "limit": params.get("limit", [None])[0],
                "offset": params.get("offset", [None])[0],
                "parameter_names": sorted(params.keys()),
                "sort": params.get("sort", [None])[0],
                "order": params.get("order", [None])[0],
            }
        )

    def accept_record(self, record, payload):
        # 동일 cursor라도 요청 조건이 다른 응답은 별도 variant로 기록한다.
        variants = [
            (key, old)
            for key, old in self.pages.items()
            if key.cursor == record.cursor and key.signature != record.signature
        ]

        for key, old in variants:
            old_items = old.get("items", [])
            new_items = payload.get("items", [])

            old_ids = [item.get("id") for item in old_items]
            new_ids = [item.get("id") for item in new_items]

            report["conflicts"].append(
                {
                    "same_request_conditions": False,
                    "initial_page": record.cursor in {None, "0"},
                    "old_filters": dict(key.signature.filters),
                    "new_filters": dict(record.signature.filters),
                    "old_count": len(old_ids),
                    "new_count": len(new_ids),
                    "same_ids_in_order": old_ids == new_ids,
                    "same_id_set": set(old_ids) == set(new_ids),
                    "same_next_cursor": old.get("cursor") == payload.get("cursor"),
                    "added_count": len(set(new_ids) - set(old_ids)),
                    "removed_count": len(set(old_ids) - set(new_ids)),
                    "old_keys": sorted(old.keys()),
                    "new_keys": sorted(payload.keys()),
                }
            )

        # 3. 기존 응답 기록 유지
        report["responses"].append({
            "keys": list(payload),
                "scope": record.signature.scope,
                "has_next_cursor": payload.get("cursor") is not None,
                "filters": dict(record.signature.filters),
            "item_keys": list(payload.get("items", [{}])[0])
            if payload.get("items")
            else [],
        })

        # 동일 요청 조건의 충돌 및 프로젝트 소속 검증은 본 구현에 위임한다.
        super().accept_record(record, payload)


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
            report["chat_count"] = len(
                {
                    item["id"]
                    for payload in e.selected_project_pages()
                    for item in payload["items"]
                }
            )
            report["debug_state"] = e.debug_state()
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
