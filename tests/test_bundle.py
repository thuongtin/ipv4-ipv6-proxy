"""Disposable bundle and dispatcher checks; no live host operations."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import build_setup


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'scripts' / 'alpine'
OPERATIONS = ('preflight', 'status', 'verify', 'restart', 'credentials', 'export', 'rollback')


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='bundle-tests-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        # An allowlisted PATH prevents accidental package, network or service calls.
        for command in ('mkdir', 'rm', 'dirname', 'cat'):
            (self.bin / command).symlink_to(shutil.which(command))
        (self.bin / 'python3').symlink_to(sys.executable)
        self.log = self.root / 'forbidden.log'
        for command in ('apk', 'curl', 'wget', 'make', 'install', 'service', 'rc-service', 'ufw', 'ip'):
            self.write_executable(self.bin / command,
                                  '#!/bin/sh\nprintf "%s\\n" "$0" >> "$FORBIDDEN_LOG"\nexit 91\n')
        self.write_executable(self.bin / 'id', '#!/bin/sh\nexit 92\n')
        self.env = dict(os.environ, PATH=str(self.bin), FORBIDDEN_LOG=str(self.log),
                        PYTHONDONTWRITEBYTECODE='1')
        self.env.pop('PYTHONPATH', None)
        self.base = self.root / 'managed'

    def tearDown(self):
        self.assertFalse(self.log.exists(), 'A package, service or network command was attempted')

    @staticmethod
    def write_executable(path, content):
        path.write_text(content, encoding='utf-8')
        path.chmod(0o755)

    def run_command(self, *command):
        return subprocess.run(command, cwd=self.root, env=self.env, capture_output=True,
                              text=True, timeout=15)

    def build(self, source=SOURCE, name='setup.sh'):
        output = self.root / name
        build_setup.build(source, output)
        return output

    def check(self, output):
        return self.run_command(sys.executable, str(SOURCE / 'build_setup.py'),
                                '--check', '--output', str(output))

    def test_build_is_deterministic_and_executable(self):
        first = self.build(name='first.sh')
        second = self.build(name='second.sh')
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(first.stat().st_mode & 0o777, 0o755)
        self.assertNotIn(build_setup.MARKER, first.read_text())
        result = self.run_command('/bin/sh', '-n', str(first))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_check_current_bundle_does_not_rewrite(self):
        output = self.build()
        before = (output.read_bytes(), output.stat().st_mtime_ns, output.stat().st_ino)
        result = self.check(output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'BUNDLE_CHECK_PASS\n')
        self.assertEqual(before, (output.read_bytes(), output.stat().st_mtime_ns, output.stat().st_ino))

    def test_check_stale_bundle_fails_without_rewrite(self):
        output = self.build()
        output.write_text('stale fixture\n')
        before = (output.read_bytes(), output.stat().st_mtime_ns, output.stat().st_ino)
        result = self.check(output)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Bundle is stale', result.stderr)
        self.assertEqual(before, (output.read_bytes(), output.stat().st_mtime_ns, output.stat().st_ino))

    def test_check_missing_bundle_does_not_create_it(self):
        output = self.root / 'absent.sh'
        result = self.check(output)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Bundle is stale', result.stderr)
        self.assertFalse(output.exists())

    def test_standalone_extracts_exact_four_modules_without_host_checks(self):
        bundle = self.build()
        destination = self.root / 'tools with spaces'
        result = self.run_command('/bin/sh', str(bundle), '--extract-only', str(destination))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p.name for p in destination.iterdir()}, set(build_setup.MODULES))
        for name in build_setup.MODULES:
            expected = (SOURCE / name).read_text()
            if not expected.endswith('\n'):
                expected += '\n'
            self.assertEqual((destination / name).read_text(), expected)
        self.assertFalse(self.base.exists())

    def test_help_preempts_other_arguments_without_side_effects(self):
        bundle = self.build()
        destination = self.root / 'must-not-extract'
        for arguments in (('--help',), ('-h',), ('--extract-only', str(destination), '--help'),
                          ('--operation', 'restart', '--help'), ('--rollback', 'snapshot', '-h')):
            with self.subTest(arguments=arguments):
                result = self.run_command('/bin/sh', str(bundle), *arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Usage:', result.stdout)
                self.assertFalse(destination.exists())
                self.assertFalse(self.base.exists())

    def test_extract_only_rejects_invalid_arguments(self):
        bundle = self.build()
        for arguments in (('--extract-only',), ('--extract-only', ''),
                          ('--extract-only', 'unused', 'extra')):
            with self.subTest(arguments=arguments):
                result = self.run_command('/bin/sh', str(bundle), *arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('Usage: --extract-only DIRECTORY', result.stderr)
                self.assertFalse((self.root / 'unused').exists())

    def wrapper_fixture(self):
        scripts = self.root / 'scripts'
        scripts.mkdir()
        shutil.copytree(SOURCE, scripts / 'alpine', ignore=shutil.ignore_patterns('__pycache__'))
        common = (ROOT / 'scripts' / '_common.sh').read_text()
        common = common.replace('/var/lib/3proxy-setup', str(self.base))
        (scripts / '_common.sh').write_text(common)
        for operation in OPERATIONS:
            shutil.copyfile(ROOT / 'scripts' / (operation + '.sh'), scripts / (operation + '.sh'))
        return scripts

    def test_operation_wrappers_help_is_side_effect_free(self):
        scripts = self.wrapper_fixture()
        for operation in OPERATIONS:
            with self.subTest(operation=operation):
                result = self.run_command('/bin/sh', str(scripts / (operation + '.sh')), '--help')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('usage:', result.stdout.lower())
                self.assertFalse(self.base.exists())

    def operation_bundle(self):
        source = self.root / 'fixture-source'
        source.mkdir()
        template = (SOURCE / 'bootstrap.sh').read_text()
        marker = self.root / 'alpine-release'
        marker.touch()
        template = template.replace('/etc/alpine-release', str(marker))
        template = template.replace('BASE=/var/lib/3proxy-setup', 'BASE="' + str(self.base) + '"')
        template = template.replace('/tmp/3proxy-operation.XXXXXX', str(self.root / 'operation.XXXXXX'))
        (source / 'bootstrap.sh').write_text(template)
        self.stub = (
            'import json, pathlib, sys\n'
            'print(json.dumps({"argv": sys.argv[1:], "directory": str(pathlib.Path(__file__).parent), '
            '"modules": sorted(p.name for p in pathlib.Path(__file__).parent.glob("*.py"))}))\n'
            'raise SystemExit(7 if "fixture-fail" in sys.argv else 0)\n'
        )
        for name in build_setup.MODULES:
            (source / name).write_text(self.stub if name == 'ops.py' else '# Fixture module\n')
        self.write_executable(self.bin / 'id', '#!/bin/sh\nprintf "0\\n"\n')
        self.write_executable(self.bin / 'mktemp',
                              '#!/bin/sh\nexec ' + shutil.which('mktemp') + ' "$@"\n')
        return self.build(source)

    def test_dispatcher_uses_installed_operations_without_extraction(self):
        bundle = self.operation_bundle()
        tools = self.base / 'tools'
        tools.mkdir(parents=True)
        installed = tools / 'ops.py'
        installed.write_text(self.stub)
        before = installed.read_bytes()
        result = self.run_command('/bin/sh', str(bundle), '--operation', 'status', '--json', 'value with spaces')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['argv'], ['status', '--json', 'value with spaces'])
        self.assertEqual(payload['directory'], str(tools))
        self.assertEqual(installed.read_bytes(), before)
        self.assertEqual(list(self.root.glob('operation.*')), [])

    def test_dispatcher_fallback_extracts_temporarily_and_preserves_exit_code(self):
        bundle = self.operation_bundle()
        for arguments, expected_exit in ((('status', '--json'), 0), (('verify', 'fixture-fail'), 7)):
            with self.subTest(arguments=arguments):
                result = self.run_command('/bin/sh', str(bundle), '--operation', *arguments)
                self.assertEqual(result.returncode, expected_exit, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload['argv'], list(arguments))
                self.assertEqual(payload['modules'], sorted(build_setup.MODULES))
                self.assertFalse(Path(payload['directory']).exists())
                self.assertEqual(list(self.root.glob('operation.*')), [])
                self.assertFalse(self.base.exists())

    def test_non_alpine_or_non_root_rejected_before_any_operation(self):
        bundle = self.operation_bundle()
        self.write_executable(self.bin / 'id', '#!/bin/sh\nprintf "1000\\n"\n')
        result = self.run_command('/bin/sh', str(bundle), '--operation', 'status')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Root on Alpine is required', result.stderr)
        self.write_executable(self.bin / 'id', '#!/bin/sh\nprintf "0\\n"\n')
        (self.root / 'alpine-release').unlink()
        result = self.run_command('/bin/sh', str(bundle), '--operation', 'status')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Root on Alpine is required', result.stderr)
        self.assertFalse(self.base.exists())

    def test_dispatcher_rejects_missing_or_unknown_operation(self):
        bundle = self.operation_bundle()
        for arguments in (('--operation',), ('--operation', 'invalid')):
            with self.subTest(arguments=arguments):
                result = self.run_command('/bin/sh', str(bundle), *arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.base.exists())
                self.assertEqual(list(self.root.glob('operation.*')), [])


if __name__ == '__main__':
    unittest.main()
