import unittest
from unittest.mock import patch

from outogpt_controller.adapters.browser import BrowserAdapter
from outogpt_controller.paths import EXTENSION_DIR


class FakeSession:
    def __init__(self, **options):
        self.options = options
        self.page = object()
        self.closed = False
        self.find_result = self.page

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.closed = True

    def new_page(self):
        return self.page

    def find_page(self, url):
        return self.find_result


class BrowserAdapterTests(unittest.TestCase):
    def test_connected_session_creates_and_reuses_caller_owned_pages(self):
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

        self.assertEqual(sessions[0].options, {})
        self.assertEqual(adapter.extension_path, EXTENSION_DIR.resolve())
        self.assertIs(create.call_args.args[0], sessions[0].page)
        self.assertIs(send.call_args.args[0], sessions[0].page)
        self.assertTrue(sessions[0].closed)

    def test_closed_conversation_tab_gets_a_new_page(self):
        session = FakeSession()
        session.find_result = None
        adapter = BrowserAdapter(session_factory=lambda **_: session).open()
        with patch(
            "outogpt_controller.adapters.browser.continue_chat_in_page",
            return_value="https://chatgpt.com/c/abc",
        ) as send:
            adapter.send_prompt("https://chatgpt.com/c/abc", "next")
        self.assertIs(send.call_args.args[0], session.page)
        adapter.close()


if __name__ == "__main__":
    unittest.main()
