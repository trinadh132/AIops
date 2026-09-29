"""Offline tests for eval_agent.py's scoring. The evaluation itself needs a
live key and database; the arithmetic that grades it shouldn't.

    python -m unittest test_eval_agent -v
"""

import os
import unittest

os.environ["OPENROUTER_API"] = "test-dummy-key"  # see unittes.py: keep .env out

import eval_agent as ev  # noqa: E402


class TestRubric(unittest.TestCase):
    def test_every_failure_mode_has_a_rubric_and_a_runbook(self):
        import agentic as ag
        self.assertEqual(set(ev.RUBRIC), ag.VALID_FAILURE_TYPES)
        for mode in ev.RUBRIC:
            self.assertIn(ev.runbook_risk(mode), ev.RISK_ORDER)

    def test_on_topic_matches_runbook_concepts_and_rejects_wrong_failures(self):
        self.assertTrue(ev.on_topic("DISK_FULL", "Log files grew without rotation; No space left on device"))
        self.assertTrue(ev.on_topic("BAD_DEPLOY_ERROR_SPIKE", "NullPointerException introduced by the new release"))
        self.assertFalse(ev.on_topic("DISK_FULL", "Connection pool undersized for current traffic"))
        self.assertFalse(ev.on_topic("RETRY_STORM", ""))


class TestRisk(unittest.TestCase):
    def test_compare_risk_directions(self):
        self.assertEqual(ev.compare_risk("high", "high"), "agree")
        self.assertEqual(ev.compare_risk("high", "low"), "over")
        self.assertEqual(ev.compare_risk("low", "high"), "under")  # the unsafe one
        self.assertEqual(ev.compare_risk("critical", "high"), "invalid")
        self.assertEqual(ev.compare_risk(None, "low"), "invalid")

    def test_expected_decision_follows_escalation_policy(self):
        self.assertEqual(ev.expected_decision("high"), "pending_approval")
        self.assertEqual(ev.expected_decision("medium"), "auto_recommend")


class TestBlindMetrics(unittest.TestCase):
    def test_rank_and_metrics(self):
        self.assertEqual(ev.rank_of("DISK_FULL", ["OOM_KILL", "DISK_FULL", "DISK_FULL"]), 2)
        self.assertIsNone(ev.rank_of("DISK_FULL", ["OOM_KILL"]))
        m = ev.blind_metrics([1, 2, None, 4], k=3)
        self.assertEqual(m["recall@1"], 0.25)
        self.assertEqual(m["recall@3"], 0.5)
        self.assertAlmostEqual(m["mrr"], (1 + 0.5 + 0.25) / 4)


class TestVariants(unittest.TestCase):
    def test_tagged_loses_only_injection_lines(self):
        for mode in ev.RUBRIC:
            leaky, tagged = ev.load_log(mode, "leaky"), ev.load_log(mode, "tagged")
            self.assertIn('"event_type":"failure_injection"', leaky)
            self.assertNotIn('"event_type":"failure_injection"', tagged)
            self.assertEqual(len(leaky.splitlines()) - len(tagged.splitlines()), 1)

    def test_label_free_contains_no_trace_of_the_answer(self):
        for mode in ev.RUBRIC:
            text = ev.load_log(mode, "label_free")
            self.assertNotIn("failure_mode", text, mode)
            self.assertNotIn(mode, text, mode)  # not even the enum name
            self.assertTrue(text.strip(), mode)  # symptoms remain

    def test_strip_labels_handles_field_and_inline_forms(self):
        line = ('{"message":"failure_mode=DISK_FULL http_status=500 message=x",'
                '"failure_mode":"DISK_FULL","level":"ERROR"}')
        self.assertEqual(ev.strip_labels(line), '{"message":"http_status=500 message=x","level":"ERROR"}')


if __name__ == "__main__":
    unittest.main(verbosity=2)
