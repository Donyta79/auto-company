"""Explicit operator recovery of an ownerless historical product-state lock.

Historical control bytes are verified as evidence, never granted model execution.
An empty directory has no writer identity; the STOPPED assertion stays human.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/core"))
import center_runner as runner
import usage_lib

IDENTITIES = ("centerId", "runtimeId", "entryId", "sourceId")
RECOVERY_PROGRAM_FILES = ("scripts/core/usage.py", "scripts/core/usage_lib.py")
RECOVERY_BUNDLE_FILES = ("isolation_proxy.py", "usage_lib.py", "namespace_guard.py", "product_identity.py", "project_isolation.py",
                         "isolation_workspace.py", "isolation-runtime-files.json")
PRODUCT_RECOVERY_LOADER = """
import importlib.util, runpy, sys
from pathlib import Path
bundle = Path(sys.argv[1])
for name in ('usage_lib', 'namespace_guard'):
    spec = importlib.util.spec_from_file_location(name, bundle / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
sys.argv = [str(bundle / 'product_identity.py'), *sys.argv[2:]]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
ISOLATION_RECOVERY_LOADER = """
import importlib.util, sys
from pathlib import Path
root = Path(sys.argv[1])
bundle = Path(sys.argv[2])
for name in ('isolation_proxy', 'usage_lib', 'namespace_guard', 'product_identity', 'project_isolation', 'isolation_workspace'):
    spec = importlib.util.spec_from_file_location(name, bundle / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
sys.modules['isolation_workspace'].recover_collection(root)
"""


@contextmanager
def framework_repository(manifest, deadline):
    """Read a bridge-exported Windows worktree without changing its Git metadata."""
    if "frameworkBundle" not in manifest:
        yield Path(runner.__file__).resolve().parents[2]
        return
    source = Path(manifest["frameworkBundle"])
    head, expected = manifest.get("frameworkHead"), manifest.get("frameworkBundleSha256")
    if (not source.is_absolute() or source.name != "recovery-framework.bundle"
            or any(path.is_symlink() for path in (source, *source.parents))
            or not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head)
            or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
        raise ValueError("recovery_framework_bundle_invalid")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    with tempfile.TemporaryDirectory(prefix="center-recovery-git-") as directory:
        private = Path(directory)
        bundle, repository = private / "framework.bundle", private / "repository.git"
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as incoming, bundle.open("xb") as output:
            metadata = os.fstat(incoming.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("recovery_framework_bundle_invalid")
            checksum = hashlib.sha256()
            for block in iter(lambda: incoming.read(262144), b""):
                if time.monotonic() >= deadline:
                    raise ValueError("recovery_check_timeout")
                checksum.update(block)
                output.write(block)
            if checksum.hexdigest() != expected:
                raise ValueError("recovery_framework_bundle_changed")
        def git(*arguments):
            remaining = deadline - time.monotonic()
            if remaining < 1:
                raise ValueError("recovery_check_timeout")
            return subprocess.run(["git", "--no-replace-objects", "-c", "core.fsmonitor=false", *arguments],
                                  stdin=subprocess.DEVNULL, capture_output=True, env=environment,
                                  timeout=min(30, remaining), check=True).stdout
        git("init", "--bare", "--quiet", str(repository))
        git("-C", str(repository), "bundle", "unbundle", str(bundle))
        if git("-C", str(repository), "cat-file", "-t", head).strip() != b"commit":
            raise ValueError("recovery_framework_head_invalid")
        git("-C", str(repository), "update-ref", "HEAD", head)
        yield repository


@contextmanager
def trusted_programs(root, manifest, framework=None):
    """Execute immutable private copies from trusted Git, never a root module path."""
    framework = Path(framework) if framework is not None else Path(runner.__file__).resolve().parents[2]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    with tempfile.TemporaryDirectory(prefix="center-lock-recovery-") as directory:
        bundle = Path(directory)
        for name in RECOVERY_BUNDLE_FILES:
            relative = "scripts/core/" + name
            reference = subprocess.run(["git", "--no-replace-objects", "-c", "core.fsmonitor=false", "-C", str(framework),
                                        "show", manifest["frameworkRevision"] + ":" + relative],
                                       stdin=subprocess.DEVNULL, capture_output=True, env=environment, timeout=10, check=True).stdout
            path = runner.control_path(root, relative)
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or path.read_bytes() != reference:
                raise ValueError("recovery_program_changed")
            (bundle / name).write_bytes(reference)
        yield bundle


def verified(manifest, framework=None):
    root = Path(manifest["root"])
    proof = runner.recovery_provenance(root, manifest["centerId"], manifest["frameworkRevision"], manifest.get("genesisTarget"), framework)
    marker = proof["marker"]
    if marker != manifest["expectedMarker"] or any(marker.get(key) != manifest.get(key) for key in IDENTITIES):
        raise ValueError("ownership_changed")
    host_framework = Path(runner.__file__).resolve().parents[2]
    framework = Path(framework) if framework is not None else host_framework
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    for name in RECOVERY_PROGRAM_FILES:
        path = runner.control_path(root, name)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > 2 * 1024 * 1024:
            raise ValueError("recovery_program_changed")
        reference = subprocess.run(["git", "--no-replace-objects", "-c", "core.fsmonitor=false", "-C", str(framework),
                                    "show", manifest["frameworkRevision"] + ":" + name],
                                   stdin=subprocess.DEVNULL, capture_output=True, env=environment, timeout=10, check=True)
        if path.read_bytes() != reference.stdout:
            raise ValueError("recovery_program_changed")
        if name.endswith("usage_lib.py") and (host_framework / name).read_bytes() != reference.stdout:
            raise ValueError("recovery_budget_protocol_changed")
    return root.resolve(), marker


def empty_lock(root):
    path = runner.control_path(root, ".autonomous-company/product-state.lock")
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink() or any(path.iterdir()):
        raise ValueError("product_lock_not_empty_directory")
    return {"device": metadata.st_dev, "inode": metadata.st_ino,
            "mtimeNs": metadata.st_mtime_ns, "ctimeNs": metadata.st_ctime_ns}


def stopped_owner(root, marker, manifest):
    check = runner._quiescence_preflight(root, marker["centerId"], {"quiescent": False, "reason": None})
    if not check["quiescent"]:
        raise ValueError(check["reason"])
    probe = runner.probe(marker["centerId"], runtime_id=marker["runtimeId"])
    owner = probe.get("owner") or {}
    if (not probe["slotFree"] or probe["ownerAlive"] or owner.get("state") != "terminal"
            or not owner.get("cleanupConfirmed") or not owner.get("launched")
            or any(owner.get(key) != marker.get(key) for key in IDENTITIES)
            or owner.get("dispatchId") != manifest.get("expectedDispatchId")
            or owner.get("requestId") != manifest.get("expectedRequestId")
            or owner.get("nonce") != manifest.get("expectedNonce")
            or runner.same_process(owner.get("child"))
            or any(runner.same_process(value) for value in owner.get("ownedProcesses", []))):
        raise ValueError("owner_recovery_required")
    return owner


def readonly_budget(root):
    ledger = runner.control_path(root, "logs/usage.jsonl")
    for name in ("logs/usage.jsonl.pending", ".auto-loop-budget-paused.resume"):
        if runner.control_path(root, name).exists():
            raise ValueError("budget_unverifiable")
    configuration = usage_lib.load_budget_config()
    summary = usage_lib.summarize_usage(ledger, period=configuration["period"], target_date=date.today())
    if summary.get("invalid_records"):
        raise ValueError("budget_unverifiable")
    budget = usage_lib.evaluate_budget(summary, configuration)
    if budget["state"] in {"hard_limit", "unverifiable"}:
        raise ValueError("budget_pause")
    return budget


def other_guards(root):
    if runner.operator_previews(root):
        raise ValueError("preview_owner_unconfirmed")
    for name in (".autonomous-company.local.lock", ".autonomous-company.local.team-active", ".autonomous-company.local.language-update"):
        if runner.control_path(root, name).exists():
            raise ValueError("configuration_writer_active")
    if not runner.existing_lock_free(runner.control_path(root, ".autonomous-company/project-registry.lock")):
        raise ValueError("configuration_writer_active")
    if runner.control_path(root, ".auto-loop-stop-pending").exists():
        raise ValueError("stop_unconfirmed")
    if runner.state_pairs(root, ".auto-loop-paused").get("PAUSE_REASON") != "interrupted_cycle":
        raise ValueError("no_interrupted_pause")
    if runner.governance_reason(root):
        raise ValueError(runner.governance_reason(root))
    if runner.control_path(root, ".auto-loop-budget-paused").exists():
        raise ValueError("budget_pause")
    readonly_budget(root)


def collection_fingerprint(root):
    path = runner.control_path(root, ".autonomous-company/isolation-pending.json")
    if not path.exists():
        return None
    journal = runner.read_json(path)
    if journal.get("version") != 1 or journal.get("status") not in {"applying", "committed"} or not isinstance(journal.get("operations"), list):
        raise ValueError("invalid_collection_journal")
    value = hashlib.sha256(path.read_bytes())
    for operation in journal["operations"]:
        if not isinstance(operation, dict) or not isinstance(operation.get("started"), bool) or not isinstance(operation.get("existed"), bool):
            raise ValueError("invalid_collection_journal")
        for key in ("source", "target", "backup"):
            relative = operation[key]
            selected = runner.control_path(root, relative)
            if key in {"source", "backup"}:
                selected.relative_to(root / ".autonomous-company/isolation")
            elif re.fullmatch(r"logs/(cycle-[A-Za-z0-9_-]+)\.work\.json", relative):
                cycle = Path(relative).name.removesuffix(".work.json")
                from product_identity import read_state
                from cycle_reports import decode, LIMIT
                state = read_state(root)
                row = state["cycles"].get(cycle)
                owner = state["identities"].get(row["identityId"]) if row else None
                if (runner.state_pairs(root, ".auto-loop-state").get("PRODUCT_CYCLE_ID") != cycle
                        or not row or not owner or row.get("project") != owner.get("project")):
                    raise ValueError("invalid_collection_target")
                for item in (selected, runner.control_path(root, operation["source"]), runner.control_path(root, operation["backup"])):
                    if item.exists():
                        metadata = item.lstat()
                        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > LIMIT:
                            raise ValueError("unsafe_collection_path")
                        report = decode(item.read_text(encoding="utf-8"), cycle)
                        if report["project"] != row["project"]:
                            raise ValueError("invalid_collection_target")
            elif not (relative == "memories/consensus.md" or relative == ".autonomous-company/product-state.json"
                      or relative.startswith("projects/") or relative.startswith(".autonomous-company/isolation/")
                      or relative.startswith("logs/cycle-") and relative.endswith(".events.jsonl")
                      or re.fullmatch(r"logs/artifacts/[0-9a-f]{32}\.json", relative)):
                raise ValueError("invalid_collection_target")
            value.update(relative.encode())
            if not selected.exists():
                value.update(b"missing")
                continue
            paths = [selected]
            if selected.is_dir():
                for folder, directories, files in os.walk(selected, followlinks=False):
                    for name in directories:
                        if (Path(folder) / name).is_symlink():
                            raise ValueError("unsafe_collection_path")
                    directories.sort()
                    paths.extend(Path(folder) / name for name in sorted(files))
            for item in paths:
                if item.is_symlink():
                    raise ValueError("unsafe_collection_path")
                metadata = item.lstat()
                if not stat.S_ISDIR(metadata.st_mode) and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1):
                    raise ValueError("unsafe_collection_path")
                value.update(item.relative_to(root).as_posix().encode())
                if item.is_file():
                    with item.open("rb") as stream:
                        for block in iter(lambda: stream.read(262144), b""):
                            value.update(block)
    return value.hexdigest()


def fingerprint(root, marker, manifest, owner):
    transaction = runner.control_path(root, ".autonomous-company/product-state.transaction.json")
    if transaction.exists() and (transaction.is_symlink() or transaction.stat().st_size > runner.MAX_JSON):
        raise ValueError("unsafe_control_file")
    value = {"marker": marker, "lock": empty_lock(root), "owner": owner,
             "sourceRevision": manifest["sourceRevision"], "frameworkRevision": manifest["frameworkRevision"],
             "recovery": runner.recovery_fingerprint(root),
             "transaction": hashlib.sha256(transaction.read_bytes()).hexdigest() if transaction.exists() else None,
             "collection": collection_fingerprint(root), "budget": readonly_budget(root)}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def held_writers(root, marker):
    with runner.execution_lock(marker):
        descriptor = runner.lock_file(runner.control_path(root, ".auto-loop.pid"))
        registry = None
        try:
            # The original registry writer uses this same exclusive lock.
            registry = runner.lock_file(runner.control_path(root, ".autonomous-company/project-registry.lock"))
            yield
        finally:
            if registry is not None:
                os.close(registry)
            os.close(descriptor)


def _recover(manifest, framework, deadline):
    root, marker = verified(manifest, framework)
    if time.monotonic() >= deadline:
        raise ValueError("recovery_check_timeout")
    owner = stopped_owner(root, marker, manifest)
    # Checks before acquisition avoid treating this command's own EX locks as writers.
    other_guards(root)
    with held_writers(root, marker):
        if runner.marker_for(root) != marker:
            raise ValueError("ownership_changed")
        if runner.read_json(runner.owner_dir(marker) / "owner.json") != owner:
            raise ValueError("owner_recovery_required")
        for name in (".autonomous-company.local.lock", ".autonomous-company.local.team-active", ".autonomous-company.local.language-update"):
            if runner.control_path(root, name).exists():
                raise ValueError("configuration_writer_active")
        if runner.operator_previews(root) or runner.governance_reason(root):
            raise ValueError("recovery_conditions_changed")
        if runner.control_path(root, ".auto-loop-budget-paused").exists() or runner.control_path(root, ".auto-loop-stop-pending").exists():
            raise ValueError("recovery_conditions_changed")
        current = fingerprint(root, marker, manifest, owner)
        if time.monotonic() >= deadline:
            raise ValueError("recovery_check_timeout")
        if manifest["action"] == "review_product_lock":
            return {"ok": True, "lockReviewToken": secrets.token_hex(24), "expiresAt": time.time() + 900,
                    "fingerprint": current, "ownerDispatchId": owner["dispatchId"], "directoryOwnerKnown": False,
                    "resumed": False}
        review = manifest["review"]
        if (manifest["action"] != "confirm_product_lock" or manifest.get("lockReviewToken") != review["lockReviewToken"]
                or time.time() > review["expiresAt"] or current != review["fingerprint"]):
            raise ValueError("product_lock_review_changed")
        if manifest.get("confirmAllWritersStopped") is not True or manifest.get("confirmLockRecoveryScope") is not True:
            raise ValueError("human_stopped_confirmation_required")
        # subprocess.run kills and waits for this direct Python child on timeout.
        # Any partial recovery remains protected and requires a fresh review.
        environment = {**os.environ, "AUTONOMOUS_COMPANY_ROOT": str(root)}
        if environment.get("AUTONOMOUS_COMPANY_CYCLE") == "1":
            raise ValueError("cycle_writer_context")
        remaining = deadline - time.monotonic() - 5
        if remaining < 1:
            raise ValueError("recovery_check_timeout")
        with trusted_programs(root, manifest, framework) as bundle:
            remaining = deadline - time.monotonic() - 5
            if remaining < 1:
                raise ValueError("recovery_check_timeout")
            if runner.control_path(root, ".autonomous-company/isolation-pending.json").exists():
                subprocess.run([sys.executable, "-I", "-c", ISOLATION_RECOVERY_LOADER, str(root), str(bundle)], cwd=bundle, env=environment,
                               capture_output=True, text=True, timeout=min(30, remaining), check=True)
                if runner.control_path(root, ".autonomous-company/isolation-pending.json").exists():
                    raise ValueError("collection_recovery_unconfirmed")
            remaining = deadline - time.monotonic() - 5
            if remaining < 1:
                raise ValueError("recovery_check_timeout")
            subprocess.run([sys.executable, "-I", "-c", PRODUCT_RECOVERY_LOADER, str(bundle), "--root", str(root),
                            "recover-lock", "--confirm", "STOPPED"], cwd=bundle, env=environment,
                           capture_output=True, text=True, timeout=min(30, remaining), check=True)
        if runner.control_path(root, ".autonomous-company/product-state.lock").exists():
            raise ValueError("product_lock_recovery_unconfirmed")
        return {"ok": True, "lockRecovered": True, "reviewRequired": True, "resumed": False}


def recover(manifest):
    deadline = time.monotonic() + 90
    with framework_repository(manifest, deadline) as framework:
        return _recover(manifest, framework, deadline)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    arguments = parser.parse_args()
    try:
        print(json.dumps(recover(runner.read_json(arguments.manifest)), ensure_ascii=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        diagnostic = getattr(error, "stderr", None) or str(error)
        print("Product lock recovery refused: " + diagnostic.strip()[:2000], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
