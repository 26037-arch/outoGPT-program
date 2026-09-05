"""Opt-in live test. It creates a real ChatGPT conversation."""

import os
import unittest

from cli_gpt.chatgpt import create_chat


@unittest.skipUnless(
    os.environ.get("CLI_GPT_RUN_INTEGRATION") == "1",
    "Set CLI_GPT_RUN_INTEGRATION=1 to run the live browser test.",
)
class LiveChatGptTests(unittest.TestCase):
    def test_create_chat(self):
        project_url = os.environ.get("CLI_GPT_PROJECT_URL")
        if not project_url:
            self.fail("CLI_GPT_PROJECT_URL is required for the live test.")
        chat_url = create_chat(project_url, "Reply with exactly: CLI-GPT integration test OK")
        self.assertIn("/c/", chat_url)


if __name__ == "__main__":
    unittest.main()

