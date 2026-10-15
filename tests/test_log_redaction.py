"""Only fictional credentials and fake provider output; no provider calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/core"))
from log_redaction import PROVIDER_KEYS, redact_output, redact_text
from runtime_events import Recorder, bounded_text


class LogRedactionTests(unittest.TestCase):
    def test_supported_and_custom_environment_credentials_share_policy(self):
        credentials = {name: f"fictional-{name}-canary" for name in PROVIDER_KEYS}
        credentials.update(GH_TOKEN="fictional-gh-canary", DEPLOY_SECRET="fictional-deploy-canary",
                           service_password="fictional-password-canary", SERVICE_API_KEY="fictional-service-canary")
        raw = "\n".join(credentials.values()) + "\nBearer fictional-unknown-token=="
        with patch.dict(os.environ, credentials, clear=True):
            self.assertEqual(redact_text(raw), bounded_text(raw))
            self.assertEqual(redact_text(raw), "\n".join(["[REDACTED]"] * len(credentials)) + "\nBearer [REDACTED]")

    def test_bearer_policy_keeps_projection_coverage(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(redact_text("bEaReR fictional:odd-token!"), "bEaReR [REDACTED]")
            clean = json.loads(redact_output('{"result":"Bearer fictional:odd-token!","usage":{"input_tokens":7}}'))
            self.assertEqual(clean["result"], "Bearer [REDACTED]")
            self.assertEqual(clean["usage"]["input_tokens"], 7)

    def test_short_value_policy_preserves_provider_protection(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "xy", "GH_TOKEN": "abc", "SERVICE_SECRET": "abcd"}, clear=True):
            self.assertEqual(redact_text("xy abc abcd"), "[REDACTED] abc [REDACTED]")
            self.assertEqual(bounded_text("xy abc abcd"), "[REDACTED] abc [REDACTED]")

    def test_overlap_multiline_escaped_json_and_numeric_usage(self):
        secret = 'fictional-quote"-slash\\-line\n雪'
        with patch.dict(os.environ, {"GH_TOKEN": secret, "FIRST_SECRET": "fictional-prefix",
                                   "SECOND_SECRET": "fictional-prefix-long", "OPENAI_API_KEY": "1234"}, clear=True):
            raw = {"result": secret + " fictional-prefix-long Bearer fictional.token==", "usage": {"input_tokens": 1234}, "cost_usd": None}
            clean = json.loads(redact_output(json.dumps(raw, indent=2)))
            mixed = "provider diagnostic\n" + json.dumps(raw, indent=2)
            self.assertNotIn(json.dumps(secret)[1:-1], redact_output(mixed))
            self.assertEqual(clean["result"], "[REDACTED] [REDACTED] Bearer [REDACTED]")
            self.assertEqual(clean["usage"]["input_tokens"], 1234)
            self.assertIsNone(clean["cost_usd"])
            self.assertEqual(redact_output("before\n" + secret + "\nafter"), "before\n[REDACTED]\nafter")

    def test_numeric_secret_in_plain_output_is_not_treated_as_usage(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "1234"}, clear=True):
            self.assertEqual(redact_output("1234"), "[REDACTED]")
            self.assertEqual(redact_output("diagnostic\n1234\nend"), "diagnostic\n[REDACTED]\nend")

    def test_multiline_secret_with_json_looking_middle_line(self):
        for middle in ("123", '{"input_tokens":123}'):
            secret = "fictional-alpha\n" + middle + "\nfictional-omega"
            with self.subTest(middle=middle), patch.dict(os.environ, {"DEPLOY_SECRET": secret, "OTHER_TOKEN": "1234"}, clear=True):
                raw = "diagnostic\n" + secret + '\n{"usage":{"input_tokens":1234}}\nend'
                clean = redact_output(raw)
                self.assertEqual(clean.splitlines()[:2], ["diagnostic", "[REDACTED]"])
                self.assertEqual(json.loads(clean.splitlines()[2])["usage"]["input_tokens"], 1234)
                self.assertNotIn("fictional-alpha", clean)
                self.assertNotIn("fictional-omega", clean)

    def test_jsonl_mixed_with_stderr_remains_parseable(self):
        raw = 'diagnostic Bearer fictional.token==\n' + json.dumps({"result": "Bearer fictional.token==", "usage": {"input_tokens": 7}}) + '\n'
        with patch.dict(os.environ, {}, clear=True):
            lines = redact_output(raw).splitlines()
        self.assertEqual(lines[0], "diagnostic Bearer [REDACTED]")
        self.assertEqual(json.loads(lines[1])["usage"]["input_tokens"], 7)
        self.assertEqual(json.loads(lines[1])["result"], "Bearer [REDACTED]")

    def test_deep_provider_details_preserve_structured_usage(self):
        raw = ('{"result":"Bearer fictional-deep-token","usage":{"input_tokens":120,"output_tokens":5},'
               '"details":' + '[' * 600 + '0' + ']' * 600 + '}')
        for prefix in ("", "provider diagnostic\n"):
            with self.subTest(jsonl=bool(prefix)), patch.dict(os.environ, {}, clear=True):
                output = redact_output(prefix + raw)
                clean = json.loads(output.splitlines()[-1])
                self.assertEqual(clean["result"], "Bearer [REDACTED]")
                self.assertEqual(clean["usage"], {"input_tokens": 120, "output_tokens": 5})
                details = clean["details"]
                for _ in range(600):
                    details = details[0]
                self.assertEqual(details, 0)

    @unittest.skipIf(os.name == "nt", "Bash adapter contracts run on Linux/WSL")
    def test_fake_provider_logs_sidecars_and_projection(self):
        for engine in ("claude", "codex", "cursor", "openai-compatible"):
            for known_usage in (True, False):
                with self.subTest(engine=engine, known_usage=known_usage), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    secrets = {"GH_TOKEN": "fictional-gh-canary", "DEPLOY_SECRET": 'fictional-deploy"\\\n雪',
                               "ANTHROPIC_API_KEY": "fictional-provider-canary"}
                    message = " / ".join(secrets.values()) + " Bearer fictional-bearer-canary=="
                    payload = {"type": "result", "subtype": "success", "result": message}
                    usage = {"input_tokens": 1234, "output_tokens": 2}
                    if known_usage:
                        payload.update(usage=usage, total_cost_usd=0.5)
                    if engine == "codex":
                        payloads = [
                            {"type": "item.completed", "item": {"id": "fake-item", "type": "command_execution", "command": message}},
                            {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
                            {"type": "turn.completed", **({"usage": usage} if known_usage else {})},
                        ]
                    else:
                        payloads = [payload]
                    # Fake executable writes provider fixtures to stdout only.
                    cli = root / "fake-provider"
                    cli.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdout.write(" + repr("\n".join(json.dumps(item) for item in payloads)) + ")\n")
                    cli.chmod(0o700)
                    env = {"PATH": os.defpath + os.pathsep + str(Path(sys.executable).parent), "HOME": directory,
                           "ENGINE": engine, "PROJECT_DIR": directory, "CYCLE_TIMEOUT_SECONDS": "10",
                           "CYCLE_TERM_GRACE_SECONDS": "0", "CYCLE_KILL_WAIT_SECONDS": "2",
                           "FAKE_PROVIDER": str(cli), "ADAPTER_PATH": str(ROOT / "scripts/core/engine-adapters.sh"), **secrets}
                    script = '''set -euo pipefail
source "$ADAPTER_PATH"
adapter_execute "$FAKE_PROVIDER"
ADAPTER_RESULT_SOURCE="$ADAPTER_OUTPUT"
engine_adapter_extract_metadata
printf '%s' "$ADAPTER_OUTPUT" > "$PROJECT_DIR/cycle.log"
engine_adapter_write_record "$PROJECT_DIR/cycle.json" success "Bearer fictional-failure-canary"
'''
                    result = subprocess.run(["bash", "--noprofile", "--norc", "-c", script], env=env, text=True, capture_output=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    with patch.dict(os.environ, secrets, clear=True):
                        recorder = Recorder(root, "cycle-fixture", home=root / "fake-codex-home")
                        recorder.ingest(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": message}}))
                    for path in (root / "cycle.log", root / "cycle.json", recorder.path):
                        persisted = path.read_text()
                        for secret in (*secrets.values(), "fictional-bearer-canary", "fictional-failure-canary"):
                            self.assertNotIn(secret, persisted)
                            self.assertNotIn(json.dumps(secret)[1:-1], persisted)
                        self.assertIn("[REDACTED]", persisted)
                    record = json.loads((root / "cycle.json").read_text())
                    self.assertEqual(record["status"], "success")
                    self.assertEqual(record["input_tokens"], 1234 if known_usage else None)
                    self.assertEqual(record["total_tokens"], 1236 if known_usage else None)
                    self.assertEqual(record["cost_usd"], 0.5 if known_usage and engine != "codex" else None)

    @unittest.skipIf(os.name == "nt", "Bash adapter contracts run on Linux/WSL")
    def test_shell_local_provider_key_still_redacted(self):
        script = 'ENGINE=claude; source "$1"; OPENAI_API_KEY=xy; printf xy | adapter_redact'
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c", script, "bash", str(ROOT / "scripts/core/engine-adapters.sh")],
                                env={"PATH": os.defpath + os.pathsep + str(Path(sys.executable).parent)}, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
