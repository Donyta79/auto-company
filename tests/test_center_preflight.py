"""Linux admission distinguishes an absent session from an unreadable service."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("preflight_runner", ROOT / "scripts/core/center_runner.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@unittest.skipUnless(sys.platform == "linux", "Linux service admission; run under WSL")
class ServicePreflightTests(unittest.TestCase):
    def preflight(self, diagnostic, *, manager=False, stdout="", code=1):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            private = Path(f"/run/user/{os.getuid()}/systemd/private")
            exists = Path.exists
            service = subprocess.CompletedProcess([], code, stdout.replace("ROOT", str(root)), diagnostic)
            with patch.object(runner, "compatible", return_value=True), \
                    patch.object(runner, "protective_reason", return_value=None), \
                    patch.object(runner, "lock_free", return_value=True), \
                    patch.object(Path, "exists", lambda path: manager if path == private else exists(path)), \
                    patch.object(runner.subprocess, "run", return_value=service):
                return runner.preflight(root, require_context=False)

    def test_old_and_new_absent_session_diagnostics_allow_direct_execution(self):
        for prefix in ("Failed to connect to bus", "Failed to connect to user scope bus via local transport"):
            for reason in ("No medium found", "No such file or directory",
                           "$DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined (consider using --machine=)"):
                with self.subTest(prefix=prefix, reason=reason):
                    result = self.preflight(f"{prefix}: {reason}\n")
                    self.assertTrue(result["quiescent"], result)

    def test_existing_user_manager_cannot_be_ignored(self):
        result = self.preflight("Failed to connect to user scope bus via local transport: No medium found", manager=True)
        self.assertEqual(result["reason"], "service_state_unknown")

    def test_permission_and_unknown_errors_remain_blocked(self):
        for reason in ("Permission denied", "Access denied", "Connection refused", "Unrecognized failure"):
            for prefix in ("Failed to connect to bus", "Failed to connect to user scope bus via local transport"):
                with self.subTest(prefix=prefix, reason=reason):
                    result = self.preflight(f"{prefix}: {reason}")
                    self.assertFalse(result["quiescent"])
                    self.assertEqual(result["reason"], "service_state_unknown")

    def test_bound_active_or_enabled_service_remains_blocked(self):
        for state in ("ActiveState=active", "UnitFileState=enabled"):
            result = self.preflight("", stdout="ExecStart=ROOT/scripts/core/auto-loop.sh\n" + state, code=0)
            self.assertEqual(result["reason"], "legacy_service_bound")

    def test_missing_unit_and_unrelated_service_do_not_block(self):
        self.assertTrue(self.preflight("Unit autonomous-company.service could not be found.")["quiescent"])
        self.assertTrue(self.preflight("", stdout="ExecStart=/unrelated/auto-loop.sh\nActiveState=active", code=0)["quiescent"])


if __name__ == "__main__":
    unittest.main()
