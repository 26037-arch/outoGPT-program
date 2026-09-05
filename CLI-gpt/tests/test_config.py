import json
import tempfile
import unittest
from pathlib import Path

from cli_gpt.config import (
    load_project_url,
    save_project_url,
    validate_chat_url,
    validate_project_url,
)
from cli_gpt.errors import InvalidChatUrl, InvalidProjectUrl


class ConfigTests(unittest.TestCase):
    def test_save_and_load_project_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            url = "https://chatgpt.com/g/g-p-example/project"
            save_project_url(url, path)

            self.assertEqual(load_project_url(path), url)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"project_url": url})

    def test_missing_config_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(InvalidProjectUrl, "gpt setup"):
                load_project_url(Path(directory) / "missing.json")

    def test_project_url_rejects_chat_url_and_foreign_host(self):
        with self.assertRaises(InvalidProjectUrl):
            validate_project_url("https://chatgpt.com/c/abc")
        with self.assertRaises(InvalidProjectUrl):
            validate_project_url("https://chatgpt.com.evil.example/project/abc")

    def test_chat_url_accepts_project_nested_conversation(self):
        url = "https://chatgpt.com/g/g-p-example/c/conversation-id"
        self.assertEqual(validate_chat_url(url), url)

    def test_chat_url_requires_conversation_identifier(self):
        for url in ("https://chatgpt.com/", "https://chatgpt.com/c/"):
            with self.subTest(url=url), self.assertRaises(InvalidChatUrl):
                validate_chat_url(url)


if __name__ == "__main__":
    unittest.main()

