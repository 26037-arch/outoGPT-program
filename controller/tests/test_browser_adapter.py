import unittest
from unittest.mock import patch

from outogpt_controller.adapters.browser import BrowserAdapter
from outogpt_controller.paths import EXTENSION_DIR


class FakeSession:
    def __init__(self, **options):
        self.options = options
        self.page = object()
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.closed = True


class BrowserAdapterTests(unittest.TestCase):
    def test_same_page_session_loads_repository_extension_and_reuses_cli_gpt(self):
        sessions = []

        def session_factory(**options):
            session = FakeSession(**options)
            sessions.append(session)
            return session

        adapter = BrowserAdapter(session_factory=session_factory)
        adapter.open()
        with (
            patch(
                "outogpt_controller.adapters.browser.create_chat_in_page",
                return_value="https://chatgpt.com/c/abc",
            ) as create,
            patch(
                "outogpt_controller.adapters.browser.continue_chat_in_page",
                return_value="https://chatgpt.com/c/abc",
            ) as send,
        ):
            adapter.create_chat("https://chatgpt.com/g/project", "first")
            adapter.send_prompt("https://chatgpt.com/c/abc", "next")
        adapter.close()

        self.assertEqual(sessions[0].options["extension_path"], EXTENSION_DIR.resolve())
        self.assertIs(create.call_args.args[0], sessions[0].page)
        self.assertIs(send.call_args.args[0], sessions[0].page)
        self.assertTrue(sessions[0].closed)


if __name__ == "__main__":
    unittest.main()
