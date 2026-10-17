"""Explicit-domain path mapping and template-name conflict boundaries."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get('AUTONOMOUS_COMPANY_TEST_SOURCE', Path(__file__).resolve().parents[1]))
sys.path[:0] = [str(ROOT / 'dashboard'), str(ROOT / 'scripts/core')]
from center_paths import resolve_import_path
from center_profiles import validate_template_name
from center_store import CenterError


class PathMappingTests(unittest.TestCase):
    def setUp(self):
        self.domain = {'platform': 'wsl', 'distribution': 'Chosen', 'user': 'operator'}
        self.calls = []
        def execute(command, **kwargs):
            self.calls.append(command)
            result = 'D:\\source' if '-w' in command else '/custom/source'
            return SimpleNamespace(stdout=result + '\n')
        self.adapter = SimpleNamespace(domain=self.domain, _execute=execute)

    def test_windows_and_posix_paths_use_selected_distribution_user_and_actual_mapping(self):
        with patch('center_paths.os.name', 'nt'):
            windows = resolve_import_path('D:\\source', self.domain, 7, self.adapter)
            posix = resolve_import_path('/custom/source', self.domain, 7, self.adapter)
        self.assertEqual(windows, posix)
        self.assertEqual(windows['executionRoot'], '/custom/source')
        self.assertEqual(windows['environmentRevision'], 7)
        for call in self.calls:
            self.assertEqual(call[:6], ['wsl.exe', '--distribution', 'Chosen', '--user', 'operator', '--exec'])
        self.assertEqual(self.calls[-1][-3:], ['test', '-d', '/custom/source'])

    def test_foreign_wsl_unc_and_relative_paths_never_execute(self):
        for value in ('relative/path', '\\\\wsl.localhost\\Other\\home\\run'):
            with self.subTest(value=value), self.assertRaises(CenterError):
                resolve_import_path(value, self.domain, 1, self.adapter)
        self.assertFalse(self.calls)

    def test_failed_domain_conversion_is_not_guessed(self):
        def fail(*args, **kwargs):
            raise subprocess.CalledProcessError(1, ['wslpath'])
        self.adapter._execute = fail
        with self.assertRaises(CenterError) as raised:
            resolve_import_path('D:\\source', self.domain, 1, self.adapter)
        self.assertEqual(raised.exception.code, 'SOURCE_MISSING')

    def test_template_name_conflict_is_case_and_whitespace_insensitive(self):
        tx = SimpleNamespace(list=lambda table: [{'templateId': 'existing', 'name': 'My  Template'}])
        with self.assertRaises(CenterError) as raised:
            validate_template_name(tx, ' my template ', 'different')
        self.assertEqual(raised.exception.code, 'TEMPLATE_NAME_CONFLICT')
        validate_template_name(tx, 'My Template', 'existing')
        validate_template_name(tx, 'Different', 'different')


if __name__ == '__main__':
    unittest.main()
