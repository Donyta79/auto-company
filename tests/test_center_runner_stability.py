"""Current-loop observations and real POSIX cleanup ownership, no models."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from isolation_fixture import install as install_fixture_boundary
sys.path.insert(0, str(ROOT / "scripts/core"))
from center_runner import atomic_json


def copy_control_entrypoints(root):
    install_fixture_boundary(root)
    for name in ("dashboard/server.py", "Makefile", "scripts/macos/start-daemon.sh", "scripts/macos/install-daemon.sh",
                 "scripts/wsl/dashboard-wsl.sh", "scripts/wsl/install-wsl-daemon.sh", "scripts/windows/start-win.ps1", "scripts/windows/stop-win.ps1"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)


class LoopStateTests(unittest.TestCase):
    def test_current_state_filter_preserves_confirmed_p1_until_valid_transition(self):
        sys.path.insert(0, str(ROOT / "scripts/core"))
        from center_runner import execution_blocked_reason
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / ".auto-loop-state"
            consensus = root / "memories/consensus.md"
            consensus.parent.mkdir()
            consensus.write_text("## Human Overrides\n## Priority Issues\n- [ ] P1: original issue\n")
            blocked = "unresolved_p1"
            for content in (None, "", "LOOP_COUNT=2\n", "STATUS=paused\n"):
                with self.subTest(content=content):
                    if content is not None:
                        state.write_text(content)
                    self.assertEqual(execution_blocked_reason(root, 0, blocked), blocked)
                    self.assertIsNone(execution_blocked_reason(root, 0))
            state.write_text("STATUS=paused\nPAUSE_REASON=unresolved_p1\n")
            self.assertEqual(execution_blocked_reason(root, time.time_ns() + 1_000_000_000, blocked), blocked)
            with patch.object(Path, "read_text", side_effect=OSError("injected unreadable state")):
                self.assertEqual(execution_blocked_reason(root, 0, blocked), blocked)
            consensus.write_text("invalid consensus fixture")
            self.assertEqual(execution_blocked_reason(root, 0, blocked), blocked)
            consensus.write_text("## Human Overrides\n## Priority Issues\n- [ ] P1: original issue\n")
            self.assertEqual(execution_blocked_reason(root, 0), blocked)
            state.write_text("STATUS=running\nPAUSE_REASON=\n")
            self.assertIsNone(execution_blocked_reason(root, 0, blocked))

    def test_current_loop_state_rejects_stale_unsafe_and_changing_files(self):
        sys.path.insert(0, str(ROOT / "scripts/core"))
        from center_runner import current_loop_state
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / ".auto-loop-state"
            self.assertIsNone(current_loop_state(root, 0))
            path.write_text("STATUS=waiting_limit\n")
            self.assertEqual(current_loop_state(root, 0), {"STATUS": "waiting_limit"})
            self.assertIsNone(current_loop_state(root, path.stat().st_mtime_ns + 1))
            original = Path.read_text
            def changed(file, *args, **kwargs):
                text = original(file, *args, **kwargs)
                os.utime(file, ns=(1, 1))
                return text
            with patch.object(Path, "read_text", changed):
                self.assertIsNone(current_loop_state(root, 0))
            path.write_text("STATUS=waiting_limit\n" + "x" * 16384)
            self.assertIsNone(current_loop_state(root, 0))
            path.unlink()
            target = root / "other-state"
            target.write_text("STATUS=waiting_limit\n")
            path.symlink_to(target)
            self.assertIsNone(current_loop_state(root, 0))



@unittest.skipUnless(sys.platform == "linux", "POSIX owner integration runs under Linux/WSL")
class RunnerStabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.root = self.folder / "中文 root % fixture"
        self.root.mkdir()
        shutil.copytree(ROOT / "scripts/core", self.root / "scripts/core")
        copy_control_entrypoints(self.root)
        shutil.copytree(ROOT / "i18n", self.root / "i18n")
        shutil.copytree(ROOT / "memories", self.root / "memories")
        shutil.copy2(ROOT / ".gitignore", self.root / ".gitignore")
        for script in (self.root / "scripts/core").glob("*.sh"):
            script.chmod(0o755)
        (self.root / "PROMPT.md").write_text("Offline process contract fixture.\n")
        subprocess.run(["git", "init", "--initial-branch=main", str(self.root)], check=True, capture_output=True)
        self.control = self.folder / "controls"
        self.control.mkdir()
        self.manifest = {"centerId": "center_fixture123", "entryId": "entry_fixture123", "sourceId": "source_fixture123", "runtimeId": "runtime_fixture123",
                         "requestId": "request_fixture123", "dispatchId": "dispatch_fixture123", "nonce": "nonce_fixture123", "root": str(self.root),
                         "controlDir": str(self.control), "environment": {"ENGINE": "claude", "MODEL": "fixture", "AUTONOMOUS_COMPANY_LANGUAGE": "en"}}
        atomic_json(self.root / ".autonomous-company-center.json", {"protocolVersion": 1, **{key: self.manifest[key] for key in ("centerId", "runtimeId", "entryId", "sourceId")}})
        self.manifest_path = self.control / "manifest.json"
        atomic_json(self.manifest_path, self.manifest)
        self.runner = self.root / "scripts/core/center_runner.py"
        fake = self.root / "fake-engine"
        fake.write_text('''#!/bin/bash
if [ "${1:-}" = "--version" ]; then exit 0; fi
count=0
[ ! -f "$AUTONOMOUS_COMPANY_ROOT/invocations" ] || count=$(cat "$AUTONOMOUS_COMPANY_ROOT/invocations")
count=$((count+1))
echo "$count" > "$AUTONOMOUS_COMPANY_ROOT/invocations"
if [ "${CREATE_ARTIFACT:-0}" = 1 ] && [ "$count" = 1 ]; then
  bash "$AUTONOMOUS_COMPANY_ROOT/scripts/core/project.sh" new --name doc-fixture >/dev/null
  echo 'Offline artifact fixture' > "$AUTONOMOUS_COMPANY_ROOT/projects/doc-fixture/README.md"
  python3 "$AUTONOMOUS_COMPANY_ROOT/scripts/core/runtime_artifacts.py" --root "$AUTONOMOUS_COMPANY_ROOT" --project projects/doc-fixture document README.md && touch "$AUTONOMOUS_COMPANY_ROOT/artifact-ok"
fi
if [ "${SPAWN_ORPHAN:-0}" = 1 ]; then
    # Wait for the child to publish its identity before ending the fake engine;
    # otherwise correct fast cleanup can kill it before the test has a PID.
    python3 -c '
import os,time
from pathlib import Path
reader,writer=os.pipe()
if os.fork():
    os.close(writer)
    assert os.read(reader,1)==b"1"
    os._exit(0)
os.close(reader)
os.setsid()
Path(os.environ["AUTONOMOUS_COMPANY_ROOT"]+"/orphan").write_text(str(os.getpid()))
os.write(writer,b"1")
os.close(writer)
time.sleep(120)'
fi
if [ "${SLOW_ENGINE:-0}" = 1 ]; then sleep 120; fi
if [ "${ADD_P1:-0}" = 1 ]; then
    python3 -c 'import os,pathlib; p=pathlib.Path(os.environ["AUTONOMOUS_COMPANY_ROOT"])/"memories/consensus.md"; p.write_text(p.read_text().replace("## Priority Issues", "## Priority Issues\\n- [ ] P1: fixture needs human decision"))'
fi
printf '{"type":"result","subtype":"success","result":"offline","usage":{"input_tokens":1,"output_tokens":1}}\\n'
if [ "$count" -ge "${STOP_AFTER:-2}" ]; then touch "$AUTONOMOUS_COMPANY_ROOT/.auto-loop-stop"; fi
''')
        fake.chmod(0o755)
        self.env = dict(os.environ, CLAUDE_BIN=str(fake), CLAUDE_PERMISSION_MODE="default", LOOP_INTERVAL="0", CYCLE_TIMEOUT_SECONDS="30",
                        CYCLE_TERM_GRACE_SECONDS="1", CYCLE_KILL_WAIT_SECONDS="1", XDG_STATE_HOME=str(self.folder / "os-state"))
        self.processes = []
        self.addCleanup(self.cleanup)


    def heartbeat(self, stop=False):
        atomic_json(self.control / "control.json", {**{key: self.manifest[key] for key in ("centerId", "requestId", "dispatchId", "nonce")},
                                                   "heartbeat": time.time(), "stop": stop, "reason": "user_stop" if stop else None})


    def cleanup(self):
        self.heartbeat(True)
        for child in self.processes:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


    def launch(self, **env):
        self.heartbeat()
        output = (self.folder / ("process-%d.log" % len(self.processes))).open("wb")
        process = subprocess.Popen([sys.executable, str(self.runner), "run", "--manifest", str(self.manifest_path)],
                                   env={**self.env, **env}, stdout=output, stderr=output)
        output.close()
        self.processes.append(process)
        return process


    def wait_for(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.heartbeat()
            if predicate():
                return
            time.sleep(0.1)
        details = "\n".join(path.read_text(errors="replace")[-5000:] for path in self.folder.glob("*.log"))
        details += (self.control / "runner.log").read_text(errors="replace")[-5000:] if (self.control / "runner.log").exists() else ""
        self.fail("Timed out: " + details)


    def check_provider_status(self, status, stale):
        shutil.copy2(self.root / "memories/consensus.template.md", self.root / "memories/consensus.md")
        state = self.root / ".auto-loop-state"
        if stale:
            state.write_text("STATUS=" + status + "\n")
            old = time.time_ns() - 60_000_000_000
            os.utime(state, ns=(old, old))
        process = self.launch(SLOW_ENGINE="1")
        self.wait_for(lambda: (self.root / "invocations").exists() or process.poll() is not None)
        self.assertIsNone(process.poll(), "A previous run's provider pause stopped this dispatch")
        self.assertTrue((self.root / "invocations").exists())
        if stale:
            self.assertFalse((self.root / ".auto-loop-stop").exists())
            self.heartbeat(True)
        else:
            state.write_text("STATUS=" + status + "\n")
        process.wait(timeout=20)
        receipt = json.loads((self.control / "receipt.json").read_text())
        self.assertEqual(receipt["reason"], "user_stop" if stale else "provider_pause")
        self.assertTrue(receipt["cleanupConfirmed"])


    def test_stale_waiting_limit_does_not_stop_new_dispatch(self):
        self.check_provider_status("waiting_limit", stale=True)


    def test_stale_circuit_break_does_not_stop_new_dispatch(self):
        self.check_provider_status("circuit_break", stale=True)


    def test_current_waiting_limit_stops_dispatch(self):
        self.check_provider_status("waiting_limit", stale=False)


    def test_current_circuit_break_stops_dispatch(self):
        self.check_provider_status("circuit_break", stale=False)


    def test_receipt_failure_escalates_owned_cleanup_and_keeps_slot_until_dead(self):
        # Inject storage failure after a real TERM-resistant descendant exists.
        # This deliberately bypasses loop preflight, not process ownership.
        sys.path.insert(0, str(ROOT / "scripts/core"))
        from center_runner import identity, same_process, signal_owned, lock_free
        import signal
        sentinel = subprocess.Popen(["sleep", "60"])
        self.processes.append(sentinel)
        resistant = self.root / "resistant.py"
        resistant.write_text("import os,signal,time\n"
                             "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                             "open('resistant.pid','w').write(str(os.getpid()))\n"
                             "while True: time.sleep(0.1)\n")
        (self.root / "scripts/core/auto-loop.sh").write_text(
            '#!/bin/bash\npython3 resistant.py &\nwait\n')
        helper = self.folder / "fault_runner.py"
        helper.write_text('''import json,sys,time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import center_runner as runner
runner.preflight = lambda *args, **kwargs: {"quiescent": True}
original_write, original_signal = runner.atomic_json, runner.signal_owned
original_children = runner.owned_children
stale = json.loads(sys.argv[4])
def children():
    return {**original_children(), stale["pid"]: stale}
runner.owned_children = children
root = Path(sys.argv[2])
failed = False
def write(path, value):
    global failed
    if value.get("state") == "running":
        deadline = time.monotonic() + 10
        while not (root / "resistant.pid").exists():
            if time.monotonic() > deadline: raise ValueError("fixture_not_ready")
            time.sleep(0.01)
        failed = True
    if failed: raise OSError("injected_storage_failure")
    return original_write(path, value)
def send(value, sig):
    if sig == runner.signal.SIGKILL:
        (root / "kill-attempted").touch()
        if not (root / "allow-kill").exists(): return
    original_signal(value, sig)
runner.atomic_json, runner.signal_owned = write, send
runner.run(sys.argv[3])
''')
        self.heartbeat()
        output = (self.folder / "fault.log").open("wb")
        process = subprocess.Popen([sys.executable, str(helper), str(self.root / "scripts/core"),
                                    str(self.root), str(self.manifest_path),
                                    json.dumps({**identity(sentinel.pid), "start": "stale-start"})], env=self.env,
                                   stdout=output, stderr=output)
        output.close()
        self.processes.append(process)
        owned = None
        slot = self.folder / "os-state/autonomous-company/center-runners" / self.manifest["centerId"] / "slot.lock"
        try:
            self.wait_for(lambda: (self.root / "resistant.pid").exists())
            owned = identity(int((self.root / "resistant.pid").read_text()))
            self.assertIsNotNone(owned)
            self.wait_for(lambda: (self.root / "kill-attempted").exists(), timeout=15)
            self.assertTrue(same_process(owned))
            self.assertFalse(lock_free(slot), "Cleanup must retain the OS slot while a child survives")
            self.assertIsNone(process.poll())
            receipt = json.loads((self.control / "receipt.json").read_text())
            self.assertFalse(receipt["cleanupConfirmed"])
            (self.root / "allow-kill").touch()
            process.wait(timeout=5)
            self.assertNotEqual(process.returncode, 0, "Storage failure must still be reported")
            self.assertFalse(same_process(owned))
            self.assertFalse(Path("/proc/%d" % owned["pid"]).exists(), "Owned zombies must be reaped")
            self.assertTrue(lock_free(slot))
            self.assertIsNone(sentinel.poll(), "An unrelated process must never be signaled")
        finally:
            if owned and same_process(owned):
                signal_owned(owned, signal.SIGKILL)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)



if __name__ == "__main__":
    unittest.main()
