"""Selected-domain probes and original-identity preparation retries; no models."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import urllib.request
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_center_runtime as fixtures
import test_center_server as http_fixtures
import center_environment
from center_runtime import CenterError, atomic_json


class EnvironmentPreparationTests(unittest.TestCase):
    setUp = fixtures.PreparationTests.setUp
    wait_operation = fixtures.PreparationTests.wait_operation

    def batch(self):
        with patch.object(self.runtime, "_worker"):
            result = self.runtime.create_batch({"idempotencyKey": "batch-original", "executionMode": "start_now", "groups": [
                {"count": 2, "direction": "First direction", "config": {"engine": "codex", "model": "fixture-a", "effort": "high", "productLanguage": "en"}},
                {"count": 2, "direction": "第二方向", "config": {"engine": "claude", "model": "fixture-b", "effort": "medium", "productLanguage": "zh-CN"}}]})
        with self.store.transaction() as tx:
            for index, row in enumerate(result["items"]):
                row.update(state="failed", reason="fixture_failure", revision=2)
                if index == 0:
                    row.update(state="succeeded", result=row["reserved"])
                tx.put("operations", row["operationId"], row)
        return result["items"]

    def test_enumeration_lists_names_without_starting_any_distribution(self):
        runner = Mock(return_value=Mock(stdout="Ubuntu\r\nOther\r\n".encode("utf-16-le")))
        with patch.object(center_environment.os, "name", "nt"), patch.object(center_environment.subprocess, "run", runner):
            result = center_environment.candidates()
        self.assertEqual([item["domain"]["distribution"] for item in result], ["Ubuntu", "Other"])
        self.assertTrue(all(item["requiresUser"] and "user" not in item["domain"] for item in result))
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.args[0], ["wsl.exe", "--list", "--quiet"])

    def test_explicit_check_probes_only_selected_user_and_domain(self):
        adapter = fixtures.PosixAdapter(self.runtime.framework_root, {"platform": "wsl", "distribution": "Chosen", "user": "chosen-user"})
        with patch.object(adapter, "_execute", return_value=Mock(stdout="ok")) as execute:
            checks = center_environment.check(adapter)
        self.assertTrue(all(item["ok"] for item in checks))
        self.assertEqual(execute.call_count, 2)
        for call in execute.call_args_list:
            self.assertEqual(call.args[0][:6], ["wsl.exe", "--distribution", "Chosen", "--user", "chosen-user", "--exec"])

    def test_check_is_read_only_and_environment_revision_is_strict(self):
        domain = {"platform": "wsl", "distribution": "Chosen", "user": "user"}
        before = self.store.revision
        with patch("center_runtime.center_environment.check", return_value=[{"code": "python", "ok": True}]):
            result = self.runtime.environment_action("check", {"domain": domain, "expectedRevision": 1})
            self.assertTrue(result["ready"])
            for revision in (None, True, "1", 0):
                with self.assertRaises(CenterError):
                    self.runtime.environment_action("check", {"domain": domain, "expectedRevision": revision})
        self.assertEqual(self.store.revision, before)
        self.assertEqual(self.runtime.domain, {"platform": "posix"})

    def test_connect_is_idempotent_pauses_queue_and_does_not_start_work(self):
        body = {"domain": {"platform": "wsl", "distribution": "Chosen", "user": "user"}, "expectedRevision": 1, "idempotencyKey": "connect-selected"}
        with self.store.transaction() as tx:
            queue = tx.get("controls", "queue")
            queue["dispatchEnabled"] = True
            tx.put("controls", "queue", queue)
        with patch("center_runtime.center_environment.check", return_value=[{"code": "python", "ok": True}]) as check:
            first = self.runtime.environment_action("connect", body)
            second = self.runtime.environment_action("connect", body)
        self.assertEqual(first, second)
        self.assertEqual(first["revision"], 2)
        self.assertEqual(check.call_count, 1)
        self.assertFalse(self.runtime.summary()["dispatchEnabled"])
        with self.store.transaction() as tx:
            self.assertEqual(tx.list("requests"), [])

    def test_connection_cannot_move_existing_runtimes_or_preparations(self):
        body = {"domain": {"platform": "wsl", "distribution": "Chosen", "user": "user"}, "expectedRevision": 1, "idempotencyKey": "connect-conflict"}
        with self.store.transaction() as tx:
            tx.put("runtimes", "runtime_existing", {"runtimeId": "runtime_existing", "executionDomain": {"platform": "posix"}})
        with patch("center_runtime.center_environment.check") as check, self.assertRaises(CenterError) as error:
            self.runtime.environment_action("connect", body)
        self.assertEqual(error.exception.code, "EXECUTION_DOMAIN_CONFLICT")
        check.assert_not_called()
        with self.store.transaction() as tx:
            tx.delete("runtimes", "runtime_existing")
        with patch.object(self.runtime, "_worker"):
            self.runtime.create_exploration({"idempotencyKey": "preparing-conflict"})
        with patch("center_runtime.center_environment.check") as check, self.assertRaises(CenterError):
            self.runtime.environment_action("connect", body)
        check.assert_not_called()

    def test_preflight_checks_each_effective_model_not_only_engine(self):
        query = Mock(return_value={"ready": True, "checks": [{"name": "fixture", "ok": True}]})
        with patch.object(self.runtime.adapter, "query", query):
            result = self.runtime.preflight({"environmentRevision": 1, "groups": [
                {"config": {"engine": "codex", "model": model, "effort": "high", "productLanguage": "en"}}
                for model in ("fixture-a", "fixture-b", "fixture-a")]})
        self.assertTrue(result["ready"])
        self.assertEqual(query.call_count, 2)
        self.assertEqual([call.kwargs["model"] for call in query.call_args_list], ["fixture-a", "fixture-b"])
        self.assertTrue(all(call.kwargs["effort"] == "high" for call in query.call_args_list))

    def test_preflight_dirty_framework_and_missing_dependencies_create_nothing(self):
        (self.runtime.framework_root / "PROMPT.md").write_text("uncommitted", encoding="utf-8")
        with patch.object(self.runtime.adapter, "query", return_value={"ready": False, "checks": []}):
            result = self.runtime.preflight({"groups": [{}]})
        self.assertFalse(result["ready"])
        self.assertIn("FRAMEWORK_UNVERIFIED", [item["code"] for item in result["checks"]])
        self.assertIn("DEPENDENCIES_UNAVAILABLE", [item["code"] for item in result["checks"]])
        with self.store.transaction() as tx:
            self.assertEqual(tx.list("operations"), [])
            self.assertEqual(tx.list("requests"), [])

    def test_environment_change_between_check_and_submit_rejects_batch(self):
        with patch.object(self.runtime, "_worker") as worker, self.assertRaises(CenterError):
            self.runtime.create_batch({"idempotencyKey": "stale-environment", "environmentRevision": 0, "groups": [{"count": 1}]})
        worker.assert_not_called()

    def test_restore_original_groups_remaining_counts_and_no_authorization(self):
        rows = self.batch()
        before = self.store.revision
        result = self.runtime.operation_action(rows[1]["operationId"], "restore-draft", {"expectedRevision": 2})
        self.assertEqual([item["count"] for item in result["draft"]["groups"]], [1, 2])
        self.assertEqual([item["direction"] for item in result["draft"]["groups"]], ["First direction", "第二方向"])
        self.assertEqual([item["config"]["model"] for item in result["draft"]["groups"]], ["fixture-a", "fixture-b"])
        self.assertTrue(all("configRevision" not in item["config"] for item in result["draft"]["groups"]))
        self.assertEqual(result["draft"]["executionMode"], "enqueue")
        self.assertEqual(result["completedCount"], 1)
        self.assertEqual(self.store.revision, before)

    def test_retry_preserves_ids_skips_success_and_is_idempotent(self):
        rows = self.batch()
        body = {"expectedRevision": 2, "idempotencyKey": "retry-original"}
        with patch.object(self.runtime, "_worker") as worker:
            result = self.runtime.operation_action(rows[1]["operationId"], "retry", body)
            repeated = self.runtime.operation_action(rows[1]["operationId"], "retry", body)
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(worker.call_count, 1)
        self.assertEqual([row["reserved"] for row in result["items"]], [row["reserved"] for row in rows[1:]])
        self.assertEqual(result, repeated)
        self.assertTrue(all(row["authorizationRevoked"] and row["authorizationSession"] is None for row in result["items"]))
        self.assertEqual(self.runtime.operation(rows[0]["operationId"]), rows[0])

    def test_cancel_archive_preserve_error_identity_and_staging_but_remove_attention(self):
        rows = self.batch()
        self.assertEqual(self.runtime.summary()["attentionCount"], 3)
        for action, row in zip(("cancel", "archive"), rows[1:3]):
            staging = self.runtime._preparation_staging(row)
            staging.mkdir(parents=True)
            (staging / "evidence").write_text("keep", encoding="utf-8")
            result = self.runtime.operation_action(row["operationId"], action, {"expectedRevision": 2, "idempotencyKey": action + "-preparation"})["items"][0]
            self.assertEqual(result["reason"], row["reason"])
            self.assertEqual(result["reserved"], row["reserved"])
            self.assertTrue(result["archived"])
            self.assertEqual((staging / "evidence").read_text(), "keep")
            with self.assertRaises(CenterError):
                self.runtime.operation_action(row["operationId"], "restore-draft", {"expectedRevision": result["revision"]})
        self.assertEqual(self.runtime.summary()["attentionCount"], 1)

    def failed_clone(self):
        with patch.object(self.runtime.adapter, "clone", side_effect=OSError("fixture clone failure")):
            row = self.runtime.create_exploration({"idempotencyKey": "clone-failure-original", "executionMode": "start_now"})
            row = self.wait_operation(row["operationId"])
        return row

    def commit_framework_change(self):
        framework = self.runtime.framework_root
        (framework / "PROMPT.md").write_text("Fixed fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(framework), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(framework), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "repair"], check=True, capture_output=True)

    def test_framework_repair_uses_new_attempt_with_original_ids_and_old_evidence(self):
        row = self.failed_clone()
        old_intent = self.runtime._preparation_staging(row) / "intent.json"
        old_bytes = old_intent.read_bytes()
        self.commit_framework_change()
        result = self.runtime.operation_action(row["operationId"], "retry", {"expectedRevision": row["revision"], "idempotencyKey": "retry-framework"})
        final = self.wait_operation(row["operationId"])
        self.assertEqual(final["state"], "succeeded", final)
        self.assertEqual(final["reserved"], row["reserved"])
        self.assertEqual(old_intent.read_bytes(), old_bytes)
        self.assertEqual(len(final["preparationHistory"]), 1)
        self.assertNotEqual(final["frameworkRevision"], row["frameworkRevision"])
        target = self.runtime.data_dir / "runtimes" / row["reserved"]["runtimeId"]
        marker = json.loads((target / ".autonomous-company-center.json").read_text())
        with self.store.transaction() as tx:
            runtime = tx.get("runtimes", row["reserved"]["runtimeId"])
            request = tx.get("requests", row["reserved"]["requestId"])
        self.assertEqual(marker["preparationAttempt"], final["preparationAttempt"])
        self.assertEqual(runtime["preparationAttempt"], final["preparationAttempt"])
        self.assertFalse(request["startAuthorized"])

    def test_tampered_intent_and_published_target_cannot_retry(self):
        row = self.failed_clone()
        intent = self.runtime._preparation_staging(row) / "intent.json"
        atomic_json(intent, {"operationId": "someone_else"})
        self.commit_framework_change()
        with patch.object(self.runtime, "_worker") as worker, self.assertRaises(CenterError) as error:
            self.runtime.operation_action(row["operationId"], "retry", {"expectedRevision": row["revision"], "idempotencyKey": "retry-tampered"})
        self.assertEqual(error.exception.code, "RECOVERY_REQUIRED")
        worker.assert_not_called()
        target = self.runtime.data_dir / "runtimes" / row["reserved"]["runtimeId"]
        target.mkdir(parents=True)
        with self.assertRaises(CenterError):
            self.runtime.operation_action(row["operationId"], "retry", {"expectedRevision": row["revision"], "idempotencyKey": "retry-published"})
        self.assertEqual(self.runtime.operation(row["operationId"]), row)

    @unittest.skipUnless(os.name == "nt", "Native Windows Git bridge export")
    def test_windows_worktree_exports_anchored_recovery_bundle_without_rewriting_metadata(self):
        worktree = self.folder / "windows-worktree"
        subprocess.run(["git", "-C", str(self.runtime.framework_root), "worktree", "add", "--detach", str(worktree), "HEAD"], check=True, capture_output=True)
        before = (worktree / ".git").read_bytes()
        head = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        manifest = self.folder / "recovery-operation" / "manifest.json"
        atomic_json(manifest, {"frameworkHead": head})
        adapter = fixtures.PosixAdapter(worktree, {"platform": "wsl", "distribution": "Fixture", "user": "fixture"})
        def execute(command, **kwargs):
            if command[0] == "git":
                return subprocess.run(command, capture_output=True, text=True, check=True)
            return subprocess.CompletedProcess(command, 0, stdout='{"ok":true}')
        with patch.object(adapter, "_execute", side_effect=execute), patch.object(adapter, "path", side_effect=lambda path: "/bridge/" + Path(path).name):
            self.assertTrue(adapter.query("product-lock-recovery", manifest=manifest)["ok"])
        output = json.loads(manifest.read_text())
        self.assertEqual(output["frameworkBundle"], "/bridge/recovery-framework.bundle")
        self.assertEqual(len(output["frameworkBundleSha256"]), 64)
        self.assertEqual((worktree / ".git").read_bytes(), before)


class EnvironmentHttpTests(unittest.TestCase):
    setUp = http_fixtures.CenterHttpTests.setUp
    tearDown = http_fixtures.CenterHttpTests.tearDown
    request = http_fixtures.CenterHttpTests.request

    def test_new_routes_keep_same_origin_envelope_and_operator_provenance(self):
        self.runtime.environment.return_value = {"domain": None, "revision": 0, "candidates": []}
        status, raw, _ = self.request("GET", "/api/center/v1/environment")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["data"]["revision"], 0)
        for route, method, args in (("environment/check", "environment_action", ("check",)),
                                    ("environment/connect", "environment_action", ("connect",)),
                                    ("preflight", "preflight", ()),
                                    ("operations/operation_fixture/restore-draft", "operation_action", ("operation_fixture", "restore-draft")),
                                    ("operations/operation_fixture/retry", "operation_action", ("operation_fixture", "retry"))):
            endpoint = getattr(self.runtime, method)
            endpoint.return_value = {"ready": True}
            body = {"expectedRevision": 2, "idempotencyKey": "http-fixture", "_provenance": {"channel": "runner"}}
            status, raw, _ = self.request("POST", "/api/center/v1/" + route, json.dumps(body))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["centerId"], "test-center")
            endpoint.assert_called_with(*args, {**body, "_provenance": {"channel": "local_api"}})
            endpoint.reset_mock()
            status, _, _ = self.request("POST", "/api/center/v1/" + route, "{}", {"Origin": "https://evil.invalid"})
            self.assertEqual(status, 403)
            endpoint.assert_not_called()

    def test_recovery_routes_preserve_source_versions_and_operator_provenance(self):
        self.runtime.source_action.return_value = {"ok": True, "resumed": False}
        for action in ("review-recovery", "confirm-recovery", "review-product-lock", "confirm-product-lock"):
            body = {"expectedRevision": 4, "expectedSourceRevision": 3, "idempotencyKey": "fixture-recovery"}
            status, raw, _ = self.request("POST", "/api/center/v1/sources/source_fixture/" + action, json.dumps(body))
            self.assertEqual(status, 202)
            self.assertFalse(json.loads(raw)["data"]["resumed"])
            self.runtime.source_action.assert_called_with("source_fixture", action, {**body, "_provenance": {"channel": "local_api"}})


class BrowserFixturePreflightTests(unittest.TestCase):
    def test_real_preflight_finishes_while_server_waits_on_stdin_pipe(self):
        script = Path(__file__).resolve().parent / "browser/center-fixture-server.py"
        child = subprocess.Popen([sys.executable, "-X", "utf8", "-u", str(script)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        try:
            fixture = json.loads(child.stdout.readline())
            for model, expected in (("fixture-model", True), ("fixture-missing-dependency", False)):
                request = urllib.request.Request(fixture["url"] + "/api/center/v1/preflight", data=json.dumps({
                    "groups": [{"count": 1, "config": {"engine": "codex", "model": model, "effort": "high", "productLanguage": "en"}}]
                }).encode(), headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    result = json.loads(response.read())["data"]
                self.assertEqual(result["ready"], expected)
                self.assertTrue(next(row for row in result["checks"] if row["code"] == "framework")["ok"])
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            child.stdout.close()


if __name__ == "__main__":
    unittest.main()
