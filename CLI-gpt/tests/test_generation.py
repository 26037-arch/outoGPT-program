import unittest
from unittest.mock import patch

from cli_gpt.chatgpt import (
    GenerationSignals,
    GenerationState,
    GenerationTracker,
    wait_for_generation,
)
from cli_gpt.errors import GenerationNotStarted, GenerationTimeout


class GenerationTests(unittest.TestCase):
    def test_state_machine_waits_generates_and_completes_when_stable(self):
        tracker = GenerationTracker(baseline_assistant_count=2, stable_observations=2)
        tracker.mark_prompt_sent()

        waiting = GenerationSignals(False, True, False, 2, "")
        streaming = GenerationSignals(True, False, False, 3, "3:10:partial")
        complete = GenerationSignals(False, True, True, 3, "3:20:complete")

        self.assertEqual(tracker.observe(waiting), GenerationState.WAITING_FOR_GENERATION)
        self.assertEqual(tracker.observe(streaming), GenerationState.GENERATING)
        self.assertEqual(tracker.observe(complete), GenerationState.GENERATING)
        self.assertEqual(tracker.observe(complete), GenerationState.COMPLETE)

    def test_generation_not_started_timeout(self):
        signal = GenerationSignals(False, True, True, 0, "")
        clock = iter((0.0, 31.0))
        with patch("cli_gpt.chatgpt._sample_signals", return_value=signal):
            with self.assertRaises(GenerationNotStarted):
                wait_for_generation(
                    object(),
                    0,
                    monotonic=lambda: next(clock),
                    sleep=lambda _: None,
                )

    def test_generation_timeout_after_start(self):
        signal = GenerationSignals(True, False, False, 1, "partial")
        clock = iter((0.0, 601.0))
        with patch("cli_gpt.chatgpt._sample_signals", return_value=signal):
            with self.assertRaises(GenerationTimeout):
                wait_for_generation(
                    object(),
                    0,
                    monotonic=lambda: next(clock),
                    sleep=lambda _: None,
                )


if __name__ == "__main__":
    unittest.main()

