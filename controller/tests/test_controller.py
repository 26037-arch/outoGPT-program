import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli_gpt.errors import BrowserLaunchFailed, PromptSendFailed
from outogpt_controller.adapters.archive import PassiveExtensionArchiveAdapter
from outogpt_controller.controller import OutogptController, extract_chat_id
from outogpt_controller.errors import InvalidChatUrlError
from outogpt_controller.models import ArchiveResult, OperationState
from outogpt_controller.registry import Registry


class RecordingRegistry(Registry):
    def __init__(self, path):
        self.states = []
        super().__init__(path)

    def transition(self, operation_id, state, **kwargs):
        self.states.append(state)
        return super().transition(operation_id, state, **kwargs)


class FakeArchive:
    def __init__(self):
        self.calls = []

    def wait_until_saved(self, chat_id, *, timeout=None):
        self.calls.append((chat_id, timeout))
        return ArchiveResult("unconfirmed", "passive")


class FakeBrowser:
    def __init__(self, *, error_at=None):
        self.error_at = error_at
        self.opened = False
        self.closed = False

    def open(self):
        if self.error_at == "open":
            raise BrowserLaunchFailed("launch failed")
        self.opened = True
        return self

    def close(self):
        self.closed = True

    def create_chat(self, project_url, prompt, *, progress=None):
        if self.error_at == "prompt":
            raise PromptSendFailed("send failed")
        progress("prompt_sent")
        return "https://chatgpt.com/c/new-chat"

    def send_prompt(
        self, chat_url, prompt, *, chat_id=None, project_url="", progress=None
    ):
        if self.error_at == "prompt":
            raise PromptSendFailed("send failed")
        progress("waiting")
        return chat_url


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.registry = RecordingRegistry(Path(self.temporary.name) / "registry.sqlite3")
        self.archive = FakeArchive()

    def tearDown(self):
        self.temporary.cleanup()

    def controller(self, browser):
        return OutogptController(
            self.registry,
            browser_factory=lambda: browser,
            archive_adapter=self.archive,
            archive_timeout=4.0,
        )

    def test_create_chat_success_records_state_chat_and_unconfirmed_archive(self):
        browser = FakeBrowser()
        result = self.controller(browser).create_chat(
            "https://chatgpt.com/g/project", "prompt"
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.chat_id, "new-chat")
        self.assertEqual(result.archive_status, "unconfirmed")
        self.assertTrue(browser.closed)
        self.assertEqual(self.archive.calls, [("new-chat", 4.0)])
        self.assertEqual(
            self.registry.states,
            [
                OperationState.BROWSER_STARTING,
                OperationState.PROMPT_SENDING,
                OperationState.WAITING_RESPONSE,
                OperationState.RESPONSE_COMPLETED,
                OperationState.ARCHIVING,
            ],
        )
        self.assertEqual(
            self.registry.get_operation(result.operation_id).status,
            OperationState.COMPLETED,
        )

    def test_send_prompt_uses_registry_url(self):
        initial = self.controller(FakeBrowser()).create_chat(
            "https://chatgpt.com/g/project", "first"
        )
        self.registry.states.clear()
        browser = FakeBrowser()
        result = self.controller(browser).send_prompt(initial.chat_id, "next")

        self.assertTrue(result.ok)
        self.assertEqual(result.chat_id, initial.chat_id)
        self.assertEqual(
            self.registry.get_chat(initial.chat_id).last_operation_id,
            result.operation_id,
        )
        self.assertTrue(browser.closed)

    def test_browser_error_is_failed_and_cleanup_is_safe(self):
        browser = FakeBrowser(error_at="open")
        result = self.controller(browser).create_chat(
            "https://chatgpt.com/g/project", "prompt"
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "BROWSER_START_FAILED")
        operation = self.registry.get_operation(result.operation_id)
        self.assertEqual(operation.status, OperationState.FAILED)
        self.assertIsNotNone(operation.finished_at)
        self.assertTrue(browser.closed)

    def test_prompt_error_is_failed_and_browser_closes(self):
        browser = FakeBrowser(error_at="prompt")
        result = self.controller(browser).create_chat(
            "https://chatgpt.com/g/project", "prompt"
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PROMPT_SEND_FAILED")
        self.assertTrue(browser.closed)

    def test_unknown_chat_id_is_structured_failure(self):
        result = self.controller(FakeBrowser()).send_prompt("missing", "prompt")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "UNKNOWN_CHAT_ID")
        self.assertEqual(
            self.registry.get_operation(result.operation_id).status,
            OperationState.FAILED,
        )

    def test_failed_send_is_the_chat_last_operation(self):
        created = self.controller(FakeBrowser()).create_chat(
            "https://chatgpt.com/g/project", "first"
        )
        failed = self.controller(FakeBrowser(error_at="prompt")).send_prompt(
            created.chat_id, "next"
        )
        self.assertEqual(
            self.registry.get_chat(created.chat_id).last_operation_id,
            failed.operation_id,
        )
        self.assertEqual(
            self.controller(FakeBrowser()).get_status(created.chat_id).operation.status,
            OperationState.FAILED,
        )

    def test_status_returns_latest_operation(self):
        result = self.controller(FakeBrowser()).create_chat(
            "https://chatgpt.com/g/project", "prompt"
        )
        status = self.controller(FakeBrowser()).get_status(result.chat_id)
        self.assertEqual(status.chat.chat_id, result.chat_id)
        self.assertEqual(status.operation.operation_id, result.operation_id)

    def test_production_browser_is_reused_until_controller_close(self):
        browser = FakeBrowser()
        with patch(
            "outogpt_controller.controller.BrowserAdapter", return_value=browser
        ):
            controller = OutogptController(
                self.registry,
                archive_adapter=self.archive,
                archive_timeout=4.0,
            )
            created = controller.create_chat(
                "https://chatgpt.com/g/project", "first"
            )
            sent = controller.send_prompt(created.chat_id, "next")

            self.assertTrue(created.ok)
            self.assertTrue(sent.ok)
            self.assertFalse(browser.closed)
            controller.close()
            self.assertTrue(browser.closed)

    def test_passive_archive_never_claims_saved(self):
        sleeps = []
        adapter = PassiveExtensionArchiveAdapter(2.0, sleep=sleeps.append)
        result = adapter.wait_until_saved("abc", timeout=1.0)
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(result.status, "unconfirmed")

    def test_chat_id_extraction(self):
        self.assertEqual(extract_chat_id("https://chatgpt.com/c/abc-_123"), "abc-_123")
        with self.assertRaises(InvalidChatUrlError):
            extract_chat_id("https://chatgpt.com/g/project")


if __name__ == "__main__":
    unittest.main()
