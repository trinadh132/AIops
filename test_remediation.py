"""
Tests for step 4: approval links, remediation routing, and the approval
Function URL handler. AWS (DynamoDB, SQS, SNS) runs in-memory via moto.

    python -m unittest test_remediation -v
"""

import base64
import json
import os
import re
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import boto3

from test_lambda_handler import AwsTestCase
from tools import approval
from tools.remediation import RemediationQueue, RemediationRouter, action_for, validate_action
from tools.runs import RunStore

os.environ.pop("SSM_PARAMETER_PREFIX", None)
import approval_handler as ah  # noqa: E402

KEY = "test-hmac-key"
BASE_URL = "https://approve.example.lambda-url.us-east-1.on.aws"
NOW = 1_800_000_000


def link_params(run_id="run-1", decision="approve", expires_at=NOW + 3600, key=KEY):
    url = approval.build_link(BASE_URL, key, run_id, decision, expires_at)
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def final_output(status="pending_approval", root_cause="disk full"):
    return {
        "status": status,
        "diagnosis": {"root_cause": root_cause, "confidence": 0.8, "reasoning": "r", "sources": ["runbook"]},
        "remediation_plan": {"steps": [{"step_number": 1, "action": "free space", "reversible": True}],
                             "overall_risk_level": "high"},
    }


class TestApprovalLinks(unittest.TestCase):
    def test_valid_link_verifies(self):
        self.assertIsNone(approval.verify(KEY, link_params(), now=NOW))

    def test_tampering_with_any_field_breaks_the_signature(self):
        for field, value in (("run_id", "run-2"), ("decision", "reject"), ("exp", str(NOW + 99999))):
            with self.subTest(field=field):
                params = {**link_params(), field: value}
                self.assertEqual(approval.verify(KEY, params, now=NOW), "bad_signature")

    def test_wrong_key_expired_and_malformed(self):
        self.assertEqual(approval.verify("other-key", link_params(), now=NOW), "bad_signature")
        self.assertEqual(approval.verify(KEY, link_params(expires_at=NOW - 1), now=NOW), "expired")
        self.assertEqual(approval.verify(KEY, {**link_params(), "decision": "delete"}, now=NOW), "bad_decision")
        self.assertEqual(approval.verify(KEY, {"run_id": "x"}, now=NOW), "missing_fields")
        self.assertEqual(approval.verify(KEY, {**link_params(), "exp": "soon"}, now=NOW), "bad_expiry")


class TestActionAllowlist(unittest.TestCase):
    def test_oom_kill_also_clears_the_leak_behind_it(self):
        self.assertEqual(action_for("OOM_KILL")["failure_modes"], ["OOM_KILL", "MEMORY_LEAK"])
        self.assertEqual(action_for("DISK_FULL")["failure_modes"], ["DISK_FULL"])

    def test_rejects_unlisted_actions_and_unknown_modes(self):
        for bad in ({"action": "rm -rf /", "failure_modes": ["DISK_FULL"]},
                    {"action": "deactivate_failure_mode", "failure_modes": ["NOT_A_MODE"]},
                    {"action": "deactivate_failure_mode", "failure_modes": []}):
            with self.subTest(action=bad):
                with self.assertRaises(ValueError):
                    validate_action(bad)


class RemediationAwsCase(AwsTestCase):
    def setUp(self):
        super().setUp()
        sqs = boto3.client("sqs")
        self.sqs = sqs
        self.queue_url = sqs.create_queue(QueueName="remediation")["QueueUrl"]
        # Capture approval emails: SNS -> SQS subscription, then read the queue.
        sns = boto3.client("sns")
        self.topic_arn = sns.create_topic(Name="approvals")["TopicArn"]
        self.inbox_url = sqs.create_queue(QueueName="inbox")["QueueUrl"]
        inbox_arn = sqs.get_queue_attributes(QueueUrl=self.inbox_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        sns.subscribe(TopicArn=self.topic_arn, Protocol="sqs", Endpoint=inbox_arn)
        self.sns = sns
        self.store = RunStore(self.make_table())
        self.queue = RemediationQueue(sqs, self.queue_url)

    def router(self, auto_remediate=False):
        return RemediationRouter(queue=self.queue, sns_client=self.sns, topic_arn=self.topic_arn,
                                 approval_base_url=BASE_URL, hmac_key=KEY, auto_remediate=auto_remediate)

    def drain(self, queue_url):
        msgs = self.sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get("Messages", [])
        return [json.loads(m["Body"]) for m in msgs]

    def emails(self):
        """SNS envelopes ({"Subject", "Message", ...}) delivered to the inbox queue."""
        return self.drain(self.inbox_url)

    def seed_pending_run(self, run_id="run-1", failure_type="DISK_FULL", root_cause="disk full"):
        claim = self.store.claim(run_id, {"failure_type": failure_type})
        fields = self.router().route(run_id, failure_type, final_output(root_cause=root_cause), now=NOW)
        self.store.complete(run_id, claim, {"final_output": final_output(root_cause=root_cause), **fields})
        self.drain(self.inbox_url)  # discard the approval email
        return run_id


class TestRouting(RemediationAwsCase):
    def test_auto_remediate_on_queues_low_risk_immediately(self):
        fields = self.router(auto_remediate=True).route("run-1", "DISK_FULL", final_output("auto_recommend"), now=NOW)
        self.assertEqual(fields["remediation_status"], "queued")
        self.assertEqual(fields["approval_status"], "not_required")
        [msg] = self.drain(self.queue_url)
        self.assertEqual(msg, {"run_id": "run-1", "issued_at": NOW, "action": "deactivate_failure_mode",
                               "failure_modes": ["DISK_FULL"]})
        self.assertEqual(self.emails(), [])

    def test_shadow_mode_sends_even_low_risk_to_approval(self):
        fields = self.router(auto_remediate=False).route("run-1", "DISK_FULL", final_output("auto_recommend"), now=NOW)
        self.assertEqual(fields["approval_status"], "pending")
        self.assertEqual(self.drain(self.queue_url), [])  # nothing executed
        self.assertEqual(len(self.emails()), 1)

    def test_high_risk_emails_signed_links_and_queues_nothing(self):
        fields = self.router(auto_remediate=True).route("run-1", "OOM_KILL", final_output(), now=NOW)
        self.assertEqual(fields["remediation_status"], "awaiting_approval")
        self.assertEqual(fields["approval_expires_at"], NOW + approval.DEFAULT_LINK_TTL_SECONDS)
        self.assertEqual(self.drain(self.queue_url), [])

        [email] = self.emails()
        self.assertIn("Approval needed: OOM_KILL", email["Subject"])
        links = re.findall(r"https://\S+", email["Message"])
        self.assertEqual(len(links), 2)
        for link in links:
            params = {k: v[0] for k, v in parse_qs(urlparse(link).query).items()}
            self.assertIsNone(approval.verify(KEY, params, now=NOW))

    def test_failed_or_rejected_diagnoses_route_nowhere(self):
        for status in ("diagnosis_failed", "rejected_invalid_alert"):
            fields = self.router(auto_remediate=True).route("run-1", "DISK_FULL", {"status": status}, now=NOW)
            self.assertEqual(fields, {"remediation_status": "none"})
        self.assertEqual(self.drain(self.queue_url), [])


class TestApprovalHandler(RemediationAwsCase):
    def request(self, method, params, now=NOW, queue=None):
        return ah.handle_request(method, params, store=self.store, queue=queue or self.queue,
                                 hmac_key=KEY, now=now)

    def test_get_shows_confirmation_and_changes_nothing(self):
        self.seed_pending_run()
        page = self.request("GET", link_params())
        self.assertEqual(page["statusCode"], 200)
        self.assertIn("<form method='post'>", page["body"])
        self.assertEqual(self.store.get("run-1")["approval_status"], "pending")
        self.assertEqual(self.drain(self.queue_url), [])

    def test_post_approve_queues_stored_action_exactly_once(self):
        self.seed_pending_run(failure_type="OOM_KILL")
        self.assertEqual(self.request("POST", link_params())["statusCode"], 200)

        run = self.store.get("run-1")
        self.assertEqual((run["approval_status"], run["remediation_status"]), ("approved", "queued"))
        [msg] = self.drain(self.queue_url)
        self.assertEqual(msg["failure_modes"], ["OOM_KILL", "MEMORY_LEAK"])

        replay = self.request("POST", link_params())
        self.assertEqual(replay["statusCode"], 409)
        self.assertEqual(self.drain(self.queue_url), [])

    def test_reject_then_approve_link_does_nothing(self):
        self.seed_pending_run()
        self.request("POST", link_params(decision="reject"))
        self.assertEqual(self.request("POST", link_params(decision="approve"))["statusCode"], 409)
        self.assertEqual(self.store.get("run-1")["remediation_status"], "rejected")
        self.assertEqual(self.drain(self.queue_url), [])

    def test_bad_links_get_one_generic_403(self):
        self.seed_pending_run()
        bodies = set()
        for params in ({**link_params(), "sig": "0" * 64}, link_params(expires_at=NOW - 1), {}):
            page = self.request("POST", params)
            self.assertEqual(page["statusCode"], 403)
            bodies.add(page["body"])
        self.assertEqual(len(bodies), 1)  # no hint about *why*
        self.assertEqual(self.store.get("run-1")["approval_status"], "pending")

    def test_queue_failure_reverts_to_pending_so_link_can_be_retried(self):
        self.seed_pending_run()

        class BrokenQueue:
            def enqueue(self, *a, **kw):
                raise ConnectionError("sqs down")

        self.assertEqual(self.request("POST", link_params(), queue=BrokenQueue())["statusCode"], 502)
        self.assertEqual(self.store.get("run-1")["approval_status"], "pending")
        self.assertEqual(self.request("POST", link_params())["statusCode"], 200)  # retry works

    def test_llm_output_is_escaped_on_the_page(self):
        self.seed_pending_run(root_cause="<script>alert(1)</script>")
        body = self.request("GET", link_params())["body"]
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_function_url_event_parsing_and_security_headers(self):
        self.seed_pending_run()
        form = "&".join(f"{k}={v}" for k, v in link_params().items())
        event = {"requestContext": {"http": {"method": "POST"}},
                 "body": base64.b64encode(form.encode()).decode(), "isBase64Encoded": True}
        deps = {"store": self.store, "queue": self.queue, "hmac_key": KEY}
        with patch.object(ah, "_deps", return_value=deps), patch.object(ah.time, "time", return_value=NOW):
            page = ah.handler(event, None)
        self.assertEqual(page["statusCode"], 200)
        self.assertEqual(page["headers"]["Cache-Control"], "no-store")
        self.assertIn("form-action 'self'", page["headers"]["Content-Security-Policy"])


class TestRunStoreApprovals(AwsTestCase):
    def setUp(self):
        super().setUp()
        self.store = RunStore(self.make_table())
        claim = self.store.claim("run-1", {})
        self.store.complete("run-1", claim, {"approval_status": "pending"})

    def test_decide_is_single_use(self):
        self.assertTrue(self.store.decide("run-1", "approve"))
        self.assertFalse(self.store.decide("run-1", "reject"))
        self.assertEqual(self.store.get("run-1")["approval_status"], "approved")

    def test_revert_only_from_approved(self):
        self.assertFalse(self.store.revert_decision("run-1"))  # still pending
        self.store.decide("run-1", "approve")
        self.assertTrue(self.store.revert_decision("run-1"))
        self.assertTrue(self.store.decide("run-1", "approve"))

    def test_set_fields_requires_existing_run(self):
        self.assertFalse(self.store.set_fields("run-missing", {"remediation_status": "queued"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
