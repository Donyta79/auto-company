"""Ownerless locks require human STOPPED consent, real OS locks and genesis."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
import center_recovery as recovery


@unittest.skipUnless(sys.platform == "linux", "Real POSIX writer locks and historical child; run under WSL")
class ProductLockRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.framework = self.base / "framework"
        self.framework.mkdir()
        for name in (*recovery.runner.CONTROL_FILES, *recovery.RECOVERY_PROGRAM_FILES):
            target = self.framework / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        self.git(self.framework, "init", "-q")
        self.git(self.framework, "add", ".")
        self.git(self.framework, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Historical")
        revision = self.git(self.framework, "rev-parse", "HEAD").strip()
        self.root = self.base / "data/runtimes/runtime_lockfixture"
        self.root.parent.mkdir(parents=True)
        self.git(self.base, "clone", "-q", str(self.framework), str(self.root))
        with (self.framework / recovery.runner.CONTROL_FILES[0]).open("ab") as stream:
            stream.write(b"\n# Different current execution framework\n")
        self.git(self.framework, "add", ".")
        self.git(self.framework, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Current")
        marker = {"protocolVersion": 2, "centerId": "center_lockfixture", "runtimeId": "runtime_lockfixture",
                  "entryId": "entry_lockfixture", "sourceId": "source_lockfixture", "operationId": "operation_lockfixture"}
        recovery.runner.atomic_json(self.root / recovery.runner.MARKER, marker)
        recovery.runner.atomic_json(self.base / "data/staging/operation_lockfixture/intent.json",
                                   {"centerId": marker["centerId"], "operationId": marker["operationId"],
                                    "reserved": {key: marker[key] for key in ("runtimeId", "entryId", "sourceId")},
                                    "frameworkRevision": revision, "target": str(self.root)})
        self.lock = self.root / ".autonomous-company/product-state.lock"
        self.lock.mkdir(parents=True)
        (self.root / "memories").mkdir()
        (self.root / "memories/consensus.md").write_text("# Consensus\n## Human Overrides\n\n## Priority Issues\n\n")
        (self.root / ".auto-loop-paused").write_text("PAUSE_REASON=interrupted_cycle\n")
        self.manifest = {**marker, "root": str(self.root), "expectedMarker": marker, "frameworkRevision": revision,
                         "sourceRevision": "source-revision-1", "expectedRequestId": "request_lockfixture",
                         "expectedDispatchId": "dispatch_lockfixture", "expectedNonce": "fixture-nonce-1",
                         "action": "review_product_lock"}
        self.environment = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "state"), "USAGE_HARD_LIMIT_TOKENS": "",
                                                   "USAGE_HARD_LIMIT_USD": "", "AUTONOMOUS_COMPANY_CYCLE": "0"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.location = patch.object(recovery.runner, "__file__", str(self.framework / "scripts/core/center_runner.py"))
        self.location.start()
        self.addCleanup(self.location.stop)
        self.owner = {**marker, "requestId": "request_lockfixture", "dispatchId": "dispatch_lockfixture", "nonce": "fixture-nonce-1",
                      "state": "terminal", "cleanupConfirmed": True, "launched": True, "ownedProcesses": []}
        self.owner_path = recovery.runner.owner_dir(marker) / "owner.json"
        recovery.runner.atomic_json(self.owner_path, self.owner)

    def git(self, root, *args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True, timeout=10).stdout

    def confirm(self, review, **updates):
        return recovery.recover({**self.manifest, "action": "confirm_product_lock", "review": review,
                                 "lockReviewToken": review["lockReviewToken"], "confirmAllWritersStopped": True,
                                 "confirmLockRecoveryScope": True, **updates})

    def test_historical_old_marker_human_confirmation_never_grants_execution(self):
        pause = (self.root / ".auto-loop-paused").read_bytes()
        marker = (self.root / recovery.runner.MARKER).read_bytes()
        self.assertFalse(recovery.runner.compatible(self.root))
        review = recovery.recover(self.manifest)
        self.assertFalse(review["directoryOwnerKnown"])
        self.assertTrue(self.lock.is_dir())
        with self.assertRaisesRegex(ValueError, "human_stopped_confirmation_required"):
            self.confirm(review, confirmAllWritersStopped=False)
        self.assertTrue(self.lock.is_dir())
        result = self.confirm(review)
        self.assertTrue(result["lockRecovered"])
        self.assertFalse(result["resumed"])
        self.assertFalse(self.lock.exists())
        self.assertEqual((self.root / ".auto-loop-paused").read_bytes(), pause)
        self.assertEqual((self.root / recovery.runner.MARKER).read_bytes(), marker)
        self.assertEqual(recovery.runner.preflight(self.root, require_context=False)["reason"], "runtime_incompatible")
        with self.assertRaises(FileNotFoundError):
            self.confirm(review)

    def test_changed_lock_transaction_ledger_or_expired_review_refuses(self):
        review = recovery.recover(self.manifest)
        for name in ("logs/usage.jsonl", ".autonomous-company/product-state.transaction.json", "memories/consensus.md"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            old = path.read_bytes() if path.exists() else None
            path.write_bytes((old or b"") + b"\n")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "product_lock_review_changed"):
                self.confirm(review)
            if old is None:
                path.unlink()
            else:
                path.write_bytes(old)
        with self.assertRaisesRegex(ValueError, "product_lock_review_changed"):
            self.confirm({**review, "expiresAt": 0})
        self.lock.rmdir()
        self.lock.mkdir()
        with self.assertRaisesRegex(ValueError, "product_lock_review_changed"):
            self.confirm(review)

    def test_original_writer_gates_and_governance_are_not_relaxed(self):
        for path in (self.root / ".auto-loop.pid", recovery.runner.owner_dir(self.manifest) / "slot.lock",
                     self.root / ".autonomous-company/project-registry.lock"):
            descriptor = recovery.runner.lock_file(path)
            try:
                with self.subTest(path=path), self.assertRaises((ValueError, BlockingIOError)):
                    recovery.recover(self.manifest)
            finally:
                os.close(descriptor)
        for name in (".autonomous-company.local.lock", ".autonomous-company.local.team-active", ".auto-loop-budget-paused"):
            path = self.root / name
            path.mkdir()
            with self.subTest(name=name), self.assertRaises(ValueError):
                recovery.recover(self.manifest)
            path.rmdir()
        with (self.root / "memories/consensus.md").open("a") as stream:
            stream.write("- P1 unresolved operator decision\n")
        with self.assertRaisesRegex(ValueError, "unresolved_p1"):
            recovery.recover(self.manifest)
        self.assertTrue(self.lock.is_dir())

    def test_nonempty_link_and_changed_control_are_never_executed(self):
        (self.lock / "writer").write_text("unknown")
        with self.assertRaisesRegex(ValueError, "product_lock_not_empty_directory"):
            recovery.recover(self.manifest)
        (self.lock / "writer").unlink()
        self.lock.rmdir()
        self.lock.symlink_to(self.root / "memories", target_is_directory=True)
        with self.assertRaises(ValueError):
            recovery.recover(self.manifest)
        self.lock.unlink()
        self.lock.mkdir()
        (self.root / "scripts/core/product_identity.py").write_text("raise RuntimeError('MUST_NOT_EXECUTE')")
        with self.assertRaisesRegex(ValueError, "recovery_control_changed"):
            recovery.recover(self.manifest)

    def test_pending_collection_recovers_original_backup_without_import_shadow(self):
        sentinel = self.base / "MUST_NOT_EXECUTE"
        payload = "from pathlib import Path\nPath(" + repr(str(sentinel)) + ").write_text('unsafe')\nraise RuntimeError('shadow')\n"
        for name in ("json.py", "sitecustomize.py"):
            (self.root / "scripts/core" / name).write_text(payload)
        ready = self.root / ".autonomous-company/isolation/fixture/ready"
        ready.mkdir(parents=True)
        target = self.root / "projects/notes/report.txt"
        target.parent.mkdir(parents=True)
        target.write_text("candidate")
        backup = ready / "previous-0"
        backup.write_text("original")
        source = ready / "report.txt"
        journal = {"version": 1, "status": "applying", "operations": [{"started": True, "existed": True,
                   **{key: str(path.relative_to(self.root)) for key, path in (("source", source), ("target", target), ("backup", backup))}}]}
        pending = self.root / ".autonomous-company/isolation-pending.json"
        recovery.runner.atomic_json(pending, journal)
        review = recovery.recover(self.manifest)
        self.assertTrue(pending.exists())
        self.assertEqual(target.read_text(), "candidate")
        self.confirm(review)
        self.assertEqual(target.read_text(), "original")
        self.assertEqual(source.with_name(source.name + ".rejected").read_text(), "candidate")
        self.assertFalse(pending.exists())
        self.assertTrue(list(pending.parent.glob("isolation-recovered-*.json")))
        self.assertFalse(sentinel.exists())

    def test_budget_review_never_recovers_pending_usage_or_consumes_resume(self):
        for name in ("logs/usage.jsonl.pending", ".auto-loop-budget-paused.resume"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            original = b'{"unknown":true}\n'
            path.write_bytes(original)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "budget_unverifiable"):
                recovery.recover(self.manifest)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((self.root / "logs/usage.jsonl").exists())
            self.assertTrue(self.lock.is_dir())
            path.unlink()

    def test_usage_dependency_is_pinned_and_missing_or_changed_bytes_fail_closed(self):
        review = recovery.recover(self.manifest)
        with recovery.trusted_programs(self.root, self.manifest) as bundle:
            self.assertEqual((bundle / "usage_lib.py").read_bytes(), (self.root / "scripts/core/usage_lib.py").read_bytes())
        program = self.root / "scripts/core/usage_lib.py"
        original = program.read_bytes()
        for replacement in (None, b"raise RuntimeError('must never execute')\n"):
            if replacement is None:
                program.unlink()
            else:
                program.write_bytes(replacement)
            with self.subTest(replacement=replacement), self.assertRaises((OSError, ValueError)):
                self.confirm(review)
            self.assertTrue(self.lock.is_dir())
            program.write_bytes(original)
        host = self.framework / "scripts/core/usage_lib.py"
        host.write_bytes(original + b"\n# changed host budget protocol\n")
        with self.assertRaisesRegex(ValueError, "recovery_budget_protocol_changed"):
            recovery.recover(self.manifest)

    def test_provenance_rejects_wrong_genesis_marker_and_linked_control(self):
        with self.assertRaisesRegex(ValueError, "unsafe_recovery_path"):
            recovery.recover({**self.manifest, "root": "relative-runtime"})
        genesis = self.base / "data/staging/operation_lockfixture/intent.json"
        original = genesis.read_bytes()
        value = json.loads(original)
        for changed in ({**value, "frameworkRevision": "0" * 40}, {**value, "target": str(self.base / "other")},
                        {**value, "reserved": {**value["reserved"], "sourceId": "source_unrelated"}}):
            recovery.runner.atomic_json(genesis, changed)
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "recovery_genesis"):
                recovery.recover(self.manifest)
            self.assertTrue(self.lock.is_dir())
        genesis.write_bytes(original)
        parser = self.root / "scripts/core/cycle_reports.py"
        saved = parser.read_bytes()
        parser.write_bytes(saved + b"\n# parser changed\n")
        with self.assertRaisesRegex(ValueError, "recovery_control_changed"):
            recovery.recover(self.manifest)
        parser.unlink()
        parser.symlink_to(self.framework / "scripts/core/cycle_reports.py")
        with self.assertRaises(ValueError):
            recovery.recover(self.manifest)
        parser.unlink()
        parser.write_bytes(saved)
        with self.assertRaisesRegex(ValueError, "ownership_changed"):
            recovery.recover({**self.manifest, "sourceId": "source_unrelated"})

    def test_bridge_bundle_recovers_without_reading_host_worktree_git_metadata(self):
        operation = self.base / "operation"
        operation.mkdir()
        bundle = operation / "recovery-framework.bundle"
        self.git(self.framework, "bundle", "create", str(bundle), "HEAD")
        self.manifest.update(frameworkBundle=str(bundle), frameworkHead=self.git(self.framework, "rev-parse", "HEAD").strip(),
                             frameworkBundleSha256=hashlib.sha256(bundle.read_bytes()).hexdigest())
        # Simulate the host worktree's unusable Windows admin pointer in Linux.
        # The committed-object copy is the only Git source for historical proof.
        (self.framework / ".git").rename(self.framework / ".git-preserved")
        (self.framework / ".git").write_text("gitdir: C:/unavailable/windows/admin\n")
        review = recovery.recover(self.manifest)
        saved = bundle.read_bytes()
        bundle.write_bytes(saved + b"tampered")
        with self.assertRaisesRegex(ValueError, "recovery_framework_bundle_changed"):
            self.confirm(review)
        self.assertTrue(self.lock.is_dir())
        bundle.write_bytes(saved)
        result = self.confirm(review)
        self.assertTrue(result["lockRecovered"])
        self.assertFalse(result["resumed"])
        self.assertEqual((self.framework / ".git").read_text(), "gitdir: C:/unavailable/windows/admin\n")

    def test_collection_work_report_scope_and_rollback(self):
        from product_identity import reserve_cycle, update_cycle
        from cycle_reports import write_report
        self.lock.rmdir()
        row = reserve_cycle(self.root, "", "attempt-recovery-report", 1, "codex", "fixture")
        update_cycle(self.root, row["cycleId"], "dispatching")
        self.lock.mkdir()
        cycle = row["cycleId"]
        (self.root / ".auto-loop-state").write_text("PRODUCT_CYCLE_ID=" + cycle + "\n")
        write_report(self.root, cycle, {"title": "Report", "summary": "Current exploration", "blocker": "", "phase": "review", "final": True})
        target = self.root / "logs" / (cycle + ".work.json")
        original = target.read_bytes()
        ready = self.root / ".autonomous-company/isolation/fixture/ready"
        ready.mkdir(parents=True)
        source, backup = ready / "report.json", ready / "previous-0"
        pending = self.root / ".autonomous-company/isolation-pending.json"
        recovery.runner.atomic_json(pending, {"version": 1, "status": "applying", "operations": [{"started": True, "existed": False,
            **{key: str(path.relative_to(self.root)) for key, path in (("source", source), ("target", target), ("backup", backup))}}]})
        review = recovery.recover(self.manifest)
        report = json.loads(original)
        target.write_text(json.dumps({**report, "project": "projects/unrelated"}))
        with self.assertRaisesRegex(ValueError, "invalid_collection_target"):
            self.confirm(review)
        target.write_bytes(original)
        result = self.confirm(review)
        self.assertTrue(result["lockRecovered"])
        self.assertFalse(target.exists())
        self.assertEqual(source.read_bytes(), original)
        self.assertTrue((self.root / ".auto-loop-paused").exists())

    def test_original_child_failure_or_timeout_keeps_protection_and_releases_os_locks(self):
        review = recovery.recover(self.manifest)
        original = subprocess.run
        def refuse_child(command, **kwargs):
            if "recover-lock" in command:
                self.assertEqual(command[1], "-I")
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return original(command, **kwargs)
        pause = (self.root / ".auto-loop-paused").read_bytes()
        with patch.object(recovery.subprocess, "run", side_effect=refuse_child), self.assertRaises(subprocess.TimeoutExpired):
            self.confirm(review)
        self.assertTrue(self.lock.is_dir())
        self.assertEqual((self.root / ".auto-loop-paused").read_bytes(), pause)
        self.assertTrue(recovery.runner.existing_lock_free(self.root / ".auto-loop.pid"))
        self.assertTrue(recovery.runner.lock_free(recovery.runner.owner_dir(self.manifest) / "slot.lock"))


if __name__ == "__main__":
    unittest.main()
