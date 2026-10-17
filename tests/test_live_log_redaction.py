"""The live mirror retains A secret sources across arbitrary UTF-8 chunks."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/core"))
from live_log import StreamRedactor
from log_redaction import redact_text


class StreamRedactionTests(unittest.TestCase):
    def test_every_split_custom_escaped_overlap_and_bearer(self):
        credentials = {"OPENAI_API_KEY": "xy", "CUSTOM_SECRET": 'private-雪"\\line\nend',
                       "DEPLOY_TOKEN": "long-private-" * 300,
                       "FIRST_PASSWORD": "alpha-middle", "SECOND_TOKEN": "middle-omega"}
        secret = credentials["CUSTOM_SECRET"]
        raw = ("before " + secret + " " + json.dumps(secret)[1:-1] + " " + credentials["DEPLOY_TOKEN"]
               + " alpha-middle-omega xy Bearer\t unknown-secret-token " + "after")
        with patch.dict(os.environ, credentials, clear=True):
            expected = redact_text(raw)
            for width in (1, 2, 7, 4093, 65536):
                redactor = StreamRedactor()
                encoded = raw.encode()
                actual = "".join(redactor.feed(encoded[index:index+width]) for index in range(0, len(encoded), width))
                actual += redactor.feed(b"", final=True)
                self.assertEqual(actual, expected)

    def test_unbounded_bearer_token_does_not_grow_pending_memory(self):
        with patch.dict(os.environ, {}, clear=True):
            redactor = StreamRedactor()
            output = redactor.feed(b"Bearer ")
            for _ in range(100):
                output += redactor.feed(b"x" * 8192)
                self.assertLessEqual(len(redactor.buffer), redactor.maximum - 1)
            output += redactor.feed(b" visible", final=True)
        self.assertEqual(output, "Bearer [REDACTED] visible")

    def test_unicode_whitespace_and_multiline_bearer(self):
        with patch.dict(os.environ, {}, clear=True):
            for text in ("Bearer\n\t secret done", "bEaReR\u2003token done", "BearerX visible"):
                redactor = StreamRedactor()
                result = "".join(redactor.feed(bytes([byte])) for byte in text.encode())
                result += redactor.feed(b"", final=True)
                self.assertEqual(result, redact_text(text))


if __name__ == "__main__":
    unittest.main()
