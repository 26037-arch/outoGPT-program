import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from cli_gpt.browser import (
    BrowserSession,
    ChromeExecutableResolver,
    ChromeLauncher,
    ChromeProcess,
)
from cli_gpt.errors import ChromeDebugPortUnavailable, ChromeNotFound, LoginNotReady
from cli_gpt.setup import interactive_setup


class ChromeExecutableResolverTests(unittest.TestCase):
    def test_finds_known_windows_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chrome = root / "Google" / "Chrome" / "Application" / "chrome.exe"
            chrome.parent.mkdir(parents=True)
            chrome.touch()
            resolver = ChromeExecutableResolver(
                environ={"PROGRAMFILES": str(root)}, which=lambda _: None
            )
            self.assertEqual(resolver.resolve(), chrome.resolve())

    def test_explicit_configured_path_works(self):
        with tempfile.TemporaryDirectory() as directory:
            chrome = Path(directory) / "chrome.exe"
            chrome.touch()
            self.assertEqual(
                ChromeExecutableResolver(chrome).resolve(), chrome.resolve()
            )

    def test_missing_chrome_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.exe"
            with self.assertRaisesRegex(ChromeNotFound, "does not exist"):
                ChromeExecutableResolver(missing).resolve()


class ChromeLauncherTests(unittest.TestCase):
    def make_launcher(self, root, *, popen=None):
        return ChromeLauncher(
            root / "chrome.exe",
            root / "profile",
            state_path=root / "state.json",
            lock=MagicMock(),
            popen=popen or MagicMock(),
        )

    def test_command_uses_local_cdp_and_dedicated_profile_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = ChromeLauncher(
                root / "chrome.exe",
                root / "profile",
                state_path=root / "state.json",
                lock=MagicMock(),
            )
            command = launcher.command()
            self.assertIn("--remote-debugging-address=127.0.0.1", command)
            self.assertIn("--remote-debugging-port=9222", command)
            self.assertIn(f"--user-data-dir={(root / 'profile').resolve()}", command)
            joined = " ".join(command)
            self.assertNotIn("--load-extension", joined)
            self.assertNotIn("--disable-extensions-except", joined)
            self.assertNotIn("stealth", joined.lower())

    def test_launched_process_is_tracked_and_cleanup_is_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = MagicMock()
            process.pid = 4321
            process.poll.return_value = None
            lock = MagicMock()
            launcher = ChromeLauncher(
                root / "chrome.exe",
                root / "profile",
                state_path=root / "state.json",
                lock=lock,
                popen=MagicMock(return_value=process),
            )
            with (
                patch.object(
                    launcher, "cdp_available", side_effect=[False, False, True]
                ),
                patch.object(launcher, "port_in_use", return_value=False),
            ):
                chrome = launcher.launch_or_attach()

            self.assertTrue(chrome.owned)
            self.assertIs(chrome.handle, process)
            launcher.popen.assert_called_once()
            self.assertEqual(launcher.popen.call_args.args[0], launcher.command())
            lock.acquire.assert_called_once()
            lock.release.assert_called_once()

            launcher.stop(chrome)
            process.terminate.assert_called_once()
            self.assertFalse((root / "state.json").exists())

    def test_healthy_recorded_process_is_attached_and_not_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = (root / "chrome.exe").resolve()
            profile = (root / "profile").resolve()
            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "pid": 1234,
                        "port": 9222,
                        "profile_dir": str(profile),
                        "executable": str(executable),
                    }
                ),
                encoding="utf-8",
            )
            popen = MagicMock()
            launcher = ChromeLauncher(
                executable,
                profile,
                state_path=state,
                lock=MagicMock(),
                popen=popen,
            )
            with (
                patch("cli_gpt.browser._process_is_alive", return_value=True),
                patch.object(launcher, "cdp_available", return_value=True),
                patch.object(launcher, "_find_listener_pid", return_value=1234),
                patch.object(
                    launcher, "_process_matches_dedicated_chrome", return_value=True
                ),
            ):
                chrome = launcher.launch_or_attach()
            self.assertFalse(chrome.owned)
            self.assertEqual(chrome.pid, 1234)
            popen.assert_not_called()

    def test_unrecorded_matching_chrome_is_recovered_and_attached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            popen = MagicMock()
            launcher = self.make_launcher(root, popen=popen)
            with (
                patch.object(launcher, "cdp_available", return_value=True),
                patch.object(launcher, "_find_listener_pid", return_value=13516),
                patch.object(
                    launcher, "_process_matches_dedicated_chrome", return_value=True
                ),
            ):
                chrome = launcher.launch_or_attach()

            self.assertEqual(chrome.pid, 13516)
            self.assertFalse(chrome.owned)
            self.assertIsNone(chrome.handle)
            popen.assert_not_called()
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["pid"], 13516)
            self.assertEqual(state["port"], 9222)
            self.assertEqual(state["profile_dir"], str((root / "profile").resolve()))
            self.assertEqual(state["executable"], str((root / "chrome.exe").resolve()))

    def test_unrecorded_chrome_with_wrong_profile_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = self.make_launcher(root)
            process = MagicMock()
            process.exe.return_value = str((root / "chrome.exe").resolve())
            process.cmdline.return_value = [
                str((root / "chrome.exe").resolve()),
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=9222",
                f"--user-data-dir={(root / 'other-profile').resolve()}",
            ]
            with (
                patch.object(launcher, "cdp_available", return_value=True),
                patch.object(launcher, "_find_listener_pid", return_value=13516),
                patch("cli_gpt.browser.psutil.Process", return_value=process),
                self.assertRaises(ChromeDebugPortUnavailable),
            ):
                launcher.launch_or_attach()
            launcher.popen.assert_not_called()

    def test_unrecorded_chrome_with_wrong_executable_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = self.make_launcher(root)
            process = MagicMock()
            process.exe.return_value = str((root / "other-chrome.exe").resolve())
            process.cmdline.return_value = launcher.command()
            with (
                patch.object(launcher, "cdp_available", return_value=True),
                patch.object(launcher, "_find_listener_pid", return_value=13516),
                patch("cli_gpt.browser.psutil.Process", return_value=process),
                self.assertRaises(ChromeDebugPortUnavailable),
            ):
                launcher.launch_or_attach()
            launcher.popen.assert_not_called()

    def test_non_chrome_listener_without_cdp_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self.make_launcher(Path(directory))
            with (
                patch.object(launcher, "cdp_available", return_value=False),
                patch.object(launcher, "port_in_use", return_value=True),
                self.assertRaises(ChromeDebugPortUnavailable),
            ):
                launcher.launch_or_attach()
            launcher.popen.assert_not_called()

    def test_matching_process_accepts_split_option_form(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = self.make_launcher(root)
            process = MagicMock()
            process.exe.return_value = str((root / "chrome.exe").resolve())
            process.cmdline.return_value = [
                str((root / "chrome.exe").resolve()),
                "--remote-debugging-address",
                "127.0.0.1",
                "--remote-debugging-port",
                "9222",
                "--user-data-dir",
                str((root / "profile").resolve()),
            ]
            with patch("cli_gpt.browser.psutil.Process", return_value=process):
                self.assertTrue(launcher._process_matches_dedicated_chrome(13516))

    def test_recovery_is_rechecked_after_launch_lock_is_acquired(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = MagicMock()
            launcher = ChromeLauncher(
                root / "chrome.exe",
                root / "profile",
                state_path=root / "state.json",
                lock=lock,
                popen=MagicMock(),
            )
            recovered = ChromeProcess(launcher.endpoint, 13516, owned=False)
            with (
                patch.object(launcher, "cdp_available", side_effect=[False, True]),
                patch.object(launcher, "port_in_use", return_value=False),
                patch.object(
                    launcher, "_recover_dedicated_chrome", return_value=recovered
                ) as recover,
            ):
                chrome = launcher.launch_or_attach()
            self.assertIs(chrome, recovered)
            recover.assert_called_once()
            launcher.popen.assert_not_called()
            lock.acquire.assert_called_once()
            lock.release.assert_called_once()


class BrowserSessionTests(unittest.TestCase):
    def session_parts(self, *, owned=True):
        handle = MagicMock()
        handle.poll.return_value = None
        chrome = ChromeProcess(
            "http://127.0.0.1:9222", 42, handle=handle if owned else None, owned=owned
        )
        launcher = MagicMock()
        launcher.endpoint = chrome.endpoint
        launcher.launch_or_attach.return_value = chrome
        page = object()
        context = MagicMock()
        context.pages = [page]
        browser = MagicMock()
        browser.contexts = [context]
        playwright = MagicMock()
        playwright.chromium.connect_over_cdp.return_value = browser
        starter = MagicMock()
        starter.start.return_value = playwright
        session = BrowserSession(
            launcher=launcher,
            require_setup=False,
            playwright_factory=lambda: starter,
        )
        return session, launcher, playwright, context, page

    def test_connect_over_cdp_reuses_existing_context(self):
        session, _, playwright, context, page = self.session_parts()
        session.open()
        self.assertIs(session.context, context)
        self.assertIs(session.page, page)
        playwright.chromium.connect_over_cdp.assert_called_once_with(
            "http://127.0.0.1:9222"
        )
        playwright.chromium.launch.assert_not_called()
        browser = playwright.chromium.connect_over_cdp.return_value
        browser.new_context.assert_not_called()
        session.close()

    def test_owned_process_is_stopped_but_attached_process_is_not(self):
        owned, owned_launcher, _, _, _ = self.session_parts(owned=True)
        owned.open()
        chrome = owned.chrome
        owned.close()
        owned_launcher.stop.assert_called_once_with(chrome)

        attached, attached_launcher, _, _, _ = self.session_parts(owned=False)
        attached.open()
        attached.close()
        attached_launcher.stop.assert_not_called()

    def test_keep_open_disconnects_without_stopping_owned_chrome(self):
        session, launcher, playwright, _, _ = self.session_parts(owned=True)
        session.keep_chrome_open = True
        session.open()
        session.close()
        playwright.stop.assert_called_once()
        launcher.stop.assert_not_called()

    def test_setup_marker_is_required_for_normal_open(self):
        session, launcher, _, _, _ = self.session_parts()
        with tempfile.TemporaryDirectory() as directory:
            session.setup_state_path = Path(directory) / "missing.json"
            session.require_setup = True
            with self.assertRaises(LoginNotReady):
                session.open()
        launcher.launch_or_attach.assert_not_called()


class ManualSetupTests(unittest.TestCase):
    def test_playwright_attachment_waits_for_explicit_confirmation(self):
        events = []
        page = object()
        session = MagicMock()
        session.start_chrome.side_effect = lambda: events.append("chrome_started")
        session.connect.side_effect = lambda: events.append("playwright_connected")
        session.find_chatgpt_pages.return_value = [page]

        def factory(**options):
            self.assertEqual(
                options, {"require_setup": False, "keep_chrome_open": True}
            )
            return session

        def confirm(_message):
            self.assertEqual(events, ["chrome_started"])
            events.append("confirmed")
            return ""

        with patch("cli_gpt.setup.find_prompt_box", return_value=object()):
            interactive_setup(
                session_factory=factory,
                input_func=confirm,
                output=lambda _: None,
            )

        self.assertEqual(
            events, ["chrome_started", "confirmed", "playwright_connected"]
        )
        session.mark_setup_complete.assert_called_once_with(extension_path=None)
        session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
