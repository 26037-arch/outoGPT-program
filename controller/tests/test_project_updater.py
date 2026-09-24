import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli_gpt.errors import PageStructureChanged
from cli_gpt.project import (
    ConversationSnapshot,
    ProjectChat,
    ProjectDiscovery,
    QAPair,
)
from outogpt_controller.errors import MarkdownArchiveError
from outogpt_controller.project_archive import ProjectArchive, sanitize_component
from outogpt_controller.project_updater import ProjectUpdater


PROJECT_URL = "https://chatgpt.com/g/g-p-project/project"


def conversation(chat_id, count, *, title=None, generating=False):
    return ConversationSnapshot(
        chat_id,
        f"https://chatgpt.com/g/g-p-project/c/{chat_id}",
        title or f"Chat {chat_id}",
        tuple(
            QAPair(f"question {index}", f"answer {index}")
            for index in range(1, count + 1)
        ),
        generating,
    )


class FakeBrowser:
    def __init__(self, snapshots, *, discovery_error=None, discovery_complete=True):
        self.snapshots = snapshots
        self.discovery_error = discovery_error
        self.discovery_complete = discovery_complete
        self.read_ids = []

    def discover_project_chats(self, project_url):
        if self.discovery_error:
            raise self.discovery_error
        chats = []
        for chat_id, snapshot in self.snapshots.items():
            if isinstance(snapshot, Exception):
                chats.append(
                    ProjectChat(
                        chat_id,
                        f"https://chatgpt.com/g/g-p-project/c/{chat_id}",
                        f"Chat {chat_id}",
                    )
                )
            else:
                chats.append(ProjectChat(chat_id, snapshot.chat_url, snapshot.title))
        return ProjectDiscovery(
            "g-p-project",
            project_url,
            "테스트 프로젝트",
            tuple(chats),
            self.discovery_complete,
            None if self.discovery_complete else "bounded traversal remained partial",
        )

    def read_project_chat(self, chat):
        self.read_ids.append(chat.chat_id)
        value = self.snapshots[chat.chat_id]
        if isinstance(value, Exception):
            raise value
        return value


class ProjectUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "ChatGPT"

    def tearDown(self):
        self.temporary.cleanup()

    def update(self, snapshots):
        return ProjectUpdater(FakeBrowser(snapshots), self.root).update(PROJECT_URL)

    def state(self):
        paths = list(self.root.glob("*/project.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text(encoding="utf-8")), paths[0].parent

    def test_first_sync_creates_per_chat_markdown_state_and_index(self):
        result = self.update({"a": conversation("a", 2)})
        self.assertTrue(result.ok)
        self.assertEqual((result.new_chats, result.qa_pairs_appended), (1, 2))
        state, directory = self.state()
        self.assertEqual(state["chats"]["a"]["qa_count"], 2)
        markdown = (directory / "chats" / "a.md").read_text(encoding="utf-8")
        self.assertIn("## Q1", markdown)
        self.assertIn("## A2", markdown)
        self.assertIn(
            "[Chat a](chats/a.md)", (directory / "index.md").read_text(encoding="utf-8")
        )

    def test_new_chat_is_added_without_rewriting_existing_chat(self):
        self.update({"a": conversation("a", 1)})
        _, directory = self.state()
        original = (directory / "chats" / "a.md").read_bytes()
        result = self.update({"a": conversation("a", 1), "b": conversation("b", 1)})
        self.assertEqual((result.new_chats, result.unchanged_chats), (1, 1))
        self.assertEqual((directory / "chats" / "a.md").read_bytes(), original)

    def test_no_new_qa_and_repeated_update_do_not_duplicate(self):
        self.update({"a": conversation("a", 2)})
        _, directory = self.state()
        path = directory / "chats" / "a.md"
        index_path = directory / "index.md"
        os.utime(index_path, (1, 1))
        original = path.read_text(encoding="utf-8")
        first = self.update({"a": conversation("a", 2)})
        second = self.update({"a": conversation("a", 2)})
        self.assertEqual(first.unchanged_chats, 1)
        self.assertEqual(second.qa_pairs_appended, 0)
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual(index_path.stat().st_mtime_ns, 1_000_000_000)

    def test_one_and_multiple_new_qa_pairs_are_appended_once(self):
        self.update({"a": conversation("a", 1)})
        one = self.update({"a": conversation("a", 2)})
        many = self.update({"a": conversation("a", 4)})
        self.assertEqual((one.updated_chats, one.qa_pairs_appended), (1, 1))
        self.assertEqual((many.updated_chats, many.qa_pairs_appended), (1, 2))
        state, directory = self.state()
        self.assertEqual(state["chats"]["a"]["qa_count"], 4)
        markdown = (directory / "chats" / "a.md").read_text(encoding="utf-8")
        self.assertEqual(
            markdown.count("## Q"), 7
        )  # 1, 2, and 4-pair immutable revisions

    def test_count_regression_preserves_old_bytes_as_revision(self):
        self.update({"a": conversation("a", 3)})
        _, directory = self.state()
        path = directory / "chats/a.md"
        before = path.read_bytes()
        result = self.update({"a": conversation("a", 2)})
        self.assertTrue(result.ok)
        self.assertEqual(result.updated_chats, 1)
        self.assertTrue(path.read_bytes().startswith(before))
        self.assertEqual(self.state()[0]["chats"]["a"]["qa_count"], 2)

    def test_generating_chat_pauses_before_next_chat(self):
        self.update({"a": conversation("a", 1), "b": conversation("b", 1)})
        browser = FakeBrowser(
            {"a": conversation("a", 2, generating=True), "b": conversation("b", 2)}
        )
        result = ProjectUpdater(browser, self.root).update(PROJECT_URL)
        self.assertTrue(result.paused)
        self.assertEqual(result.pending_chat_id, "a")
        self.assertEqual(browser.read_ids, ["a", "a", "a"])
        self.assertEqual(result.skipped_generating_chats, 0)
        self.assertEqual(self.state()[0]["chats"]["b"]["qa_count"], 1)

    def test_verified_empty_chat_is_saved_but_generating_chat_is_pending(self):
        result = self.update(
            {
                "empty": conversation("empty", 0),
                "active": conversation("active", 0, generating=True),
            }
        )
        state, directory = self.state()
        self.assertTrue(result.paused)
        self.assertIn("empty", state["chats"])
        self.assertNotIn("active", state["chats"])
        self.assertTrue((directory / "chats/empty.md").is_file())

    def test_state_advances_only_after_markdown_append_succeeds(self):
        self.update({"a": conversation("a", 1)})
        archive = ProjectArchive.open(
            self.root, "g-p-project", "테스트 프로젝트", PROJECT_URL
        )
        path = archive.chat_path("a")
        original = path.read_bytes()

        def fail_append(*args, **kwargs):
            raise MarkdownArchiveError("disk full")

        archive.sync_snapshot = fail_append
        updater = ProjectUpdater(
            FakeBrowser({"a": conversation("a", 2)}),
            self.root,
            archive_factory=lambda *args: archive,
        )
        result = updater.update(PROJECT_URL)
        state, _ = self.state()
        self.assertFalse(result.ok)
        self.assertEqual(state["chats"]["a"]["qa_count"], 1)
        self.assertEqual(path.read_bytes(), original)

    def test_saved_snapshot_is_recovered_if_state_write_was_interrupted(self):
        self.update({"a": conversation("a", 1)})
        archive = ProjectArchive.open(self.root, "g-p-project", "Project", PROJECT_URL)
        archive.sync_snapshot(conversation("a", 2))
        before = archive.chat_path("a").read_bytes()
        result = self.update({"a": conversation("a", 2)})
        self.assertTrue(result.ok)
        self.assertEqual(result.qa_pairs_appended, 0)
        self.assertEqual(self.state()[0]["chats"]["a"]["qa_count"], 2)
        self.assertEqual(archive.chat_path("a").read_bytes(), before)

    def test_atomic_replace_failure_preserves_prior_markdown_and_success_counters(self):
        self.update({"a": conversation("a", 1)})
        _, directory = self.state()
        path = directory / "chats" / "a.md"
        original = path.read_bytes()

        with patch(
            "outogpt_controller.project_archive.os.replace",
            side_effect=OSError("simulated replace failure"),
        ):
            result = self.update({"a": conversation("a", 2)})

        self.assertFalse(result.ok)
        self.assertEqual(result.updated_chats, 0)
        self.assertEqual(result.saved_chats, 0)
        self.assertEqual(result.qa_pairs_appended, 0)
        self.assertEqual(result.failed_chats, 1)
        self.assertEqual(path.read_bytes(), original)

    def test_partial_discovery_is_checkpointed_without_reading_chats(self):
        browser = FakeBrowser(
            {"good": conversation("good", 1)}, discovery_complete=False
        )
        result = ProjectUpdater(browser, self.root).update(PROJECT_URL)
        state, directory = self.state()
        self.assertTrue(result.paused)
        self.assertEqual(browser.read_ids, [])
        self.assertEqual(state["chats"], {})
        progress = json.loads(
            (directory / "update-progress.json").read_text(encoding="utf-8")
        )
        self.assertEqual(progress["stage"], "discovery")
        self.assertEqual(progress["discovered"][0]["chat_id"], "good")

    def test_unknown_loading_is_pending_not_failed_or_saved(self):
        unknown = ConversationSnapshot(
            "pending",
            "https://chatgpt.com/g/g-p-project/c/pending",
            "Pending",
            (),
            False,
            "unknown",
            "still loading",
        )
        result = self.update({"pending": unknown, "good": conversation("good", 1)})
        state, _ = self.state()
        self.assertFalse(result.ok)
        self.assertEqual(result.pending_unknown_loading_chats, 1)
        self.assertEqual(result.failed_chats, 0)
        self.assertEqual(result.saved_chats, 0)
        self.assertNotIn("pending", state["chats"])
        self.assertEqual(result.errors[0]["stage"], "extraction")

    def test_orphan_markdown_from_failed_state_write_is_recovered_without_overwrite(
        self,
    ):
        self.update({"a": conversation("a", 2)})
        state, directory = self.state()
        path = directory / "chats" / "a.md"
        original = path.read_bytes()
        state["chats"] = {}
        (directory / "project.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = self.update({"a": conversation("a", 2)})
        recovered, _ = self.state()
        self.assertTrue(result.ok)
        self.assertEqual(result.qa_pairs_appended, 0)
        self.assertEqual(recovered["chats"]["a"]["qa_count"], 2)
        self.assertEqual(path.read_bytes(), original)

    def test_existing_unmarked_tail_is_preserved_before_new_revision(self):
        self.update({"a": conversation("a", 1)})
        _, directory = self.state()
        path = directory / "chats/a.md"
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("\n## Q2\n\npartial interrupted write")
        before = path.read_bytes()
        result = self.update({"a": conversation("a", 2)})
        self.assertTrue(result.ok)
        self.assertTrue(path.read_bytes().startswith(before))
        self.assertEqual(self.state()[0]["chats"]["a"]["qa_count"], 2)

    def test_fewer_markers_than_state_is_an_error_and_preserves_bytes(self):
        self.update({"a": conversation("a", 2)})
        _, directory = self.state()
        path = directory / "chats" / "a.md"
        broken = path.read_text(encoding="utf-8").replace(
            "<!-- outogpt-qa-end:2 -->", "<!-- missing-marker:2 -->"
        )
        path.write_text(broken, encoding="utf-8", newline="\n")
        before = path.read_bytes()

        result = self.update({"a": conversation("a", 3)})
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0]["code"], "MARKDOWN_ARCHIVE_ERROR")
        self.assertEqual(path.read_bytes(), before)

    def test_corrupt_project_state_blocks_writes_instead_of_starting_over(self):
        self.update({"a": conversation("a", 1)})
        state_path = next(self.root.glob("*/project.json"))
        state_path.write_bytes(b"{not-json")
        before = state_path.read_bytes()

        result = self.update({"a": conversation("a", 2)})
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0]["code"], "PROJECT_STATE_ERROR")
        self.assertEqual(state_path.read_bytes(), before)

    def test_project_name_change_recovers_archive_by_project_id(self):
        self.update({"a": conversation("a", 1)})
        state, directory = self.state()
        archive = ProjectArchive.open(
            self.root,
            state["project_id"],
            "Renamed Project",
            PROJECT_URL,
        )
        self.assertEqual(archive.directory, directory)
        self.assertEqual(archive.state.project_name, "Renamed Project")

    def test_same_project_names_are_separated_by_project_id(self):
        first = ProjectArchive.open(
            self.root, "g-p-first", "Same Name", "https://chatgpt.com/g/g-p-first"
        )
        first.save_state()
        second = ProjectArchive.open(
            self.root, "g-p-second", "Same Name", "https://chatgpt.com/g/g-p-second"
        )
        second.save_state()
        self.assertNotEqual(first.directory, second.directory)
        self.assertIn("g-p-second", second.directory.name)

    def test_project_directory_and_chat_file_names_are_windows_safe(self):
        self.assertEqual(
            sanitize_component('bad<>:"/\\|?*name. ', "fallback"), "bad---------name"
        )
        self.assertEqual(sanitize_component("CON.txt", "fallback"), "_CON.txt")
        archive = ProjectArchive.open(
            self.root, "g-p-safe", "Safe", "https://chatgpt.com/g/g-p-safe"
        )
        with self.assertRaises(MarkdownArchiveError):
            archive.chat_path("../escape")

    def test_broken_discovery_does_not_touch_existing_state(self):
        self.update({"a": conversation("a", 1)})
        state_path = next(self.root.glob("*/project.json"))
        original = state_path.read_bytes()
        browser = FakeBrowser({}, discovery_error=PageStructureChanged("broken DOM"))
        result = ProjectUpdater(browser, self.root).update(PROJECT_URL)
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0]["code"], "PAGE_STRUCTURE_CHANGED")
        self.assertEqual(state_path.read_bytes(), original)

    def test_extraction_error_retries_same_chat_and_stops_remaining_chats(self):
        browser = FakeBrowser(
            {
                "bad": PageStructureChanged("bad messages"),
                "good": conversation("good", 1),
            }
        )
        result = ProjectUpdater(browser, self.root).update(PROJECT_URL)
        state, directory = self.state()
        self.assertTrue(result.paused)
        self.assertEqual(browser.read_ids, ["bad", "bad", "bad"])
        self.assertEqual(result.saved_chats, 0)
        self.assertEqual(state["chats"], {})
        progress = json.loads(
            (directory / "update-progress.json").read_text(encoding="utf-8")
        )
        self.assertEqual(progress["pending_chat_id"], "bad")

    def test_unicode_code_blocks_and_multiline_answers_survive(self):
        snapshot = ConversationSnapshot(
            "korean",
            "https://chatgpt.com/g/g-p-project/c/korean",
            "한국어 대화",
            (
                QAPair(
                    "질문입니다",
                    "첫 줄\n\n```python\nprint('안녕')\n```\n\n마지막 줄",
                ),
            ),
        )
        self.update({"korean": snapshot})
        _, directory = self.state()
        markdown = (directory / "chats" / "korean.md").read_text(encoding="utf-8")
        self.assertIn("질문입니다", markdown)
        self.assertIn("```python\nprint('안녕')\n```", markdown)
        self.assertIn("마지막 줄", markdown)

    def test_rich_markdown_is_preserved_without_flattening(self):
        rich = (
            "# Heading\n\nA paragraph with **bold**, *emphasis*, and `inline()`.\n\n"
            "- first\n- second\n\n> quoted\n\n"
            "[OpenAI](https://openai.com)\n\n"
            "| Name | Value |\n| --- | --- |\n| alpha | 1 |\n\n"
            "![diagram](https://example.com/diagram.png)\n\n"
            "$$\nx^2 + y^2\n$$"
        )
        snapshot = ConversationSnapshot(
            "rich",
            "https://chatgpt.com/g/g-p-project/c/rich",
            "Rich content",
            (QAPair("Show rich output", rich),),
        )
        self.update({"rich": snapshot})
        _, directory = self.state()
        markdown = (directory / "chats" / "rich.md").read_text(encoding="utf-8")
        self.assertIn(rich, markdown)


if __name__ == "__main__":
    unittest.main()
