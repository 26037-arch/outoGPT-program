import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cli_gpt.errors import InvalidProjectUrl, PageStructureChanged
from outogpt_controller.cli import main
from outogpt_controller.models import ControllerResult, OperationState
from outogpt_controller.project_updater import ProjectUpdateResult


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

    def update_project(self, project_url, *, archive_root):
        result = ProjectUpdateResult(
            True,
            project_url,
            project_name="Demo Project",
            discovered_chats=2,
            new_chats=1,
            unchanged_chats=1,
            qa_pairs_appended=3,
        )
        result.archive_directory = str(archive_root / "Demo Project")
        if project_url.endswith("broken"):
            result.add_error(PageStructureChanged("broken project DOM"))
        return result


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

    def test_project_update_json_has_counters_and_archive_directory(self):
        code, stdout, stderr = self.run_cli(
            [
                "project",
                "update",
                "--project-url",
                "https://chatgpt.com/g/g-p-demo/project",
                "--archive-root",
                "archive",
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["discovered_chats"], 2)
        self.assertEqual(payload["new_chats"], 1)
        self.assertEqual(payload["qa_pairs_appended"], 3)
        self.assertTrue(payload["archive_directory"].endswith("Demo Project"))

    def test_project_update_uses_saved_url_when_option_is_omitted(self):
        with patch(
            "outogpt_controller.cli.load_project_url",
            return_value="https://chatgpt.com/g/g-p-saved/project",
        ):
            code, stdout, _ = self.run_cli(["project", "update", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(stdout)["project_url"],
            "https://chatgpt.com/g/g-p-saved/project",
        )

    def test_project_update_uses_saved_unicode_archive_root(self):
        with tempfile.TemporaryDirectory() as directory:
            configured = Path(directory) / "OneDrive 보관 폴더"
            with patch(
                "outogpt_controller.cli.load_archive_root", return_value=configured
            ):
                code, stdout, stderr = self.run_cli(
                    [
                        "project",
                        "update",
                        "--project-url",
                        "https://chatgpt.com/g/g-p-demo/project",
                        "--json",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["archive_root_source"], "config")
        self.assertEqual(Path(payload["archive_root"]), configured.resolve())

    def test_project_update_without_saved_url_is_invalid_argument(self):
        with patch(
            "outogpt_controller.cli.load_project_url",
            side_effect=InvalidProjectUrl("not configured"),
        ):
            code, stdout, stderr = self.run_cli(["project", "update", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["error"]["code"], "INVALID_ARGUMENT")

    def test_project_update_returns_one_json_object_and_exit_one_on_errors(self):
        code, stdout, stderr = self.run_cli(
            [
                "project",
                "update",
                "--project-url",
                "https://chatgpt.com/g/g-p-broken",
                "--json",
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(len(stdout.splitlines()), 1)
        self.assertEqual(json.loads(stdout)["errors"][0]["code"], "PAGE_STRUCTURE_CHANGED")


if __name__ == "__main__":
    unittest.main()
