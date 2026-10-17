"""Runtime/API recovery wiring, identity and intent boundaries; no model runs."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_center_runtime as fixtures
from center_runtime import CenterError


class RecoveryApiTests(unittest.TestCase):
    setUp = fixtures.QueueTests.setUp
    source = fixtures.QueueTests.source

    def body(self, key="review-fixture"):
        return {"expectedRevision": 1, "expectedSourceRevision": 1, "idempotencyKey": key}

    def marker(self):
        return json.loads((Path(self.a["root"]) / ".autonomous-company-center.json").read_text())

    def test_execution_query_rechecks_domain_and_returned_marker(self):
        marker = self.marker()
        with patch.object(self.adapter, "query", return_value={"ok": True, "marker": {**marker, "sourceId": "foreign"}}) as query:
            with self.assertRaises(CenterError) as error:
                self.runtime.execution_query(self.a["sourceId"], "config")
            self.assertEqual(error.exception.code, "SOURCE_CONFLICT")
            query.assert_called_once()
        with self.store.transaction() as tx:
            runtime = tx.get("runtimes", self.a["runtimeId"])
            runtime["executionDomain"] = {"platform": "wsl", "distribution": "Other", "user": "other"}
            tx.put("runtimes", runtime["runtimeId"], runtime)
        with patch.object(self.adapter, "query") as query, self.assertRaises(CenterError):
            self.runtime.execution_query(self.a["sourceId"], "config")
        query.assert_not_called()

    def test_execution_log_alias_and_observation_preserve_actual_configuration(self):
        marker = self.marker()
        settings = {"ENGINE": "codex", "MODEL": "fixture-model", "CODEX_REASONING_EFFORT": "high", "AUTONOMOUS_COMPANY_LANGUAGE": "zh-CN"}
        with patch.object(self.adapter, "query", return_value={"ok": True, "marker": marker, "settings": settings}) as query:
            self.runtime.execution_query(self.a["sourceId"], "log", path="logs/fixture.log", offset=0, limit=8)
            self.assertEqual(query.call_args.kwargs["relative_path"], "logs/fixture.log")
            result = self.runtime.observe_source(self.a)
        self.assertEqual(result["config"], {"engine": "codex", "model": "fixture-model", "effort": "high", "productLanguage": "zh-CN"})
        self.assertEqual(result["status"]["executionDomain"], {"platform": "posix"})
        with patch.object(self.adapter, "query", return_value={"ok": True, "marker": marker, "settings": {}}):
            self.assertIsNone(self.runtime.observe_source(self.a)["config"]["engine"])

    def test_review_recovery_persists_intent_and_is_idempotent_without_dispatch(self):
        manifests = []
        def query(action, **kwargs):
            self.assertEqual(action, "control")
            manifest = json.loads(Path(kwargs["manifest"]).read_text())
            manifests.append(manifest)
            with self.store.transaction() as tx:
                self.assertEqual(tx.get("operations", manifest["operationId"])["state"], "preparing")
            return {"ok": True, "writersStopped": True, "recoveryComplete": True, "reviewToken": "review-proof", "resumed": False}
        with patch.object(self.adapter, "query", query):
            result = self.runtime.source_action(self.a["sourceId"], "review-recovery", self.body())
            again = self.runtime.source_action(self.a["sourceId"], "review-recovery", self.body())
        self.assertEqual(result, again)
        self.assertEqual(len(manifests), 1)
        self.assertEqual(manifests[0]["action"], "review_recovery")
        self.assertTrue(result["canConfirm"])
        self.assertFalse(result["resumed"])
        self.assertFalse(self.runtime.summary()["dispatchEnabled"])
        self.assertEqual(self.adapter.launched, [])

    def test_confirm_forwards_only_explicit_review_acceptance(self):
        body = {**self.body("confirm-fixture"), "reviewToken": "review-proof", "confirmCodeEffects": True, "confirmExternalEffects": "true"}
        manifests = []
        def query(action, **kwargs):
            manifests.append(json.loads(Path(kwargs["manifest"]).read_text()))
            return {"ok": True, "resumed": False}
        with patch.object(self.adapter, "query", query):
            result = self.runtime.source_action(self.a["sourceId"], "confirm-recovery", body)
        self.assertEqual(manifests[0]["action"], "confirm_recovery")
        self.assertTrue(manifests[0]["productCodeReviewed"])
        self.assertFalse(manifests[0]["externalEffectsReviewed"])
        self.assertEqual(manifests[0]["reviewToken"], "review-proof")
        self.assertFalse(result["resumed"])

    def test_review_returns_post_recovery_source_revisions(self):
        def refresh(source_id):
            with self.store.transaction() as tx:
                source = tx.get("sources", source_id)
                source.update(revision=4, sourceRevision=3)
                tx.put("sources", source_id, source)
                return source
        with patch.object(self.catalog, "refresh_source", refresh), patch.object(self.adapter, "query", return_value={
                "ok": True, "writersStopped": True, "recoveryComplete": True, "reviewToken": "proof", "resumed": False}):
            result = self.runtime.source_action(self.a["sourceId"], "review-recovery", self.body())
            self.assertEqual((result["expectedRevision"], result["expectedSourceRevision"]), (4, 3))
            repeated = self.runtime.source_action(self.a["sourceId"], "review-recovery", self.body())
        self.assertEqual(result, repeated)

    def test_stale_source_revision_and_open_request_refuse_before_control(self):
        with patch.object(self.adapter, "query") as query:
            with self.assertRaises(CenterError):
                self.runtime.source_action(self.a["sourceId"], "review-recovery", {**self.body(), "expectedSourceRevision": True})
            with self.store.transaction() as tx:
                tx.put("requests", "request_blocking", {"sourceId": self.a["sourceId"], "state": "queued"})
            with self.assertRaises(CenterError) as error:
                self.runtime.source_action(self.a["sourceId"], "review-recovery", self.body())
        self.assertEqual(error.exception.code, "OPEN_REQUEST_EXISTS")
        query.assert_not_called()

    def test_failed_recovery_cannot_repeat_external_effects_with_same_key_and_redacts(self):
        failure = subprocess.CalledProcessError(78, ["fixture"], stderr="refused fixture-sensitive-token")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fixture-sensitive-token"}), patch.object(self.adapter, "query", side_effect=failure) as query:
            with self.assertRaises(CenterError) as error:
                self.runtime.source_action(self.a["sourceId"], "review-recovery", self.body())
            self.assertNotIn("fixture-sensitive-token", str(error.exception))
            with self.assertRaises(CenterError):
                self.runtime.source_action(self.a["sourceId"], "review-recovery", self.body())
        self.assertEqual(query.call_count, 1)
        with self.store.transaction() as tx:
            self.assertEqual(tx.list("operations")[0]["state"], "failed")

    def test_owned_capabilities_follow_domain_and_query_failure_does_not_authorize(self):
        payload = {"ok": True, "marker": self.marker(), "context": {"valid": True}, "compatible": True,
                   "pauseReason": "interrupted_cycle", "protectiveReason": "interrupted_cycle"}
        with patch.object(self.adapter, "query", return_value=payload):
            result = self.runtime.decorate_entry({**self.a, "availability": "available"})
        self.assertFalse(result["capabilities"]["execute"])
        self.assertTrue(result["capabilities"]["reviewRecovery"])
        with patch.object(self.adapter, "query", side_effect=ValueError("unavailable")):
            result = self.runtime.decorate_entry({**self.a, "availability": "available"})
        self.assertFalse(result["capabilities"]["execute"])
        self.assertFalse(result["capabilities"]["reviewRecovery"])
        self.assertTrue(result["capabilities"]["release"])


class ProductLockApiTests(unittest.TestCase):
    setUp = fixtures.PreparationTests.setUp
    wait_operation = fixtures.PreparationTests.wait_operation

    def prepare_source(self):
        original = self.runtime.create_exploration({"idempotencyKey": "product-lock-genesis"})
        creation = self.wait_operation(original["operationId"])
        self.assertEqual(creation["state"], "succeeded", creation)
        reserved = creation["reserved"]
        with self.store.transaction() as tx:
            source = tx.get("sources", reserved["sourceId"])
            request = tx.get("requests", reserved["requestId"])
            request.update(state="ended", startedAt="2026-10-18T00:00:00+00:00", dispatchId="dispatch_fixture",
                           nonce="fixture_nonce", terminalReason="interrupted_cycle")
            request["receipt"] = {key: request[key] for key in ("requestId", "dispatchId", "entryId", "sourceId", "runtimeId", "nonce")}
            request["receipt"].update(state="terminal", cleanupConfirmed=True, launched=True,
                                      centerId=self.store.center_id, reason="interrupted_cycle")
            tx.put("requests", request["requestId"], request)
            tx.put("events", "event_fixture", {"requestId": request["requestId"], "actorKind": "runner", "toState": "ended", "reason": "interrupted_cycle",
                    "provenance": {"channel": "runner_receipt", "receiptId": "dispatch_fixture"}})
        return source, creation

    def test_product_lock_review_confirmation_requires_same_unused_token_and_genesis(self):
        source, creation = self.prepare_source()
        body = {"expectedRevision": source["revision"], "expectedSourceRevision": source["sourceRevision"], "idempotencyKey": "lock-review-fixture"}
        manifests = []
        def query(action, **kwargs):
            self.assertEqual(action, "product-lock-recovery")
            manifest = json.loads(Path(kwargs["manifest"]).read_text())
            manifests.append(manifest)
            if manifest["action"] == "review_product_lock":
                return {"ok": True, "lockReviewToken": "lock-proof"}
            return {"ok": True, "lockRecovered": True, "reviewRequired": True, "resumed": False}
        with patch.object(self.runtime.adapter, "query", query):
            reviewed = self.runtime.source_action(source["sourceId"], "review-product-lock", body)
            self.assertTrue(reviewed["canRecoverLock"])
            self.assertEqual(manifests[0]["genesisTarget"], source["root"])
            confirmed = self.runtime.source_action(source["sourceId"], "confirm-product-lock", {
                **body, "idempotencyKey": "lock-confirm-fixture", "lockReviewToken": "lock-proof",
                "confirmAllWritersStopped": True, "confirmLockRecoveryScope": True})
            with self.assertRaises(CenterError):
                self.runtime.source_action(source["sourceId"], "confirm-product-lock", {
                    **body, "idempotencyKey": "lock-confirm-again", "lockReviewToken": "lock-proof",
                    "confirmAllWritersStopped": True, "confirmLockRecoveryScope": True})
        self.assertTrue(confirmed["lockRecovered"])
        self.assertTrue(confirmed["reviewRequired"])
        self.assertFalse(confirmed["resumed"])
        self.assertEqual(len(manifests), 2)
        self.assertFalse(self.runtime.summary()["dispatchEnabled"])

    def test_historical_incompatible_runtime_offers_only_proven_empty_lock_review(self):
        source, creation = self.prepare_source()
        marker = json.loads((Path(source["root"]) / ".autonomous-company-center.json").read_text())
        payload = {"ok": True, "marker": marker, "compatible": False, "context": {"valid": False, "reason": "configuration_writer_active"},
                   "pauseReason": "interrupted_cycle", "productLock": {"emptyDirectory": True}}
        with patch.object(self.runtime.adapter, "query", return_value=payload):
            view = self.runtime.decorate_entry(source)
            self.assertTrue(view["capabilities"]["reviewProductLock"])
            self.assertFalse(view["capabilities"]["reviewRecovery"])
            self.assertFalse(view["capabilities"]["execute"])
            payload["productLock"]["emptyDirectory"] = False
            self.assertFalse(self.runtime.decorate_entry(source)["capabilities"]["reviewProductLock"])


if __name__ == "__main__":
    unittest.main()
