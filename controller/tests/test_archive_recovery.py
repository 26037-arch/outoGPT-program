"""Fault injection and recovery tests; all files use temporary directories."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cli_gpt.errors import ConversationLoadingUnknown
from cli_gpt.project import QAPair
from outogpt_controller.project_archive import ProjectArchive
from outogpt_controller.project_updater import ProjectUpdater
from test_project_updater import FakeBrowser, PROJECT_URL, conversation


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self):
        return ProjectArchive.open(self.root, "g-p-project", "Project", PROJECT_URL)

    def update(self, browser, **kwargs):
        return ProjectUpdater(browser, self.root, **kwargs).update(PROJECT_URL)

    def test_same_qa_count_changed_content_is_preserved_and_idempotent(self):
        self.update(FakeBrowser({"a": conversation("a", 1)}))
        archive = self.archive()
        before = archive.chat_path("a").read_bytes()
        edited = replace(
            conversation("a", 1), qa_pairs=(QAPair("edited question", "edited answer"),)
        )
        result = self.update(FakeBrowser({"a": edited}))
        self.assertTrue(result.ok)
        after = archive.chat_path("a").read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertIn(b"edited answer", after)
        self.assertEqual(result.updated_chats, 1)
        self.assertTrue(self.update(FakeBrowser({"a": edited})).ok)
        self.assertEqual(archive.chat_path("a").read_bytes(), after)

    def test_content_corruption_with_unchanged_qa_count_pauses(self):
        self.update(FakeBrowser({"a": conversation("a", 1)}))
        path = self.archive().chat_path("a")
        corrupted = path.read_bytes().replace(b"answer 1", b"broken 1")
        path.write_bytes(corrupted)
        browser = FakeBrowser({"a": conversation("a", 1), "b": conversation("b", 1)})
        result = self.update(browser)
        self.assertTrue(result.paused)
        self.assertEqual(browser.read_ids, ["a"])
        self.assertEqual(path.read_bytes(), corrupted)

    def test_temporary_loading_retries_without_visiting_next_chat(self):
        browser = FakeBrowser({"a": conversation("a", 1), "b": conversation("b", 1)})
        calls = []

        def read(chat):
            calls.append(chat.chat_id)
            if len(calls) < 3:
                raise ConversationLoadingUnknown("slow hydration")
            return browser.snapshots[chat.chat_id]

        browser.read_project_chat = read
        self.assertTrue(self.update(browser).ok)
        self.assertEqual(calls, ["a", "a", "a", "b"])

    def test_save_failure_retries_cached_snapshot_without_navigation(self):
        browser = FakeBrowser({"a": conversation("a", 1), "b": conversation("b", 1)})
        original = ProjectArchive.sync_snapshot
        attempts = []

        def save(archive, snapshot):
            attempts.append(snapshot.chat_id)
            if len(attempts) < 3:
                raise OSError("temporarily locked")
            return original(archive, snapshot)

        with patch.object(ProjectArchive, "sync_snapshot", save):
            self.assertTrue(self.update(browser).ok)
        self.assertEqual(attempts, ["a", "a", "a", "b"])
        self.assertEqual(browser.read_ids, ["a", "b"])

    def test_permanent_save_failure_does_not_register_or_read_next(self):
        browser = FakeBrowser({"a": conversation("a", 1), "b": conversation("b", 1)})
        with patch.object(
            ProjectArchive, "sync_snapshot", side_effect=OSError("disk full")
        ):
            result = self.update(browser)
        self.assertTrue(result.paused)
        self.assertEqual(browser.read_ids, ["a"])
        archive = self.archive()
        self.assertEqual(archive.state.chats, {})
        self.assertEqual(archive.load_progress()["stage"], "persistence")

    def test_state_failure_after_save_is_adopted_without_duplicate_revision(self):
        browser = FakeBrowser({"a": conversation("a", 1)})
        save_state = ProjectArchive.save_state

        def fail_completed_state(archive):
            if archive.state.chats:
                raise OSError("state write failed")
            return save_state(archive)

        with patch.object(ProjectArchive, "save_state", fail_completed_state):
            result = self.update(browser)
        self.assertTrue(result.paused)
        archive = self.archive()
        before = archive.chat_path("a").read_bytes()
        self.assertEqual(archive.state.chats, {})
        self.assertTrue(self.update(FakeBrowser({"a": conversation("a", 1)})).ok)
        self.assertEqual(archive.chat_path("a").read_bytes(), before)
        self.assertIn("a", self.archive().state.chats)

    def test_resume_visits_pending_first_even_if_discovery_order_changed(self):
        browser = FakeBrowser(
            {
                "a": conversation("a", 1),
                "b": ConversationLoadingUnknown("delayed"),
                "c": conversation("c", 1),
            }
        )
        self.assertTrue(self.update(browser).paused)
        resumed = FakeBrowser(
            {
                "c": conversation("c", 1),
                "a": conversation("a", 1),
                "b": conversation("b", 1),
            }
        )
        self.assertTrue(self.update(resumed).ok)
        self.assertEqual(resumed.read_ids[0], "b")
        self.assertEqual(self.archive().load_progress()["status"], "complete")

    def test_keyboard_interrupt_keeps_pending_chat(self):
        browser = FakeBrowser({"a": conversation("a", 1), "b": conversation("b", 1)})
        browser.read_project_chat = lambda chat: (_ for _ in ()).throw(
            KeyboardInterrupt()
        )
        self.assertTrue(self.update(browser).paused)
        self.assertEqual(self.archive().load_progress()["pending_chat_id"], "a")

    def test_unanswered_and_hidden_messages_survive_in_same_md(self):
        snapshot = replace(
            conversation("a", 0),
            messages=(
                {
                    "id": "u",
                    "role": "user",
                    "markdown": "unanswered question",
                    "source": {"content": "original"},
                },
            ),
            non_ui_messages=(
                {
                    "id": "system",
                    "reason": "system",
                    "source": {"content": "hidden content"},
                },
            ),
        )
        self.assertTrue(self.update(FakeBrowser({"a": snapshot})).ok)
        body = self.archive().chat_path("a").read_text(encoding="utf-8")
        self.assertIn("unanswered question", body)
        self.assertIn("hidden content", body)
        self.assertEqual(self.archive().state.chats["a"].qa_count, 0)

    def test_legacy_markdown_and_unmarked_tail_are_never_deleted(self):
        archive = self.archive()
        archive.create_chat(
            "a", "Legacy", conversation("a", 1).chat_url, [QAPair("old", "preserve")]
        )
        path = archive.chat_path("a")
        before = path.read_bytes() + b"\nUnmarked valuable text"
        path.write_bytes(before)
        self.assertTrue(self.update(FakeBrowser({"a": conversation("a", 1)})).ok)
        self.assertTrue(path.read_bytes().startswith(before))

    def test_marker_like_user_content_is_length_framed(self):
        payload = (
            "<!-- outogpt-snapshot:"
            + "a" * 64
            + ":0 -->\n<!-- outogpt-snapshot-end -->"
        )
        snapshot = replace(
            conversation("a", 1), qa_pairs=(QAPair(payload, "```answer```"),)
        )
        self.assertTrue(self.update(FakeBrowser({"a": snapshot})).ok)
        before = self.archive().chat_path("a").read_bytes()
        self.assertTrue(self.update(FakeBrowser({"a": snapshot})).ok)
        self.assertEqual(self.archive().chat_path("a").read_bytes(), before)

    def test_final_file_read_failure_never_marks_complete(self):
        original_read = Path.read_text

        def fail_md_read(path, *args, **kwargs):
            if path.name == "a.md":
                raise OSError("committed file unreadable")
            return original_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", fail_md_read):
            result = self.update(FakeBrowser({"a": conversation("a", 1)}))
        self.assertTrue(result.paused)
        self.assertEqual(self.archive().state.chats, {})
