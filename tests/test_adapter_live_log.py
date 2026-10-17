"""Live log byte growth and credential redaction under real WSL processes, no model."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "linux", "Real POSIX adapter/helper processes; run under WSL")
class AdapterLiveLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        shutil.copytree(ROOT / "scripts/core", self.root / "scripts/core")
        self.cycle = "cycle-stream-test"
        self.log = self.root / "logs" / (self.cycle + ".log")
        self.env = {**os.environ, "PROJECT_DIR": str(self.root), "AUTONOMOUS_COMPANY_CYCLE_ID": self.cycle,
                    "ADAPTER_PATH": str(self.root / "scripts/core/engine-adapters.sh"), "ENGINE": "codex",
                    "CYCLE_TIMEOUT_SECONDS": "15", "CYCLE_TERM_GRACE_SECONDS": "1", "CYCLE_KILL_WAIT_SECONDS": "1"}
        self.children = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.03)
        self.fail("Live log condition not observed")

    def start(self, body, **environment):
        emitter = self.root / "emitter.py"
        emitter.write_text(body)
        script = self.root / "contract.sh"
        script.write_text('''#!/bin/bash
set -euo pipefail
source "$ADAPTER_PATH"
adapter_execute python3 "$PROJECT_DIR/emitter.py"
printf '%s\\n' "$ADAPTER_EXIT_CODE" > "$PROJECT_DIR/exit-code"
printf '%s' "$ADAPTER_OUTPUT" > "$PROJECT_DIR/final-output"
printf '%s' "$ADAPTER_LOG_PID" > "$PROJECT_DIR/mirror-pid"
''')
        output = (self.root / "contract-output").open("wb")
        process = subprocess.Popen(["bash", str(script)], env={**self.env, **environment}, stdout=output, stderr=output)
        output.close()
        self.children.append(process)
        return process

    def test_streams_before_exit_and_redacts_secrets_split_across_writes(self):
        secret = "sensitive-credential-exact-test"
        body = '''import os,time
from pathlib import Path
root=Path(os.environ['PROJECT_DIR'])
secret=os.environ['OPENAI_API_KEY'].encode()
os.write(1,b'visible start\\n'+b'x'*100+b'\\n'+secret[:12])
(root/'partial-ready').touch()
while not (root/'continue').exists(): time.sleep(.02)
os.write(1,secret[12:]+b'\\n'+os.environ['ANTHROPIC_AUTH_TOKEN'].encode()+b'\\n'+b'y'*100)
(root/'complete-ready').touch()
while not (root/'finish').exists(): time.sleep(.02)
raise SystemExit(7)
'''
        process = self.start(body, OPENAI_API_KEY=secret, ANTHROPIC_AUTH_TOKEN="another-private-token")
        self.wait_for(lambda: (self.root / "partial-ready").exists() and self.log.exists() and b"visible start" in self.log.read_bytes())
        first = self.log.read_bytes()
        self.assertIsNone(process.poll())
        self.assertNotIn(secret[:12].encode(), first)
        (self.root / "continue").touch()
        self.wait_for(lambda: (self.root / "complete-ready").exists() and b"[REDACTED]" in self.log.read_bytes())
        current = self.log.read_bytes()
        self.assertGreater(len(current), len(first))
        self.assertNotIn(secret.encode(), current)
        self.assertNotIn(b"another-private-token", current)
        (self.root / "finish").touch()
        process.wait(timeout=10)
        self.assertEqual(process.returncode, 0, (self.root / "contract-output").read_text())
        self.assertEqual((self.root / "exit-code").read_text().strip(), "7")
        self.assertEqual((self.root / "mirror-pid").read_text(), "")
        self.assertEqual(self.log.read_bytes(), (self.root / "final-output").read_bytes())
        self.assertEqual(self.log.read_bytes().count(b"[REDACTED]"), 2)

    def test_all_six_credentials_cross_write_and_utf8_boundaries(self):
        names = ("OPENAI_COMPATIBLE_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY",
                 "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CURSOR_API_KEY")
        credentials = {name: "exact-private-" + str(index) + "-credential-long-value" for index, name in enumerate(names)}
        credentials["CURSOR_API_KEY"] = "cursor-私有凭据-字节边界"
        credentials["ANTHROPIC_API_KEY"] = "anthropic-private-line1\nline2-complete"
        body = "import os,time\nfor name in " + repr(names) + ":\n token=os.environ[name].encode()\n middle=len(token)//2\n os.write(1,b'prefix '+token[:middle]);time.sleep(.03)\n os.write(1,token[middle:]+b' suffix\\n');time.sleep(.03)\nraise SystemExit(7)\n"
        process = self.start(body, **credentials)
        process.wait(timeout=15)
        self.assertEqual(process.returncode, 0, (self.root / "contract-output").read_text())
        current = self.log.read_bytes()
        for value in credentials.values():
            self.assertNotIn(value.encode(), current)
        self.assertEqual(current.count(b"[REDACTED]"), 6)
        self.assertEqual(current, (b"prefix [REDACTED] suffix\n" * 6))
        self.assertEqual((self.root / "exit-code").read_text().strip(), "7")
        self.assertEqual((self.root / "final-output").read_bytes(), current.rstrip(b"\n"))

    def test_overlapping_credentials_mask_whole_original_intervals(self):
        cases = [
            ({"OPENAI_COMPATIBLE_API_KEY": "fixture-prefix", "OPENAI_API_KEY": "fixture-prefix-private-tail"},
             b"fixture-prefix-private-tail"),
            ({"CODEX_API_KEY": "alpha-middle", "ANTHROPIC_API_KEY": "middle-omega"},
             b"alpha-middle-omega"),
            ({"ANTHROPIC_AUTH_TOKEN": "aba", "CURSOR_API_KEY": "bab"}, b"abababab"),
        ]
        for credentials, payload in cases:
            with self.subTest(credentials=tuple(credentials)):
                encoded = base64.b64encode(payload).decode()
                body = "import os,base64,time\np=base64.b64decode(" + repr(encoded) + ")\nfor byte in p:\n os.write(1,bytes([byte]));time.sleep(.01)\nos.write(1,b' visible end\\n');raise SystemExit(7)\n"
                process = self.start(body, **credentials)
                process.wait(timeout=15)
                self.assertEqual(process.returncode, 0, (self.root / "contract-output").read_text())
                self.assertEqual(self.log.read_bytes(), b"[REDACTED] visible end\n")
                self.assertEqual((self.root / "final-output").read_bytes(), b"[REDACTED] visible end")
                self.assertEqual((self.root / "exit-code").read_text().strip(), "7")

    def test_custom_escaped_bearer_and_structured_final_usage(self):
        credentials = {"CUSTOM_SECRET": 'fictional-雪"\\line\nend', "NUMERIC_TOKEN": "1234",
                       "SHORT_TOKEN": "abcd"}
        payload = {"type": "result", "result": " / ".join(credentials.values()) + " Bearer private-bearer",
                   "usage": {"input_tokens": 1234, "output_tokens": 2}, "is_error": False}
        body = ("import os,time\np=" + repr(json.dumps(payload).encode()) +
                "\nfor offset in range(0,len(p),3):\n os.write(1,p[offset:offset+3]);time.sleep(.001)\n")
        process = self.start(body, **credentials)
        process.wait(timeout=15)
        self.assertEqual(process.returncode, 0, (self.root / "contract-output").read_text())
        live = self.log.read_text()
        self.assertNotIn("fictional-", live)
        self.assertNotIn("private-bearer", live)
        final = json.loads((self.root / "final-output").read_text())
        self.assertEqual(final["usage"], {"input_tokens": 1234, "output_tokens": 2})
        self.assertIs(final["is_error"], False)
        self.assertNotIn("abcd", final["result"])

    def test_mirror_start_refusal_never_invokes_provider(self):
        self.log.parent.mkdir()
        preserved = self.root / "preserved"
        preserved.write_text("original")
        self.log.symlink_to(preserved)
        process = self.start("from pathlib import Path; import os; (Path(os.environ['PROJECT_DIR'])/'provider-called').touch()")
        process.wait(timeout=10)
        self.assertEqual(process.returncode, 0, (self.root / "contract-output").read_text())
        self.assertEqual((self.root / "exit-code").read_text().strip(), "78")
        self.assertFalse((self.root / "provider-called").exists())
        self.assertEqual(preserved.read_text(), "original")

    def test_helper_refuses_linked_capture_target_and_completion(self):
        directory = Path(self.temp.name) / "private"
        directory.mkdir(mode=0o700)
        capture = directory / "capture"
        capture.write_bytes(b"private input")
        self.log.parent.mkdir(parents=True, exist_ok=True)
        other = Path(self.temp.name) / "preserved"
        other.write_bytes(b"preserve these bytes")
        os.link(other, self.log)
        command = [sys.executable, str(self.root / "scripts/core/center_runner.py"), "stream-log",
                   "--root", str(self.root), "--input-path", str(capture), "--relative-path", "logs/" + self.cycle + ".log"]
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe_stream_target", result.stderr)
        self.assertEqual(other.read_bytes(), b"preserve these bytes")
        self.log.unlink()
        linked = directory / "hardlinked-capture"
        os.link(capture, linked)
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=5)
        self.assertIn("unsafe_stream_capture", result.stderr)
        linked.unlink()
        complete = directory / "capture.complete"
        complete.symlink_to(other)
        result = subprocess.run([sys.executable, str(self.root / "scripts/core/center_runner.py"), "stream-log-complete",
                                 "--input-path", str(capture)], env=self.env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe_stream_completion", result.stderr)
        self.assertEqual(other.read_bytes(), b"preserve these bytes")

    def test_helper_rejects_unsafe_ready_and_nonregular_target_without_hanging(self):
        directory = Path(self.temp.name) / "private-ready"
        directory.mkdir(mode=0o700)
        capture = directory / "capture"
        capture.write_bytes(b"input")
        self.log.parent.mkdir(parents=True)
        preserved = self.root / "preserved-ready"
        preserved.write_text("original")
        ready = directory / "capture.ready"
        ready.symlink_to(preserved)
        command = [sys.executable, str(self.root / "scripts/core/center_runner.py"), "stream-log",
                   "--root", str(self.root), "--input-path", str(capture), "--relative-path", "logs/" + self.cycle + ".log"]
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(preserved.read_text(), "original")
        self.log.unlink()
        os.mkfifo(self.log)
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)

    def test_large_output_and_utf8_are_not_truncated_or_duplicated(self):
        payload = ("中文输出\n" * 16000).encode()
        encoded = base64.b64encode(payload).decode()
        body = "import os,base64,time\ndata=base64.b64decode(" + repr(encoded) + ")\nfor offset in range(0,len(data),4093):\n os.write(1,data[offset:offset+4093]);time.sleep(.001)\n"
        process = self.start(body)
        process.wait(timeout=15)
        self.assertEqual(process.returncode, 0, (self.root / "contract-output").read_text())
        self.assertEqual(self.log.read_bytes(), payload)
        self.assertEqual((self.root / "exit-code").read_text().strip(), "0")
        # Shell command substitution intentionally removes final newlines, as
        # before; the live stream preserves every original byte while active.
        self.assertEqual((self.root / "final-output").read_bytes(), payload.rstrip(b"\n"))


if __name__ == "__main__":
    unittest.main()
