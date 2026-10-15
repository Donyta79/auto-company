"""Only provider failures may select the long usage-limit cooldown."""

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("limit_metadata", ROOT / "scripts/core/engine-metadata.py")
metadata = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metadata)


class UsageLimitTests(unittest.TestCase):
    def test_business_words_and_numbers_do_not_indicate_a_provider_limit(self):
        for text in ("Build failed: missing billing.ts", "AssertionError: quota counter expected 2",
                     "compile error near line 429", "Error: billing calculation failed",
                     "Error: quota validation failed", "Error: overloaded function is ambiguous"):
            with self.subTest(text=text):
                self.assertFalse(metadata.usage_limited(text))
                self.assertFalse(metadata.usage_limited(json.dumps({
                    "type": "turn.failed", "error": {"message": text},
                })))

    def test_structured_provider_codes_and_http_status_survive(self):
        for error in ({"code": "rate_limit_exceeded"}, {"type": "overloaded_error"},
                      {"code": "insufficient_quota"}, {"status_code": 429},
                      {"message": "You've hit your usage limit. Try again later."}):
            with self.subTest(error=error):
                self.assertTrue(metadata.usage_limited(json.dumps({"type": "turn.failed", "error": error})))
                self.assertTrue(metadata.usage_limited(json.dumps({"type": "error", "error": error})))

    def test_result_errors_and_early_plain_diagnostics(self):
        for message in ("HTTP 429: Too Many Requests", "Rate limit exceeded",
                        "You exceeded your current quota", "Insufficient credits",
                        "Server is overloaded", "Billing hard limit reached"):
            with self.subTest(message=message):
                self.assertTrue(metadata.usage_limited("Error: " + message))
                for fields in ({"is_error": True}, {"status": "error"}, {"subtype": "error_during_execution"}):
                    self.assertTrue(metadata.usage_limited(json.dumps({"type": "result", "result": message, **fields})))
        self.assertTrue(metadata.usage_limited(json.dumps({
            "type": "result", "subtype": "error_during_execution", "is_error": True,
            "errors": ["Rate limit exceeded"],
        })))

    def test_tool_output_and_assistant_messages_cannot_select_cooldown(self):
        for item in ({"type": "command_execution", "aggregated_output": "HTTP 429: Too Many Requests"},
                     {"type": "agent_message", "text": "You've hit your usage limit"}):
            output = json.dumps({"type": "item.completed", "item": item})
            output += '\n{"type":"turn.failed","error":{"message":"process crashed"}}'
            self.assertFalse(metadata.usage_limited(output))
        self.assertFalse(metadata.usage_limited(json.dumps({
            "type": "result", "status": "success", "result": "Rate limit exceeded",
        })))
        for kind in ("tool_result", "command_execution", "agent_message"):
            with self.subTest(kind=kind):
                self.assertFalse(metadata.usage_limited(json.dumps({
                    "type": kind, "status": "error", "is_error": True,
                    "message": "Rate limit exceeded", "result": "HTTP 429: Too Many Requests",
                })))

    def test_final_outcome_overrides_recovered_limit_error(self):
        error = json.dumps({"type": "error", "message": "Rate limit exceeded"})
        self.assertFalse(metadata.usage_limited(error + '\n{"type":"turn.completed"}'))
        self.assertFalse(metadata.usage_limited(error + '\n{"type":"turn.failed","error":{"message":"disk full"}}'))
        self.assertTrue(metadata.usage_limited('{"type":"turn.completed"}\n' + error))

    def test_final_turn_result_preserves_recovered_success_and_usage(self):
        error = json.dumps({"type": "turn.failed", "error": {"code": "rate_limit_exceeded"}})
        success = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 8, "output_tokens": 3}})
        result = metadata.normalize("codex", error + "\n" + success)
        self.assertEqual((result["status"], result["subtype"], result["total_tokens"]), ("success", "success", 11))
        self.assertFalse(metadata.usage_limited(error + "\n" + success))
        result = metadata.normalize("codex", success + "\n" + error)
        self.assertEqual(result["status"], "error")
        self.assertTrue(metadata.usage_limited(success + "\n" + error))

    @unittest.skipIf(os.name == "nt", "Bash contract runs under WSL/POSIX")
    def test_actual_loop_classifier_uses_the_metadata_boundary(self):
        source = (ROOT / "scripts/core/auto-loop.sh").read_text()
        function = re.search(r"^check_usage_limit\(\) \{\n.*?^\}", source, re.M | re.S).group()
        script = 'SCRIPT_DIR="$1"\nENGINE=codex\n' + function + '\ncheck_usage_limit "$2"'
        for text, expected in (("Build failed: missing billing.ts", 1), ("HTTP 429: Too Many Requests", 0)):
            result = subprocess.run(["bash", "-c", script, "test", str(ROOT / "scripts/core"), text],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
