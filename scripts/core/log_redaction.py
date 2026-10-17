"""Shared best-effort redaction for adapter logs and runtime projections.

This is an output filter, not a sandbox or a detector for arbitrary secrets.
Provider key variables retain their historical any-length coverage. Other
secret-named environment values need four characters to limit false positives.
"""
from __future__ import annotations

import json
import os
import re
import sys

PROVIDER_KEYS = (
    "OPENAI_COMPATIBLE_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CURSOR_API_KEY",
)
SECRET_PARTS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY")
# Preserve the event projection policy; structured output is decoded before
# this runs so a token match cannot consume JSON syntax.
BEARER = re.compile(r"(?i)(Bearer\s+)\S+")


def _secret_values() -> set[str]:
    secrets = {
        secret for name, secret in os.environ.items()
        if secret and (name in PROVIDER_KEYS or (
            len(secret) >= 4 and any(part in name.upper() for part in SECRET_PARTS)
        ))
    }
    # Diagnostics can surround pretty-printed or incomplete JSON. Cover the
    # standard escaped spellings there too, even when no JSON document parses.
    secrets.update(json.dumps(secret, ensure_ascii=ascii_only)[1:-1]
                   for secret in tuple(secrets) for ascii_only in (True, False))
    return secrets


def _replace_secrets(value: str, secrets: set[str]) -> str:
    if secrets:
        # Mask the union of original intervals, including intersecting secrets.
        # Replacements are never reinterpreted as another configured secret.
        covered = bytearray(len(value))
        for secret in secrets:
            start = 0
            while (start := value.find(secret, start)) != -1:
                covered[start:start + len(secret)] = b"\x01" * len(secret)
                start += 1
        result, offset = [], 0
        for match in re.finditer(b"\x01+", covered):
            result.extend((value[offset:match.start()], "[REDACTED]"))
            offset = match.end()
        result.append(value[offset:])
        value = "".join(result)
    return value


def redact_text(value: str) -> str:
    return BEARER.sub(r"\1[REDACTED]", _replace_secrets(value, _secret_values()))


def _redact_json(value):
    if isinstance(value, str):
        return redact_text(value)
    # Preserve typed usage numbers, booleans and nulls, even if their textual
    # representation coincides with a configured secret.
    if not isinstance(value, (dict, list)):
        return value
    result = {} if isinstance(value, dict) else []
    pending = [(value, result)]
    # Do not add recursive Python frames to JSON that the decoder can handle.
    while pending:
        source, target = pending.pop()
        items = source.items() if isinstance(source, dict) else enumerate(source)
        for key, item in items:
            if isinstance(item, (dict, list)):
                clean = {} if isinstance(item, dict) else []
                pending.append((item, clean))
            else:
                clean = redact_text(item) if isinstance(item, str) else item
            if isinstance(target, dict):
                target[redact_text(key)] = clean
            else:
                target.append(clean)
    return result


def redact_output(value: str) -> str:
    """Redact decoded JSON strings, preserving structured provider metadata."""
    try:
        item = json.loads(value)
        if isinstance(item, (dict, list, str)):
            return json.dumps(_redact_json(item), ensure_ascii=False)
        return redact_text(value)
    except (ValueError, RecursionError):
        pass
    # A multiline secret may itself contain a JSON-looking line. Mask literal
    # multiline values before splitting, otherwise that line would split the
    # match across separately redacted chunks. Single-line numeric secrets are
    # still handled only inside decoded strings, preserving JSONL usage.
    value = _replace_secrets(value, {secret for secret in _secret_values()
                                     if "\n" in secret or "\r" in secret})
    result, text = [], []
    for line in value.splitlines(keepends=True):
        try:
            item = json.loads(line)
        except (ValueError, RecursionError):
            text.append(line)
            continue
        if not isinstance(item, (dict, list, str)):
            text.append(line)
            continue
        if text:
            result.append(redact_text("".join(text)))
            text = []
        result.append(json.dumps(_redact_json(item), ensure_ascii=False))
        if line.endswith("\n"):
            result.append("\n")
    result.append(redact_text("".join(text)))
    return "".join(result)


if __name__ == "__main__":
    raw = sys.stdin.read()
    sys.stdout.write(redact_output(raw) if "--structured" in sys.argv[1:] else redact_text(raw))
