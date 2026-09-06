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

    def test_setup_waits_for_each_enter_before_verifying_and_retries(self):
        class Page:
            def __init__(self):
                self.visits = []

            def goto(self, url, **options):
                self.visits.append(url)

        class SetupManager(FakeSession):
            def __init__(self, **options):
                super().__init__(**options)
                self.events = []
                self.page = None
                self.setup_page = None
                self.chatgpt_page = Page()
                self.extension_checks = 0

            def start_bootstrap(self):
                self.events.append("start_bootstrap")
                return self

            def find_open_chatgpt_page(self):
                self.events.append("inspect_pages")
                return self.chatgpt_page

            def finish_bootstrap_and_start_automation(self):
                self.events.append("close_bootstrap")
                self.events.append("start_automation")
                self.page = Page()
                self.setup_page = self.page
                return self

            def verify_extension(self):
                self.events.append("verify_extension")
                self.extension_checks += 1

        manager = SetupManager()
        adapter = BrowserAdapter(session_factory=lambda **options: manager)
        entered = []
        output = []

        def press_enter(prompt):
            self.assertEqual(manager.events.count("inspect_pages"), len(entered))
            entered.append(prompt)
            return ""

        with (
            patch(
                "outogpt_controller.adapters.browser.verify_chatgpt_login",
                side_effect=[False, True],
            ) as verify_login,
            patch(
                "outogpt_controller.adapters.browser.verify_project_access",
                return_value=True,
            ) as verify_project,
        ):
            adapter.setup(
                "https://chatgpt.com/g/project",
                input_func=press_enter,
                output=output.append,
            )

        self.assertEqual(len(entered), 2)
        self.assertEqual(verify_login.call_count, 2)
        verify_project.assert_called_once()
        self.assertEqual(manager.extension_checks, 1)
        self.assertFalse(manager.closed)
        self.assertIn("[setup] ChatGPT login was not detected.", output)
        self.assertEqual(
            manager.events,
            [
                "start_bootstrap",
                "inspect_pages",
                "inspect_pages",
                "close_bootstrap",
                "start_automation",
                "verify_extension",
            ],
        )
        self.assertEqual(manager.chatgpt_page.visits, [])


if __name__ == "__main__":
    unittest.main()
