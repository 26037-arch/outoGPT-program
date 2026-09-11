import json
import tempfile
import unittest
from pathlib import Path

from cli_gpt.errors import PageStructureChanged
from cli_gpt.project import (
    ConversationSnapshot,
    ProjectChat,
    ProjectDiscovery,
    QAPair,
)
from outogpt_controller.errors import MarkdownArchiveError
from outogpt_controller.project_archive import ProjectArchive
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
    def __init__(self, snapshots, *, discovery_error=None):
        self.snapshots = snapshots
        self.discovery_error = discovery_error
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
        return ProjectDiscovery("g-p-project", project_url, "테스트 프로젝트", chats)

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
        original = path.read_text(encoding="utf-8")
        first = self.update({"a": conversation("a", 2)})
        second = self.update({"a": conversation("a", 2)})
        self.assertEqual(first.unchanged_chats, 1)
        self.assertEqual(second.qa_pairs_appended, 0)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_one_and_multiple_new_qa_pairs_are_appended_once(self):
        self.update({"a": conversation("a", 1)})
        one = self.update({"a": conversation("a", 2)})
        many = self.update({"a": conversation("a", 4)})
        self.assertEqual((one.updated_chats, one.qa_pairs_appended), (1, 1))
        self.assertEqual((many.updated_chats, many.qa_pairs_appended), (1, 2))
        state, directory = self.state()
        self.assertEqual(state["chats"]["a"]["qa_count"], 4)
        markdown = (directory / "chats" / "a.md").read_text(encoding="utf-8")
        self.assertEqual(markdown.count("## Q"), 4)

    def test_current_count_below_saved_count_does_nothing(self):
        self.update({"a": conversation("a", 3)})
        _, directory = self.state()
        path = directory / "chats" / "a.md"
        original = path.read_bytes()
        state_path = directory / "project.json"
        original_state = state_path.read_bytes()
        result = self.update({"a": conversation("a", 2)})
        state, _ = self.state()
        self.assertEqual(result.unchanged_chats, 1)
        self.assertEqual(state["chats"]["a"]["qa_count"], 3)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(state_path.read_bytes(), original_state)

    def test_generating_chat_is_skipped_while_other_chat_updates(self):
        self.update({"a": conversation("a", 1), "b": conversation("b", 1)})
        result = self.update(
            {
                "a": conversation("a", 2, generating=True),
                "b": conversation("b", 2),
            }
        )
        state, _ = self.state()
        self.assertEqual(result.skipped_generating_chats, 1)
        self.assertEqual(result.updated_chats, 1)
        self.assertEqual(state["chats"]["a"]["qa_count"], 1)
        self.assertEqual(state["chats"]["b"]["qa_count"], 2)

    def test_new_generating_and_empty_chats_are_not_registered(self):
        result = self.update(
            {
                "a": conversation("a", 1, generating=True),
                "b": conversation("b", 0),
            }
        )
        state, directory = self.state()
        self.assertEqual(result.skipped_generating_chats, 1)
        self.assertEqual(result.skipped_empty_chats, 1)
        self.assertEqual(state["chats"], {})
        self.assertFalse((directory / "chats" / "a.md").exists())

    def test_state_advances_only_after_markdown_append_succeeds(self):
        self.update({"a": conversation("a", 1)})
        archive = ProjectArchive.open(
            self.root, "g-p-project", "테스트 프로젝트", PROJECT_URL
        )
        path = archive.chat_path("a")
        original = path.read_bytes()

        def fail_append(*args, **kwargs):
            raise MarkdownArchiveError("disk full")

        archive.append_pairs = fail_append
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

    def test_completed_append_is_recovered_if_state_write_was_interrupted(self):
        self.update({"a": conversation("a", 1)})
        archive = ProjectArchive.open(
            self.root, "g-p-project", "테스트 프로젝트", PROJECT_URL
        )
        archive.append_pairs("a", [QAPair("question 2", "answer 2")], 2)

        result = self.update({"a": conversation("a", 2)})
        state, directory = self.state()
        markdown = (directory / "chats" / "a.md").read_text(encoding="utf-8")
        self.assertEqual(result.qa_pairs_appended, 0)
        self.assertEqual(state["chats"]["a"]["qa_count"], 2)
        self.assertEqual(markdown.count("## Q"), 2)

    def test_broken_discovery_does_not_touch_existing_state(self):
        self.update({"a": conversation("a", 1)})
        state_path = next(self.root.glob("*/project.json"))
        original = state_path.read_bytes()
        browser = FakeBrowser({}, discovery_error=PageStructureChanged("broken DOM"))
        result = ProjectUpdater(browser, self.root).update(PROJECT_URL)
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0]["code"], "PAGE_STRUCTURE_CHANGED")
        self.assertEqual(state_path.read_bytes(), original)

    def test_one_chat_extraction_error_does_not_stop_remaining_chats(self):
        browser = FakeBrowser(
            {
                "bad": PageStructureChanged("bad messages"),
                "good": conversation("good", 1),
            }
        )
        result = ProjectUpdater(browser, self.root).update(PROJECT_URL)
        state, _ = self.state()
        self.assertFalse(result.ok)
        self.assertEqual(result.new_chats, 1)
        self.assertIn("good", state["chats"])
        self.assertNotIn("bad", state["chats"])

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


if __name__ == "__main__":
    unittest.main()
