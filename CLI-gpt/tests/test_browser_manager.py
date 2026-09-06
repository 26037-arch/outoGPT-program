import tempfile
import unittest
from pathlib import Path

from cli_gpt.browser import BrowserManager, BrowserPhase
from cli_gpt.errors import ExtensionLoadFailed


class FakePage:
    def __init__(self, *, closed=False):
        self.closed = closed
        self.visits = []

    def is_closed(self):
        return self.closed

    def goto(self, url, **options):
        self.visits.append((url, options))


class FakeWorker:
    def __init__(self, url):
        self.url = url

    def evaluate(self, expression):
        return self.url.split("://", 1)[1].split("/", 1)[0]


class FakeContext:
    def __init__(self, workers=()):
        self.service_workers = list(workers)
        self.created = []
        self.closed = False

    def new_page(self):
        page = FakePage()
        self.created.append(page)
        return page

    def close(self):
        self.closed = True

    def expect_event(self, event, timeout):
        raise TimeoutError


class EventInfo:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class EventContext(FakeContext):
    def __init__(self, worker):
        super().__init__()
        self.worker = worker

    def expect_event(self, event, timeout):
        self.asserted_event = event
        return EventInfo(self.worker)


class BrowserManagerTests(unittest.TestCase):
    def manager(self, directory):
        extension = Path(directory) / "extension"
        extension.mkdir()
        (extension / "manifest.json").write_text(
            '{"manifest_version":3,"background":{"service_worker":"background/router.js"}}',
            encoding="utf-8",
        )
        return BrowserManager(
            profile_dir=Path(directory) / "chromium-profile",
            lock_path=Path(directory) / "profile.lock",
            extension_path=extension,
        )

    def test_launch_is_bundled_chromium_with_persistent_profile_and_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            options = manager._launch_options()
            self.assertEqual(options["channel"], "chromium")
            self.assertFalse(options["headless"])
            self.assertNotIn("executable_path", options)
            self.assertEqual(manager.profile_dir, Path(directory) / "chromium-profile")
            self.assertEqual(len(options["args"]), 2)
            bootstrap_options = manager._launch_options(with_extension=False)
            self.assertNotIn("args", bootstrap_options)

    def test_service_worker_success_and_failure_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            worker = FakeWorker("chrome-extension://abc/background/router.js")
            manager.context = FakeContext([worker])
            manager.phase = BrowserPhase.AUTOMATION
            self.assertIs(manager.verify_extension(timeout=0), worker)

            manager.context = FakeContext()
            manager.phase = BrowserPhase.AUTOMATION
            with self.assertRaises(ExtensionLoadFailed):
                manager.verify_extension(timeout=0)

    def test_service_worker_event_is_used_when_worker_is_not_already_registered(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            worker = FakeWorker("chrome-extension://abc/background/router.js")
            manager.context = EventContext(worker)
            manager.phase = BrowserPhase.AUTOMATION
            self.assertIs(manager.verify_extension(timeout=1), worker)
            self.assertEqual(manager.context.asserted_event, "serviceworker")

    def test_new_conversation_reuses_page_and_closed_page_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.context = FakeContext()
            first = manager.new_conversation("https://chatgpt.com/g/project")
            manager.bind_conversation(first, "https://chatgpt.com/c/a")

            self.assertIs(
                manager.page_for_conversation(
                    "a", "https://chatgpt.com/c/a", first.project_url
                ).page,
                first.page,
            )
            self.assertEqual(len(manager.context.created), 1)

            second = manager.new_conversation(first.project_url)
            self.assertIsNot(second.page, first.page)
            self.assertEqual(len(manager.context.created), 2)

            closed_page = first.page
            closed_page.closed = True
            recovered = manager.page_for_conversation(
                "a", "https://chatgpt.com/c/a", first.project_url
            )
            self.assertIsNot(recovered.page, closed_page)
            self.assertEqual(recovered.page.visits[0][0], "https://chatgpt.com/c/a")
            self.assertEqual(len(manager.context.created), 3)

    def test_context_stays_open_until_manager_close(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            context = FakeContext()
            manager.context = context
            manager.new_conversation("https://chatgpt.com/g/project")
            self.assertFalse(context.closed)
            manager.close()
            self.assertTrue(context.closed)


if __name__ == "__main__":
    unittest.main()
