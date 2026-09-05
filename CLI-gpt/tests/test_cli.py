import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cli_gpt.cli import build_parser, friendly_error, main
from cli_gpt.errors import GenerationTimeout, InvalidChatUrl, PromptBoxNotFound


class CliTests(unittest.TestCase):
    def test_argument_parsing(self):
        parser = build_parser()
        new = parser.parse_args(["new", "여러 줄\nprompt"])
        send = parser.parse_args(["send", "https://chatgpt.com/c/abc", "follow up"])

        self.assertEqual((new.command, new.prompt), ("new", "여러 줄\nprompt"))
        self.assertEqual((send.command, send.chat_url, send.prompt), (
            "send",
            "https://chatgpt.com/c/abc",
            "follow up",
        ))

    def test_setup_command(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            stdout = io.StringIO()
            with patch("cli_gpt.cli.save_project_url") as save, redirect_stdout(stdout):
                result = main(["setup", "https://chatgpt.com/g/project"])
            self.assertEqual(result, 0)
            save.assert_called_once_with("https://chatgpt.com/g/project")
            self.assertIn("Project URL saved.", stdout.getvalue())
            self.assertFalse(config_path.exists())

    def test_expected_error_is_friendly_without_traceback(self):
        stderr = io.StringIO()
        with patch("cli_gpt.cli.continue_chat", side_effect=InvalidChatUrl("bad URL")), redirect_stderr(stderr):
            result = main(["send", "bad", "prompt"])
        self.assertEqual(result, 1)
        self.assertIn("Invalid chat URL", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_exception_mapping(self):
        self.assertIn("Prompt box not found", friendly_error(PromptBoxNotFound("changed")))
        self.assertIn("timed out", friendly_error(GenerationTimeout("slow")))


if __name__ == "__main__":
    unittest.main()

