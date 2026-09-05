import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from cli_gpt.browser import BrowserSession
from cli_gpt.chatgpt import (
    continue_chat,
    continue_chat_in_page,
    create_chat,
    create_chat_in_page,
)
from cli_gpt.errors import InvalidExtensionPath


class PageApiTests(unittest.TestCase):
    def test_create_chat_in_page_reuses_existing_actions(self):
        page = MagicMock()
        page.url = "https://chatgpt.com/g/project"
        prompt_box = object()
        with (
            patch("cli_gpt.chatgpt._open", return_value=prompt_box) as open_page,
            patch("cli_gpt.chatgpt._send_prompt_and_wait") as send,
            patch(
                "cli_gpt.chatgpt._wait_for_new_chat_url",
                return_value="https://chatgpt.com/c/abc",
            ) as wait_url,
        ):
            result = create_chat_in_page(page, page.url, "prompt")
        self.assertEqual(result, "https://chatgpt.com/c/abc")
        open_page.assert_called_once()
        send.assert_called_once()
        wait_url.assert_called_once_with(page, "https://chatgpt.com/g/project")

    def test_continue_chat_in_page_reuses_existing_actions(self):
        page = MagicMock()
        page.url = "https://chatgpt.com/c/abc"
        with (
            patch("cli_gpt.chatgpt._open", return_value=object()),
            patch("cli_gpt.chatgpt._send_prompt_and_wait") as send,
        ):
            result = continue_chat_in_page(page, page.url, "prompt")
        self.assertEqual(result, page.url)
        send.assert_called_once()

    def test_legacy_wrappers_still_own_a_browser_session(self):
        session = MagicMock()
        session.__enter__.return_value.page = object()
        with (
            patch("cli_gpt.chatgpt.BrowserSession", return_value=session),
            patch(
                "cli_gpt.chatgpt.create_chat_in_page",
                return_value="https://chatgpt.com/c/new",
            ) as create_in_page,
        ):
            self.assertEqual(
                create_chat("https://chatgpt.com/g/project", "prompt"),
                "https://chatgpt.com/c/new",
            )
        create_in_page.assert_called_once_with(
            session.__enter__.return_value.page,
            "https://chatgpt.com/g/project",
            "prompt",
            progress=None,
        )
        session.__exit__.assert_called_once()

        with (
            patch("cli_gpt.chatgpt.BrowserSession", return_value=session),
            patch(
                "cli_gpt.chatgpt.continue_chat_in_page",
                return_value="https://chatgpt.com/c/abc",
            ) as continue_in_page,
        ):
            continue_chat("https://chatgpt.com/c/abc", "next")
        continue_in_page.assert_called_once()


class BrowserExtensionTests(unittest.TestCase):
    def test_extension_launch_arguments_are_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            extension = Path(directory) / "extension"
            extension.mkdir()
            (extension / "manifest.json").write_text("{}", encoding="utf-8")
            session = BrowserSession(
                profile_dir=Path(directory) / "profile",
                lock_path=Path(directory) / "lock",
                extension_path=extension,
            )
            args = session._launch_options()["args"]
            resolved = str(extension.resolve())
            self.assertEqual(
                args,
                [
                    f"--disable-extensions-except={resolved}",
                    f"--load-extension={resolved}",
                ],
            )

            plain = BrowserSession(
                profile_dir=Path(directory) / "plain-profile",
                lock_path=Path(directory) / "plain-lock",
            )
            self.assertNotIn("args", plain._launch_options())

    def test_invalid_extension_paths_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(InvalidExtensionPath):
                BrowserSession(extension_path=root / "missing")
            empty = root / "empty"
            empty.mkdir()
            with self.assertRaisesRegex(InvalidExtensionPath, "manifest.json"):
                BrowserSession(extension_path=empty)


if __name__ == "__main__":
    unittest.main()
