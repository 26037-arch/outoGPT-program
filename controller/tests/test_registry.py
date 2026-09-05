import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from outogpt_controller.models import OperationState, OperationType
from outogpt_controller.registry import Registry


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.registry = Registry(Path(self.temporary.name) / "registry.sqlite3")

    def tearDown(self):
        self.temporary.cleanup()

    def test_schema_contains_required_tables(self):
        with closing(sqlite3.connect(self.registry.database_path)) as connection:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertTrue({"chats", "operations", "schema_version"} <= names)

    def test_chat_and_operation_round_trip(self):
        operation = self.registry.create_operation(OperationType.CREATE)
        self.registry.transition(
            operation.operation_id,
            OperationState.RESPONSE_COMPLETED,
            chat_id="abc",
        )
        self.registry.save_chat(
            "abc",
            "https://chatgpt.com/g/project",
            "https://chatgpt.com/c/abc",
            operation.operation_id,
        )
        completed = self.registry.complete_operation(operation.operation_id)

        chat = self.registry.get_chat("abc")
        self.assertEqual(chat.chat_url, "https://chatgpt.com/c/abc")
        self.assertEqual(chat.last_operation_id, operation.operation_id)
        self.assertEqual(completed.status, OperationState.COMPLETED)
        self.assertIsNotNone(completed.finished_at)
        self.assertEqual(
            self.registry.get_latest_operation("abc").operation_id,
            operation.operation_id,
        )

    def test_failure_fields_are_persisted(self):
        operation = self.registry.create_operation(OperationType.SEND, "abc")
        failed = self.registry.fail_operation(
            operation.operation_id, "PROMPT_SEND_FAILED", "could not send"
        )
        self.assertEqual(failed.status, OperationState.FAILED)
        self.assertEqual(failed.error_code, "PROMPT_SEND_FAILED")
        self.assertEqual(failed.error_message, "could not send")


if __name__ == "__main__":
    unittest.main()
