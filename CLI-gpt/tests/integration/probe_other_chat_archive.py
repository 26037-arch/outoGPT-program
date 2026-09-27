"""Validate a non-first project chat through scrolling and Markdown rendering.

The output contains counts, route evidence, scroll state, and content digests only.
It never records message text, titles, cookies, headers, or response bodies.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from cli_gpt.browser import BrowserSession
from cli_gpt.config import load_project_url
from cli_gpt.project import discover_project_chats, read_conversation
from outogpt_controller.project_archive import ProjectArchive


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_PATH = REPOSITORY_ROOT / "outputs" / "other-chat-validation.json"


def main() -> int:
    report: dict[str, object] = {
        "kind": "other-chat-archive-validation",
        "ok": False,
        "target_index": 1,
    }
    progress_events: list[tuple[str, dict]] = []

    def progress(stage, details):
        progress_events.append((stage, dict(details)))

    try:
        project_url = load_project_url()
        with BrowserSession() as browser:
            discovery = discover_project_chats(
                browser.new_page(), project_url, progress=progress
            )
            if not discovery.complete or len(discovery.chats) < 2:
                raise RuntimeError(
                    "A complete project list with at least two chats is required."
                )

            target = discovery.chats[1]
            snapshot = read_conversation(
                browser.new_page(), target, progress=progress, stall_rounds=15
            )
            rendered = ProjectArchive._snapshot_body(snapshot)
            encoded = rendered.encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT / "outputs") as temp:
                candidate = Path(temp) / "candidate.md"
                candidate.write_bytes(encoded)
                committed = candidate.read_bytes()
                if committed != encoded:
                    raise RuntimeError("Temporary Markdown read-back differed.")

            completed = next(
                details
                for stage, details in reversed(progress_events)
                if stage == "conversation_complete"
            )
            scroll = completed.get("scroll") or {}
            visible_ids = [message["id"] for message in snapshot.messages]
            if len(visible_ids) != len(set(visible_ids)):
                raise RuntimeError("Rendered visible message UUIDs were not unique.")
            if any(message["markdown"] not in rendered for message in snapshot.messages):
                raise RuntimeError("A verified message was absent from rendered Markdown.")

            report.update(
                {
                    "ok": True,
                    "project_chat_count": len(discovery.chats),
                    "target_chat_id_sha256": hashlib.sha256(
                        target.chat_id.encode("utf-8")
                    ).hexdigest(),
                    "qa_pairs": len(snapshot.qa_pairs),
                    "visible_messages": len(snapshot.messages),
                    "hidden_messages": len(snapshot.non_ui_messages),
                    "markdown_bytes": len(encoded),
                    "markdown_sha256": digest,
                    "markdown_readback_sha256": hashlib.sha256(committed).hexdigest(),
                    "network_pages": completed.get("project_pages"),
                    "network_terminal_chain_verified": (
                        completed.get("pending") == 0
                        and completed.get("verification_wait") is None
                    ),
                    "completion_round": completed.get("round"),
                    "completion_pages": completed.get("pages"),
                    "completion_pending": completed.get("pending"),
                    "completion_verification_wait": completed.get(
                        "verification_wait"
                    ),
                    "scroll": scroll,
                }
            )
    except BaseException as error:
        report["error"] = {
            "type": type(error).__name__,
            "message": str(error).strip() or type(error).__name__,
        }
        if progress_events:
            stage, details = progress_events[-1]
            report["last_progress"] = {"stage": stage, "details": details}

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
