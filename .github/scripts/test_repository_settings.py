"""Offline checks for the actual settings payload and unchanged authority gates."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name('repository-settings.sh')


class RepositorySettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mainrag-settings-policy-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        for tool in ('jq', 'sha256sum'):
            executable = shutil.which(tool)
            self.assertIsNotNone(executable, f'{tool} is required for this offline gate')
            (self.directory / tool).symlink_to(executable)
        # No real gh or network-capable client is on PATH. Any accidental live
        # action fails even if the caller has credentials in its environment.
        fake = self.directory / 'gh'
        fake.write_text('#!/bin/sh\necho "unexpected live GitHub action" >&2\nexit 99\n')
        fake.chmod(0o700)
        self.environment = {'PATH': str(self.directory), 'REPOSITORY': 'synthetic/mainrag'}

    def call(self, code: str, *arguments: str, environment=None):
        return subprocess.run(['/bin/bash', '-c', code, 'fixture', str(SCRIPT), *arguments],
                              cwd=SCRIPT.parents[2], env=environment or self.environment,
                              text=True, capture_output=True, timeout=10)

    def payload(self):
        result = self.call('source "$1"; protection_payload "$2"', '15368')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_pull_request_configuration_is_retained_with_zero_approvals(self):
        reviews = self.payload()['required_pull_request_reviews']
        self.assertIsInstance(reviews, dict, 'null disables the pull-request review configuration')
        self.assertEqual(reviews, {'dismiss_stale_reviews': True, 'require_code_owner_reviews': False,
                                  'required_approving_review_count': 0, 'require_last_push_approval': False})

    def test_required_checks_and_other_protections_are_unchanged(self):
        payload = self.payload()
        self.assertEqual(payload['required_status_checks'], {
            'strict': True, 'checks': [{'context': value, 'app_id': 15368}
                                      for value in ('ci-required', 'workflow-policy', 'issue-contract')],
        })
        self.assertTrue(payload['enforce_admins'])
        self.assertTrue(payload['required_conversation_resolution'])
        self.assertFalse(payload['allow_force_pushes'])
        self.assertFalse(payload['allow_deletions'])

    def test_invalid_check_source_fails_without_any_live_call(self):
        for value in ('', '0', '-1', '1.0', 'null', '1; false', '1\n2'):
            with self.subTest(value=value):
                result = self.call('source "$1"; protection_payload "$2"', value)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertIn('positive GitHub Actions app ID required', result.stderr)
                self.assertNotIn('unexpected live GitHub action', result.stderr)

    def test_apply_still_requires_each_explicit_confirmation_before_live_access(self):
        environment = dict(self.environment)
        cases = [
            ('set CONFIRM_REPOSITORY', 'CONFIRM_REPOSITORY', 'synthetic/mainrag'),
            ('explicit owner settings authorization', 'CONFIRM_OWNER_SETTINGS_APPLY', 'yes'),
            ('confirm the documented single-maintainer', 'CONFIRM_SINGLE_MAINTAINER_MODEL', 'yes'),
            ('SETTINGS_OPERATOR is required', None, None),
        ]
        for message, key, value in cases:
            result = self.call('exec /bin/bash "$1" apply', environment=environment)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(message, result.stderr)
            self.assertNotIn('unexpected live GitHub action', result.stderr)
            if key:
                environment[key] = value


if __name__ == '__main__':
    unittest.main(verbosity=2)
