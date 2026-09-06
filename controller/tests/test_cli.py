import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from outogpt_controller.cli import main
from outogpt_controller.models import ControllerResult, OperationState


class FakeController:
    def __init__(self, *, registry):
        self.registry = registry

    def create_chat(self, project_url, prompt):
        return ControllerResult(
            True,
            "op_create",
            "abc",
            "https://chatgpt.com/c/abc",
            OperationState.COMPLETED,
            "completed",
            "unconfirmed",
            "passive",
        )

    def send_prompt(self, chat_id, prompt):
        return ControllerResult(
            False,
            "op_send",
            chat_id,
            None,
            OperationState.FAILED,
            error_code="PROMPT_SEND_FAILED",
            error_message="failed",
        )


class CliTests(unittest.TestCase):
    def run_cli(self, arguments):
        with tempfile.TemporaryDirectory() as directory:
            stdout, stderr = io.StringIO(), io.StringIO()
            argv = ["--database", str(Path(directory) / "registry.sqlite3"), *arguments]
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(argv, controller_factory=FakeController)
            return code, stdout.getvalue(), stderr.getvalue()

    def test_json_success_is_exactly_one_machine_readable_object(self):
        code, stdout, stderr = self.run_cli(
            [
                "chat",
                "create",
                "--project-url",
                "https://chatgpt.com/g/project",
                "--prompt",
                "analyze",
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        lines = stdout.splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["operation_id"], "op_create")
        self.assertEqual(payload["archive_status"], "unconfirmed")

    def test_prompt_file_and_failure_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt.md"
            prompt.write_text("next", encoding="utf-8")
            code, stdout, _ = self.run_cli(
                [
                    "chat",
                    "send",
                    "--chat-id",
                    "abc",
                    "--prompt-file",
                    str(prompt),
                    "--json",
                ]
            )
        self.assertEqual(code, 1)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "PROMPT_SEND_FAILED")

    def test_json_argument_error_is_still_one_json_object(self):
        code, stdout, stderr = self.run_cli(
            [
                "chat",
                "create",
                "--project-url",
                "https://chatgpt.com/g/project",
                "--json",
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(len(stdout.splitlines()), 1)
        self.assertEqual(json.loads(stdout)["error"]["code"], "INVALID_ARGUMENT")

    def test_setup_delegates_to_manual_chrome_flow(self):
        stdout = io.StringIO()
        with (
            patch("outogpt_controller.cli.interactive_setup") as setup,
            redirect_stdout(stdout),
        ):
            code = main(["setup"])
        self.assertEqual(code, 0)
        setup.assert_called_once()
        self.assertIn("setup completed", stdout.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
